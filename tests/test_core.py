from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tradovate_bot.calendar.cme import MarketCalendar
from tradovate_bot.models import EntryType, SkipReason, parse_trade_row, row_is_active
from tradovate_bot.state import ROW_ARMED, ROW_FLAT, ROW_OPEN, BotState, load_state, save_state

FIXTURES = Path(__file__).parent / "fixtures"


def make_row(
    *,
    ticker="ES",
    upper=110,
    lower=100,
    mid=105,
    entry_type="Outer",
    contracts=1,
    outcome=None,
    target=22,
    stop=11,
    closeness=5,
) -> list:
    """One `VP` sheet row in sheet order: A..L."""
    return [
        "2026-09-09",
        ticker,
        "Weekly HVN",
        upper,
        lower,
        mid,
        entry_type,
        contracts,
        outcome,
        target,
        stop,
        closeness,
    ]


def parse(row: list, **overrides):
    params = {
        "default_contracts": 1,
        "default_closeness_factor": 0.0,
        "resolve_symbol": lambda ticker: str(ticker).strip().upper(),
    }
    params.update(overrides)
    return parse_trade_row(2, row, **params)


def test_row_is_active_only_while_outcome_empty():
    assert row_is_active(make_row()) is True
    assert row_is_active(make_row(outcome="  ")) is True
    for outcome in ("Win", "Loss", "Miss", "open"):
        assert row_is_active(make_row(outcome=outcome)) is False


def test_sample_sheet_row_parses():
    row = json.loads((FIXTURES / "sample_sheet_row.json").read_text(encoding="utf-8"))
    parsed = parse(row)
    assert parsed.trade is not None
    trade = parsed.trade
    assert trade.row_number == 2
    assert trade.ticker == "ES"
    assert trade.setup_type == "Weekly HVN"
    assert (trade.upper, trade.lower, trade.mid) == (110.0, 100.0, 105.0)
    assert trade.entry_type is EntryType.OUTER
    assert trade.quantity == 1
    assert (trade.target_points, trade.stop_points) == (22.0, 11.0)
    assert trade.closeness_factor == 5.0
    assert trade.closeness_band == (95.0, 115.0)
    assert trade.client_tag == "vp:r2:ES"


def test_mid_defaults_to_range_centre():
    parsed = parse(make_row(mid=None))
    assert parsed.trade is not None
    assert parsed.trade.mid == 105.0


def test_entry_type_is_case_insensitive():
    assert parse(make_row(entry_type="mid")).trade.entry_type is EntryType.MID
    assert parse(make_row(entry_type=" Outer ")).trade.entry_type is EntryType.OUTER


def test_skip_reasons():
    assert parse(make_row(outcome="Win")).reason is SkipReason.OUTCOME_SET
    assert parse(make_row(ticker=None)).reason is SkipReason.MISSING_TICKER
    assert parse(make_row(entry_type="Both")).reason is SkipReason.INVALID_ENTRY_TYPE
    assert parse(make_row(upper=None)).reason is SkipReason.MISSING_RANGE
    assert parse(make_row(upper=100, lower=110)).reason is SkipReason.INVALID_RANGE
    assert parse(make_row(mid=120)).reason is SkipReason.MID_OUTSIDE_RANGE
    assert parse(make_row(target=None)).reason is SkipReason.MISSING_TARGET
    assert parse(make_row(stop=None)).reason is SkipReason.MISSING_STOP
    assert parse(make_row(target=0)).reason is SkipReason.NON_POSITIVE_POINTS


def test_closeness_falls_back_when_blank_or_negative():
    assert parse(make_row(closeness=None)).trade.closeness_factor == 0.0
    assert parse(make_row(closeness=-3)).trade.closeness_factor == 0.0
    fallback = parse(make_row(closeness=None), default_closeness_factor=2.5)
    assert fallback.trade.closeness_factor == 2.5


def test_row_lifecycle_and_tracking(tmp_path):
    state = BotState()
    state.mark_armed(
        "vp:r2:ES",
        order_id=99,
        symbol="ESZ6",
        side="Short",
        entry=100.0,
        target=78.0,
        stop_loss=111.0,
        quantity=1,
    )
    assert state.row("vp:r2:ES").status == ROW_ARMED
    assert 99 in state.tracked_orders()

    state.mark_open("vp:r2:ES")
    assert state.row("vp:r2:ES").status == ROW_OPEN

    state.mark_flat("vp:r2:ES")
    assert state.row("vp:r2:ES").status == ROW_FLAT
    assert state.tracked_orders() == {}

    path = tmp_path / "state.json"
    save_state(path, state)
    assert load_state(path).row("vp:r2:ES").status == ROW_FLAT


def test_state_tolerates_corrupt_file(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    assert load_state(path).rows == {}


def test_news_cache_expires(tmp_path):
    state = BotState()
    now = datetime(2026, 9, 9, 13, 0, tzinfo=UTC)
    state.store_news([{"event_id": "x", "event_time": now.isoformat()}], now=now)
    assert state.cached_news(now, max_age_minutes=30) is not None
    assert state.cached_news(now.replace(minute=45), max_age_minutes=30) is None

    path = tmp_path / "state.json"
    save_state(path, state)
    assert load_state(path).news_events


def test_market_open_covers_overnight_and_maintenance_halt():
    cal = MarketCalendar()
    ct = ZoneInfo("America/Chicago")
    # Wednesday 2026-09-16 trades from Tue 17:00 CT through Wed 16:00 CT.
    assert cal.is_market_open(datetime(2026, 9, 16, 8, 0, tzinfo=ct))
    assert cal.is_market_open(datetime(2026, 9, 16, 3, 0, tzinfo=ct))
    assert not cal.is_market_open(datetime(2026, 9, 16, 16, 30, tzinfo=ct))
    # 19:00 CT Wednesday already belongs to Thursday's session.
    assert cal.is_market_open(datetime(2026, 9, 16, 19, 0, tzinfo=ct))
    # Weekend is closed.
    assert not cal.is_market_open(datetime(2026, 9, 19, 12, 0, tzinfo=ct))


def test_pre_close_window_follows_the_calendar():
    cal = MarketCalendar()
    ct = ZoneInfo("America/Chicago")
    assert not cal.is_pre_close(datetime(2026, 9, 16, 15, 30, tzinfo=ct), minutes_before_close=20)
    assert cal.is_pre_close(datetime(2026, 9, 16, 15, 50, tzinfo=ct), minutes_before_close=20)
    # Early close (day after Thanksgiving, 12:00 CT) moves the window with it.
    assert cal.is_pre_close(datetime(2026, 11, 27, 11, 50, tzinfo=ct), minutes_before_close=20)