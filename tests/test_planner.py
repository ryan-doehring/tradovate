from __future__ import annotations

from datetime import date

from tradovate_bot.models import EntryType, Side, TradePlan
from tradovate_bot.planner import Decision, plan_entry, plan_without_price, tick_aligned


def make_trade(
    *,
    entry_type=EntryType.OUTER,
    upper=110.0,
    lower=100.0,
    mid=105.0,
    target=22.0,
    stop=11.0,
    closeness=5.0,
    quantity=1,
    ticker="ES",
) -> TradePlan:
    return TradePlan(
        row_number=2,
        date_added=date(2026, 9, 9),
        ticker=ticker,
        setup_type="Weekly HVN",
        upper=upper,
        lower=lower,
        mid=mid,
        entry_type=entry_type,
        quantity=quantity,
        target_points=target,
        stop_points=stop,
        closeness_factor=closeness,
        tradovate_symbol=ticker,
    )


def test_outer_below_lower_shorts_the_retest():
    plan = plan_entry(make_trade(), price=93.0, tick_size=0.25)
    assert plan.decision is Decision.ARM
    assert plan.level == 100.0
    assert plan.side is Side.SHORT
    assert (plan.entry, plan.target, plan.stop_loss) == (100.0, 78.0, 111.0)


def test_outer_above_upper_goes_long_the_retest():
    plan = plan_entry(make_trade(), price=117.0, tick_size=0.25)
    assert plan.decision is Decision.ARM
    assert plan.level == 110.0
    assert plan.side is Side.LONG
    assert (plan.entry, plan.target, plan.stop_loss) == (110.0, 132.0, 99.0)


def test_closeness_band_blocks_arming():
    for price in (95.0, 100.0, 105.0, 110.0, 115.0):
        plan = plan_entry(make_trade(), price=price, tick_size=0.25)
        assert plan.decision is Decision.BLOCKED_CLOSENESS, price
        assert plan.entry is None


def test_mid_retest_from_either_side():
    below = plan_entry(make_trade(entry_type=EntryType.MID), price=93.0, tick_size=0.25)
    assert below.decision is Decision.ARM
    assert below.level == 105.0
    assert below.side is Side.SHORT
    assert (below.entry, below.target, below.stop_loss) == (105.0, 83.0, 116.0)

    above = plan_entry(make_trade(entry_type=EntryType.MID), price=117.0, tick_size=0.25)
    assert above.level == 105.0
    assert above.side is Side.LONG
    assert (above.entry, above.target, above.stop_loss) == (105.0, 127.0, 94.0)


def test_off_tick_prices_are_rejected_not_rounded():
    # ES ticks 0.25, so a 0.10 target offset is off the grid.
    plan = plan_entry(make_trade(target=22.1), price=93.0, tick_size=0.25)
    assert plan.decision is Decision.OFF_TICK
    assert "target" in plan.reason
    assert plan.entry == 100.0  # still reported for inspection


def test_unknown_tick_size_skips_the_grid_check():
    plan = plan_entry(make_trade(target=22.1), price=93.0, tick_size=None)
    assert plan.decision is Decision.ARM


def test_malformed_closeness_cannot_produce_a_marketable_order():
    # The sheet parser normalises a negative factor to 0, but the planner must still
    # refuse to create an order that would fill instantly.
    outer = plan_entry(make_trade(closeness=-15.0), price=105.0, tick_size=0.25)
    assert outer.decision is Decision.NO_SETUP

    mid = plan_entry(
        make_trade(entry_type=EntryType.MID, closeness=-15.0),
        price=105.0,
        tick_size=0.25,
    )
    assert mid.decision is Decision.INVALID


def test_plan_without_price_reports_the_row():
    plan = plan_without_price(make_trade(), reason="quote is 42 min old")
    assert plan.decision is Decision.NO_PRICE
    assert plan.is_armed is False
    assert plan.to_dict()["row"] == 2


def test_tick_aligned_helper():
    assert tick_aligned(100.25, 0.25)
    assert not tick_aligned(100.10, 0.25)
    assert tick_aligned(100.10, None)