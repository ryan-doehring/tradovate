from __future__ import annotations

from datetime import date

from tradovate_bot.models import EntryType, TradePlan
from tradovate_bot.planner import plan_entry
from tradovate_bot.state import BotState
from tradovate_bot.sync.reconciler import ActionType, reconcile

TAG = "vp:r2:ES"
LEGACY_TAG = "vp2026:2026-09-09:ES:110.0"


def is_bot_tag(tag: str) -> bool:
    """Mirror Settings.is_bot_tag: current prefix or any legacy prefix."""
    return tag.startswith("vp:") or tag.startswith("vp2026")


def is_legacy_tag(tag: str) -> bool:
    return tag.startswith("vp2026")


def make_trade(**overrides) -> TradePlan:
    values: dict = {
        "row_number": 2,
        "date_added": date(2026, 9, 9),
        "ticker": "ES",
        "setup_type": "Weekly HVN",
        "upper": 110.0,
        "lower": 100.0,
        "mid": 105.0,
        "entry_type": EntryType.OUTER,
        "quantity": 1,
        "target_points": 22.0,
        "stop_points": 11.0,
        "closeness_factor": 5.0,
        "tradovate_symbol": "ESZ6",
    }
    values.update(overrides)
    return TradePlan(**values)


def armed_plan(price: float = 93.0, **overrides):
    return plan_entry(make_trade(**overrides), price=price, tick_size=0.25)


def working_order(**overrides) -> dict:
    order = {
        "id": 5001,
        "action": "Sell",
        "symbol": "ESZ6",
        "orderQty": 1,
        "price": 100.0,
        "orderType": "Limit",
        "ordStatus": "Working",
        "customTag50": TAG,
    }
    order.update(overrides)
    return order


def run(plans, orders, *, state=None, allow_placement=True, net=None):
    return reconcile(
        plans,
        orders,
        state=state or BotState(),
        is_bot_tag=is_bot_tag,
        is_legacy_tag=is_legacy_tag,
        allow_placement=allow_placement,
        net_positions=net,
    )


def test_places_when_nothing_is_working():
    result = run([armed_plan()], [])
    assert [action.action for action in result.actions] == [ActionType.PLACE_BRACKET]
    assert result.actions[0].plan.entry == 100.0


def test_leaves_an_already_armed_row_alone():
    result = run([armed_plan()], [working_order()])
    assert result.actions == []
    assert result.notes[0]["note"] == "already_armed"


def test_replaces_an_order_whose_plan_changed():
    result = run([armed_plan(entry_type=EntryType.MID)], [working_order()])
    assert [action.action for action in result.actions] == [
        ActionType.CANCEL_ORDER,
        ActionType.PLACE_BRACKET,
    ]


def test_blocked_row_keeps_its_resting_order():
    blocked = plan_entry(make_trade(), price=105.0, tick_size=0.25)
    assert blocked.is_armed is False
    result = run([blocked], [working_order()])
    assert result.actions == []


def test_row_removed_from_the_sheet_is_cancelled():
    result = run([], [working_order()])
    assert [action.action for action in result.actions] == [ActionType.CANCEL_ORDER]
    assert result.actions[0].order_id == 5001


def test_legacy_tag_is_migrated_by_cancel_and_replace():
    result = run([armed_plan()], [working_order(customTag50=LEGACY_TAG)])
    assert [action.action for action in result.actions] == [
        ActionType.CANCEL_ORDER,
        ActionType.PLACE_BRACKET,
    ]


def test_flat_window_blocks_placement_but_still_cleans_up():
    result = run([armed_plan()], [working_order(customTag50=LEGACY_TAG)], allow_placement=False)
    assert [action.action for action in result.actions] == [ActionType.CANCEL_ORDER]


def test_open_position_is_not_re_entered():
    state = BotState()
    state.mark_open(TAG)
    result = run([armed_plan()], [], state=state)
    assert result.actions == []
    assert result.notes[0]["note"] == "position_open"


def test_untracked_position_blocks_placement():
    result = run([armed_plan()], [], net={"ESZ6": 2})
    assert result.actions == []
    assert result.notes[0]["note"] == "untracked_position"


def test_foreign_orders_are_ignored():
    result = run([armed_plan()], [working_order(customTag50="", id=7777)])
    assert [action.action for action in result.actions] == [ActionType.PLACE_BRACKET]