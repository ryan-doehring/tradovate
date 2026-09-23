from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from tradovate_bot.config import Settings, TradingMode
from tradovate_bot.executor import ActionExecutor
from tradovate_bot.models import Side, TradePlan, parse_trade_row, row_is_active
from tradovate_bot.news.forexfactory import NewsEvent, active_news_window, in_blackout, should_reopen
from tradovate_bot.state import BotState, load_state, save_state
from tradovate_bot.sync.reconciler import ActionType, reconcile_trades

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def sample_vp2026_row() -> list:
    return json.loads((FIXTURES / "sample_sheet_row.json").read_text(encoding="utf-8"))


def test_row_is_active_when_outcome_empty(sample_vp2026_row):
    assert row_is_active(sample_vp2026_row) is True


def test_row_is_inactive_for_win_loss_miss():
    for outcome in ("Win", "Loss", "Miss"):
        row = [None] * 13
        row[1] = "MES"
        row[5] = outcome
        assert row_is_active(row) is False


def test_row_active_even_if_exit_filled():
    row = [None] * 13
    row[1] = "MES"
    row[5] = None
    row[7] = 7580.0
    assert row_is_active(row) is True


def test_parse_trade_row_from_sample(sample_vp2026_row):
    trade = parse_trade_row(
        2,
        sample_vp2026_row,
        default_contracts=1,
        resolve_symbol=lambda t: t.strip().upper().lstrip("/"),
    )
    assert trade is not None
    assert trade.ticker == "MES"
    assert trade.tradovate_symbol == "MES"
    assert trade.side is Side.SHORT
    assert trade.entry == 7590.0
    assert trade.target == 7575.0
    assert trade.stop_loss == 7597.5
    assert trade.quantity == 1


def test_default_contracts_when_empty():
    row = [
        "2026-07-19T00:00:00",
        "MNQ",
        "Setup",
        "Long",
        None,
        None,
        21000.0,
        None,
        21100.0,
        20950.0,
    ]
    trade = parse_trade_row(2, row, default_contracts=1, resolve_symbol=lambda t: t)
    assert trade is not None
    assert trade.quantity == 1


def test_match_key_uses_product_root_qty_entry():
    trade = TradePlan(
        row_number=2,
        date_added=date(2026, 7, 19),
        ticker="MES",
        side=Side.SHORT,
        quantity=1,
        entry=7590.0,
        target=7575.0,
        stop_loss=7597.5,
        tradovate_symbol="MESU6",
    )
    assert trade.match_key == ("MES", 1, 7590.0)
    assert "MES" in trade.client_tag
    assert "MESU6" not in trade.client_tag


def test_reconcile_places_missing_order():
    trade = TradePlan(
        row_number=2,
        date_added=date(2026, 7, 19),
        ticker="MES",
        side=Side.SHORT,
        quantity=1,
        entry=7590.0,
        target=7575.0,
        stop_loss=7597.5,
        tradovate_symbol="MESU6",
    )
    actions = reconcile_trades([trade], open_orders=[])
    assert len(actions) == 1
    assert actions[0].action is ActionType.PLACE_BRACKET


def test_reconcile_cancels_extra_order():
    trade = TradePlan(
        row_number=2,
        date_added=date(2026, 7, 19),
        ticker="MES",
        side=Side.SHORT,
        quantity=1,
        entry=7590.0,
        target=7575.0,
        stop_loss=7597.5,
        tradovate_symbol="MESU6",
    )
    open_orders = [
        {
            "id": 99,
            "symbol": "MNQU6",
            "orderQty": 1,
            "price": 21000.0,
            "orderType": "Limit",
            "ordStatus": "Working",
        }
    ]
    actions = reconcile_trades([trade], open_orders=open_orders)
    assert any(a.action is ActionType.CANCEL_ORDER for a in actions)


def test_dry_run_skips_execution():
    settings = Settings(trading_mode=TradingMode.DRY_RUN)
    trade = TradePlan(
        row_number=2,
        date_added=date(2026, 7, 19),
        ticker="MES",
        side=Side.SHORT,
        quantity=1,
        entry=7590.0,
        target=7575.0,
        stop_loss=7597.5,
        tradovate_symbol="MESU6",
    )
    from tradovate_bot.sync.reconciler import PlannedAction

    actions = [
        PlannedAction(
            action=ActionType.PLACE_BRACKET,
            reason="test",
            trade=trade,
            symbol="MESU6",
        )
    ]
    report = ActionExecutor(settings).execute(actions, job="test")
    assert len(report.planned_actions) == 1
    assert len(report.executed) == 0
    assert report.skipped[0]["skip_reason"] == "TRADING_MODE=dry_run"


def test_news_blackout_and_reopen_windows():
    from zoneinfo import ZoneInfo

    event = NewsEvent(
        event_id="test-nfp",
        title="Non-Farm Payrolls",
        currency="USD",
        impact="high",
        event_time=datetime(2026, 7, 19, 13, 30, tzinfo=ZoneInfo("UTC")),
    )
    assert in_blackout(
        event, datetime(2026, 7, 19, 13, 28, tzinfo=ZoneInfo("UTC")), buffer_minutes=5
    )
    assert not in_blackout(
        event, datetime(2026, 7, 19, 13, 20, tzinfo=ZoneInfo("UTC")), buffer_minutes=5
    )
    assert should_reopen(
        event, datetime(2026, 7, 19, 14, 0, tzinfo=ZoneInfo("UTC")), reopen_minutes=30
    )
    active = active_news_window(
        [event],
        datetime(2026, 7, 19, 13, 28, tzinfo=ZoneInfo("UTC")),
        buffer_minutes=5,
        reopen_minutes=30,
    )
    assert active is not None


def test_bot_state_persistence(tmp_path: Path):
    path = tmp_path / "state.json"
    state = BotState()
    state.pause_for_news("vp2026:2026-07-19:MES:7590.0", "test-nfp")
    save_state(path, state)
    loaded = load_state(path)
    assert "vp2026:2026-07-19:MES:7590.0" in loaded.paused_tags


def test_cme_calendar_regular_day():
    from tradovate_bot.calendar.cme import MarketCalendar

    cal = MarketCalendar()
    session = cal.session_for(date(2026, 7, 15))
    assert session.is_closed is False
    assert session.market_close > session.market_open
