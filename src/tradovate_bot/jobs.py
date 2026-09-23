"""Job orchestration.

One ``tick`` per cycle: guards first (pre-close or red news => flat and no working
orders), otherwise reconcile the sheet against the broker. The named jobs
(``sync`` / ``news-guard`` / ``pre-close``) are thin aliases of the same logic, kept
for manual runs; the tick decides which branch applies.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tradovate_bot.calendar.cme import MarketCalendar
from tradovate_bot.config import Settings, TradingMode, load_settings
from tradovate_bot.executor import ActionExecutor, RunReport, write_report
from tradovate_bot.models import TradePlan
from tradovate_bot.news.forexfactory import (
    NewsEvent,
    NewsScrapeError,
    fetch_usd_high_impact_events,
    in_flat_window,
)
from tradovate_bot.planner import EntryPlan, plan_entry, plan_without_price
from tradovate_bot.prices import PriceError, QuoteCache, freshness_reason, resolve_quote
from tradovate_bot.sheets.reader import SheetRead, SheetReader
from tradovate_bot.state import ROW_OPEN, BotState, load_state, save_state
from tradovate_bot.sync.reconciler import (
    ActionType,
    PlannedAction,
    order_id_of,
    order_tag,
    reconcile,
)
from tradovate_bot.tradovate.client import TradovateClient, is_working
from tradovate_bot.tradovate.contracts import (
    fetch_tick_size,
    resolve_active_contract,
    tick_size_for,
)

STATE_PATH = Path("data/state.json")
NEWS_CACHE_MINUTES = 30


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


def _sheet_reader(settings: Settings) -> SheetReader:
    if not settings.google_service_account_json or not settings.google_sheet_id:
        raise RuntimeError("Google Sheets credentials are not configured")
    return SheetReader(
        service_account_json=settings.google_service_account_json,
        sheet_id=settings.google_sheet_id,
        tab_name=settings.google_sheet_tab,
        default_contracts=settings.default_contracts,
        default_closeness_factor=settings.default_closeness_factor,
        resolve_symbol=settings.resolve_symbol,
    )


def _resolve_contracts(
    trades: list[TradePlan],
    client: TradovateClient | None,
    settings: Settings,
) -> list[TradePlan]:
    """Replace product roots (ES) with the active contract month (ESZ6)."""
    if client is None:
        return trades
    cache: dict[str, str] = {}
    resolved: list[TradePlan] = []
    for trade in trades:
        root = trade.tradovate_symbol
        if root not in cache:
            cache[root] = resolve_active_contract(
                client,
                root,
                min_days_to_expiry=settings.min_days_to_expiry,
            )
        resolved.append(trade.with_symbol(cache[root]))
    return resolved


def _tick_sizes(
    trades: list[TradePlan],
    client: TradovateClient | None,
    settings: Settings,
) -> dict[str, float | None]:
    """Tick size per product root: broker value when available, static map otherwise."""
    sizes: dict[str, float | None] = {}
    for trade in trades:
        root = trade.product_root
        if root in sizes:
            continue
        size = fetch_tick_size(client, trade.tradovate_symbol) if client is not None else None
        sizes[root] = size or tick_size_for(root)
    return sizes


def _within_news_scan_hours(settings: Settings, now: datetime) -> bool:
    hour = now.astimezone(ZoneInfo(settings.timezone)).hour
    return settings.news_scan_start_hour <= hour < settings.news_scan_end_hour


def _news_events(
    settings: Settings,
    state: BotState,
    now: datetime,
    artifact_dir: Path | None,
) -> list[NewsEvent]:
    """Cached calendar when fresh enough, otherwise scrape (and cache) it."""
    cached = state.cached_news(now, max_age_minutes=NEWS_CACHE_MINUTES)
    if cached is not None:
        events = [NewsEvent.from_dict(item) for item in cached]
        return [event for event in events if event is not None]
    if not _within_news_scan_hours(settings, now):
        return []
    try:
        events = fetch_usd_high_impact_events(timezone=settings.timezone)
    except NewsScrapeError as exc:
        _write_scrape_debug(artifact_dir, exc)
        raise
    state.store_news([event.to_dict() for event in events], now=now)
    return events


def _flat_reason(
    settings: Settings,
    calendar: MarketCalendar,
    events: list[NewsEvent],
    now: datetime,
) -> str | None:
    """Why we must be flat with no working orders right now, if we must."""
    if calendar.is_pre_close(now, minutes_before_close=settings.eod_minutes_before_close):
        return "pre_close"
    for event in events:
        if in_flat_window(
            event,
            now,
            buffer_minutes=settings.news_buffer_minutes,
            reopen_minutes=settings.news_reopen_minutes,
        ):
            return f"news:{event.event_id}"
    return None


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


def _refresh_rows(
    state: BotState,
    orders: list[dict],
    net_positions: dict[str, int],
    now: datetime,
) -> list[dict]:
    """Sync row lifecycles from the broker so fills/completions free a row up again."""
    notes: list[dict] = []
    by_id: dict[int, dict] = {}
    for order in orders:
        order_id = order_id_of(order)
        if order_id is not None:
            by_id[order_id] = order

    for order_id, row in list(state.tracked_orders().items()):
        order = by_id.get(order_id)
        if order is None:
            notes.append({"tag": row.tag, "note": "order_not_found_at_broker"})
            state.mark_idle(row.tag, now=now)
            continue
        status = str(order.get("ordStatus") or "").strip().lower()
        if status == "filled":
            state.mark_open(row.tag, now=now)
        elif not is_working(order):
            if net_positions.get(row.symbol, 0) == 0:
                state.mark_flat(row.tag, now=now)
            else:
                state.mark_open(row.tag, now=now)

    for row in list(state.rows.values()):
        if row.status != ROW_OPEN or not row.symbol:
            continue
        if net_positions.get(row.symbol, 0) == 0:
            state.mark_flat(row.tag, now=now)
            notes.append({"tag": row.tag, "note": "position_closed"})
    return notes


def _build_plans(
    settings: Settings,
    trades: list[TradePlan],
    *,
    client: TradovateClient | None,
    cache: QuoteCache,
    tick_sizes: dict[str, float | None],
    read_cell,
    now: datetime,
) -> tuple[list[EntryPlan], list[dict]]:
    """One price per product root per run, then plan each row."""
    plans: list[EntryPlan] = []
    price_notes: list[dict] = []

    for trade in trades:
        root = trade.product_root
        quote = cache.get(root)
        if quote is None:
            try:
                quote = resolve_quote(
                    root=root,
                    symbol=trade.tradovate_symbol,
                    source=settings.price_source,
                    client=client,
                    read_cell=read_cell,
                    sheet_cell=settings.sheet_price_cell,
                    sheet_time_cell=settings.sheet_price_time_cell,
                    yahoo_symbols=settings.yahoo_symbol_map,
                )
            except PriceError as exc:
                plans.append(plan_without_price(trade, reason=f"price unavailable: {exc}"))
                continue
            cache.put(root, quote)
            price_notes.append(
                {
                    "root": root,
                    "price": quote.price,
                    "source": quote.source,
                    "as_of": quote.as_of.isoformat(),
                }
            )

        stale = freshness_reason(quote, max_age_minutes=settings.max_price_age_minutes, now=now)
        if stale:
            plans.append(plan_without_price(trade, reason=stale))
            continue

        plans.append(plan_entry(trade, price=quote.price, tick_size=tick_sizes.get(root)))

    return plans, price_notes


def _sync(
    settings: Settings,
    state: BotState,
    now: datetime,
    *,
    client: TradovateClient | None,
    account: dict | None,
    working_orders: list[dict],
    positions: dict[str, int],
    artifact_dir: Path | None,
    job: str,
) -> int:
    reader = _sheet_reader(settings)
    read: SheetRead = reader.fetch_plan()
    trades = _resolve_contracts(read.trades, client, settings)
    tick_sizes = _tick_sizes(trades, client, settings)

    wants_sheet_price = bool(settings.sheet_price_cell) or settings.price_source == "sheet"
    read_cell = reader.read_cell if wants_sheet_price else None
    plans, price_notes = _build_plans(
        settings,
        trades,
        client=client,
        cache=QuoteCache(),
        tick_sizes=tick_sizes,
        read_cell=read_cell,
        now=now,
    )

    orders = client.list_orders() if client is not None else []
    lifecycle_notes = _refresh_rows(state, orders, positions, now)

    result = reconcile(
        plans,
        working_orders,
        state=state,
        is_bot_tag=settings.is_bot_tag,
        is_legacy_tag=settings.is_legacy_tag,
        net_positions=positions,
    )

    executor = ActionExecutor(settings, client)
    report = executor.execute(result.actions, job=job, account=account)
    report.decisions.extend(plan.to_dict() for plan in plans)
    report.decisions.extend(read.skipped)
    report.notes.extend(price_notes)
    report.notes.extend(lifecycle_notes)
    report.notes.extend(result.notes)

    for placed in report.placed_orders:
        state.mark_armed(
            str(placed["tag"]),
            order_id=placed.get("order_id"),
            symbol=str(placed.get("symbol") or ""),
            side=str(placed.get("side") or ""),
            entry=placed.get("entry"),
            target=placed.get("target"),
            stop_loss=placed.get("stop_loss"),
            quantity=placed.get("quantity"),
            now=now,
        )

    active_tags = {trade.client_tag for trade in trades}
    for tag in list(state.rows):
        if tag not in active_tags:
            state.forget(tag)

    save_state(STATE_PATH, state)
    if artifact_dir:
        write_report(report, artifact_dir)
    return 1 if report.errors else 0


def _flatten(
    settings: Settings,
    state: BotState,
    now: datetime,
    *,
    client: TradovateClient | None,
    account: dict | None,
    working_orders: list[dict],
    positions: dict[str, int],
    reason: str,
    artifact_dir: Path | None,
    job: str,
) -> int:
    """Cancel every bot order and flatten every position: nothing may hold risk now."""
    actions: list[PlannedAction] = []
    for order in working_orders:
        tag = order_tag(order)
        if not settings.is_bot_tag(tag):
            continue
        actions.append(
            PlannedAction(
                action=ActionType.CANCEL_ORDER,
                reason=reason,
                order_id=order_id_of(order),
                symbol=str(order.get("symbol") or ""),
                details={"tag": tag},
            )
        )
    for symbol, quantity in positions.items():
        if quantity:
            actions.append(
                PlannedAction(
                    action=ActionType.FLATTEN_POSITION,
                    reason=reason,
                    symbol=symbol,
                    details={"net_positions": quantity},
                )
            )

    executor = ActionExecutor(settings, client)
    report = executor.execute(actions, job=job, account=account)
    report.skipped.append({"reason": reason})

    for order in working_orders:
        tag = order_tag(order)
        if settings.is_bot_tag(tag):
            state.mark_flat(tag, now=now)

    save_state(STATE_PATH, state)
    if artifact_dir:
        write_report(report, artifact_dir)
    return 1 if report.errors else 0


def _empty_report(settings: Settings, job: str) -> RunReport:
    return RunReport(
        mode=settings.trading_mode.value,
        job=job,
        timestamp=datetime.now(UTC).isoformat(),
    )


def run_tick(
    *,
    dry_run: bool = False,
    artifact_dir: Path | None = None,
    job: str = "tick",
) -> int:
    """One cycle: guards first, otherwise reconcile the sheet against the broker."""
    settings = load_settings()
    if dry_run:
        settings.trading_mode = TradingMode.DRY_RUN

    now = datetime.now(UTC)
    calendar = MarketCalendar(timezone=settings.timezone)
    state = load_state(STATE_PATH)

    if not calendar.is_market_open(now):
        report = _empty_report(settings, job)
        report.skipped.append({"reason": "market_closed"})
        if artifact_dir:
            write_report(report, artifact_dir)
        return 0

    events = _news_events(settings, state, now, artifact_dir)
    reason = _flat_reason(settings, calendar, events, now)

    client: TradovateClient | None = None
    account: dict | None = None
    working_orders: list[dict] = []
    positions: dict[str, int] = {}

    if settings.tradovate_username:
        client, account = _authenticate(settings)
        working_orders = client.list_working_orders()
        positions = client.net_positions()

    try:
        if reason:
            return _flatten(
                settings,
                state,
                now,
                client=client,
                account=account,
                working_orders=working_orders,
                positions=positions,
                reason=reason,
                artifact_dir=artifact_dir,
                job=job,
            )
        return _sync(
            settings,
            state,
            now,
            client=client,
            account=account,
            working_orders=working_orders,
            positions=positions,
            artifact_dir=artifact_dir,
            job=job,
        )
    except Exception as exc:  # noqa: BLE001 - leave an audit artifact, then fail loudly
        report = _empty_report(settings, job)
        report.errors.append(f"unhandled: {exc!r}")
        if artifact_dir:
            write_report(report, artifact_dir)
        raise
    finally:
        if client is not None:
            client.close()


def run_sync(*, dry_run: bool = False, artifact_dir: Path | None = None) -> int:
    """Manual alias for a tick, reported under the trade-sync job name."""
    return run_tick(dry_run=dry_run, artifact_dir=artifact_dir, job="trade-sync")


def run_news_guard(*, dry_run: bool = False, artifact_dir: Path | None = None) -> int:
    """Manual alias for a tick, reported under the news-guard job name."""
    return run_tick(dry_run=dry_run, artifact_dir=artifact_dir, job="news-guard")


def run_pre_close(*, dry_run: bool = False, artifact_dir: Path | None = None) -> int:
    """Manual alias for a tick, reported under the pre-close job name."""
    return run_tick(dry_run=dry_run, artifact_dir=artifact_dir, job="pre-close")