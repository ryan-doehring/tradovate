from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from tradovate_bot.calendar.cme import MarketCalendar
from tradovate_bot.config import Settings, TradingMode, load_settings
from tradovate_bot.executor import ActionExecutor, write_report
from tradovate_bot.news.forexfactory import (
    NewsScrapeError,
    active_news_window,
    fetch_usd_high_impact_events,
    in_blackout,
    should_reopen,
)
from tradovate_bot.sheets.reader import SheetReader
from tradovate_bot.state import load_state, save_state
from tradovate_bot.sync.reconciler import ActionType, PlannedAction, reconcile_trades
from tradovate_bot.tradovate.client import TradovateClient
from tradovate_bot.tradovate.contracts import resolve_active_contract


def _authenticate(settings: Settings) -> tuple[TradovateClient, dict]:
    client = TradovateClient(base_url=settings.tradovate_api_url)
    client.authenticate(
        username=settings.tradovate_username,
        password=settings.tradovate_password,
        app_id=settings.tradovate_app_id,
        cid=settings.tradovate_cid,
        sec=settings.tradovate_sec,
    )
    account = client.get_primary_account()
    return client, account


def _resolve_front_months(
    trades: list,
    client: TradovateClient | None,
    *,
    min_days_to_expiry: int,
) -> list:
    """Replace product roots (MES) with active contract months (MESU6)."""
    if client is None:
        return trades

    cache: dict[str, str] = {}
    resolved = []
    for trade in trades:
        root = trade.tradovate_symbol
        if root not in cache:
            cache[root] = resolve_active_contract(
                client, root, min_days_to_expiry=min_days_to_expiry
            )
        resolved.append(
            type(trade)(
                **{
                    **trade.__dict__,
                    "tradovate_symbol": cache[root],
                }
            )
        )
    return resolved


def _read_sheet(settings: Settings):
    if not settings.google_service_account_json or not settings.google_sheet_id:
        raise RuntimeError("Google Sheets credentials are not configured")
    reader = SheetReader(
        service_account_json=settings.google_service_account_json,
        sheet_id=settings.google_sheet_id,
        tab_name=settings.google_sheet_tab,
        default_contracts=settings.default_contracts,
        resolve_symbol=settings.resolve_symbol,
    )
    return reader.fetch_active_trades()


def _write_scrape_debug(artifact_dir: Path | None, exc: NewsScrapeError) -> None:
    """Persist the raw page so a broken scrape can be debugged from the run artifact."""
    if artifact_dir is None:
        return
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "forexfactory-debug.html").write_text(exc.html, encoding="utf-8")
    (artifact_dir / "news-scrape-error.json").write_text(
        json.dumps({"detail": exc.detail, "status_code": exc.status_code}, indent=2),
        encoding="utf-8",
    )


def run_sync(*, dry_run: bool = False, artifact_dir: Path | None = None) -> int:
    settings = load_settings()
    if dry_run:
        settings.trading_mode = TradingMode.DRY_RUN

    calendar = MarketCalendar(timezone=settings.timezone)
    now = datetime.now(timezone.utc)
    if not calendar.is_market_open(now):
        report = ActionExecutor(settings).execute([], job="trade-sync")
        report.skipped.append({"reason": "market_closed"})
        if artifact_dir:
            write_report(report, artifact_dir)
        return 0

    state = load_state(Path("data/state.json"))
    stale_pauses = state.prune_stale_pauses(now, ttl_minutes=settings.news_pause_ttl_minutes)
    trades = _read_sheet(settings)

    client: TradovateClient | None = None
    account = None
    open_orders: list = []

    if settings.tradovate_username:
        client, account = _authenticate(settings)
        open_orders = client.list_working_orders()
        trades = _resolve_front_months(
            trades, client, min_days_to_expiry=settings.min_days_to_expiry
        )

    actions = reconcile_trades(trades, open_orders, paused_tags=state.paused_tags)
    executor = ActionExecutor(settings, client)
    report = executor.execute(actions, job="trade-sync", account=account)
    if stale_pauses:
        report.skipped.append({"reason": "stale_news_pause_cleared", "client_tags": stale_pauses})

    if client:
        client.close()

    if artifact_dir:
        write_report(report, artifact_dir)
    save_state(Path("data/state.json"), state)
    return 1 if report.errors else 0


def run_news_guard(*, dry_run: bool = False, artifact_dir: Path | None = None) -> int:
    settings = load_settings()
    if dry_run:
        settings.trading_mode = TradingMode.DRY_RUN

    tz = settings.timezone
    now = datetime.now(timezone.utc)
    try:
        events = fetch_usd_high_impact_events(timezone=tz)
    except NewsScrapeError as exc:
        _write_scrape_debug(artifact_dir, exc)
        raise
    active = active_news_window(
        events,
        now,
        buffer_minutes=settings.news_buffer_minutes,
        reopen_minutes=settings.news_reopen_minutes,
    )

    state = load_state(Path("data/state.json"))
    stale_pauses = state.prune_stale_pauses(now, ttl_minutes=settings.news_pause_ttl_minutes)
    actions: list[PlannedAction] = []

    client: TradovateClient | None = None
    account = None

    if active and in_blackout(active, now, buffer_minutes=settings.news_buffer_minutes):
        if settings.tradovate_username:
            client, account = _authenticate(settings)
            open_orders = client.list_working_orders()
            positions = client.list_positions()

            for order in open_orders:
                order_id = order.get("id") or order.get("orderId")
                tag = str(order.get("customTag50", ""))
                if tag.startswith("vp2026:"):
                    state.pause_for_news(tag, active.event_id)
                actions.append(
                    PlannedAction(
                        action=ActionType.CANCEL_ORDER,
                        reason=f"news_blackout:{active.event_id}",
                        order_id=int(order_id) if order_id is not None else None,
                        symbol=str(order.get("symbol", "")),
                    )
                )

            for position in positions:
                net_qty = position.get("netPos", 0)
                if net_qty and net_qty != 0:
                    symbol = str(position.get("symbol", ""))
                    actions.append(
                        PlannedAction(
                            action=ActionType.FLATTEN_POSITION,
                            reason=f"news_blackout:{active.event_id}",
                            symbol=symbol,
                        )
                    )

    elif active and should_reopen(active, now, reopen_minutes=settings.news_reopen_minutes):
        trades = _read_sheet(settings)
        if settings.tradovate_username:
            client, account = _authenticate(settings)
            trades = _resolve_front_months(
                trades, client, min_days_to_expiry=settings.min_days_to_expiry
            )

        paused_tags = {
            tag for tag in state.paused_tags if state.paused_event_id(tag) == active.event_id
        }

        for trade in trades:
            if trade.client_tag not in paused_tags:
                continue
            actions.append(
                PlannedAction(
                    action=ActionType.PLACE_BRACKET,
                    reason=f"news_reopen:{active.event_id}",
                    trade=trade,
                    symbol=trade.tradovate_symbol,
                    details={
                        "entry": trade.entry,
                        "target": trade.target,
                        "stop_loss": trade.stop_loss,
                    },
                )
            )
            state.clear_pause(trade.client_tag)

    executor = ActionExecutor(settings, client)
    report = executor.execute(actions, job="news-guard", account=account)
    if stale_pauses:
        report.skipped.append({"reason": "stale_news_pause_cleared", "client_tags": stale_pauses})

    if client:
        client.close()

    save_state(Path("data/state.json"), state)
    if artifact_dir:
        write_report(report, artifact_dir)
    return 1 if report.errors else 0


def run_eod_flat(*, dry_run: bool = False, artifact_dir: Path | None = None) -> int:
    settings = load_settings()
    if dry_run:
        settings.trading_mode = TradingMode.DRY_RUN

    calendar = MarketCalendar(timezone=settings.timezone)
    now = datetime.now(timezone.utc)

    if not calendar.is_eod_window(now, minutes_before_close=settings.eod_minutes_before_close):
        report = ActionExecutor(settings).execute([], job="eod-flat")
        report.skipped.append({"reason": "outside_eod_window"})
        if artifact_dir:
            write_report(report, artifact_dir)
        return 0

    actions: list[PlannedAction] = []
    client: TradovateClient | None = None
    account = None

    if settings.tradovate_username:
        client, account = _authenticate(settings)
        positions = client.list_positions()
        for position in positions:
            net_qty = position.get("netPos", 0)
            if net_qty and net_qty != 0:
                symbol = str(position.get("symbol", ""))
                actions.append(
                    PlannedAction(
                        action=ActionType.FLATTEN_POSITION,
                        reason="eod_flat_positions_only",
                        symbol=symbol,
                    )
                )

    executor = ActionExecutor(settings, client)
    report = executor.execute(actions, job="eod-flat", account=account)

    if client:
        client.close()

    if artifact_dir:
        write_report(report, artifact_dir)
    return 1 if report.errors else 0
