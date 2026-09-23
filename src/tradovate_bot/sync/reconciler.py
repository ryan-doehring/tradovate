"""Decide what to place and cancel from plans, live orders and row state.

The sheet is the source of truth, but a row that is merely *blocked* this run (inside the
closeness band, no level for an Outer setup, stale price) must never cause a cancel. Only
orders whose row is gone from the sheet, closed, or whose plan changed are cancelled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from tradovate_bot.planner import EntryPlan
from tradovate_bot.state import ROW_OPEN, BotState


class ActionType(str, Enum):
    PLACE_BRACKET = "place_bracket"
    CANCEL_ORDER = "cancel_order"
    FLATTEN_POSITION = "flatten_position"


@dataclass
class PlannedAction:
    action: ActionType
    reason: str
    plan: EntryPlan | None = None
    order_id: int | None = None
    symbol: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReconcileResult:
    actions: list[PlannedAction] = field(default_factory=list)
    notes: list[dict] = field(default_factory=list)

    def note(self, tag: str, note: str, **extra: Any) -> None:
        self.notes.append({"tag": tag, "note": note, **extra})


def order_id_of(order: dict[str, Any]) -> int | None:
    value = order.get("id") or order.get("orderId")
    return int(value) if value is not None else None


def order_tag(order: dict[str, Any]) -> str:
    return str(order.get("customTag50") or "")


def reconcile(
    plans: list[EntryPlan],
    working_orders: list[dict[str, Any]],
    *,
    state: BotState,
    is_bot_tag,
    is_legacy_tag,
    allow_placement: bool = True,
    allow_cancel: bool = True,
    net_positions: dict[str, int] | None = None,
    tick_size: float | None = None,
) -> ReconcileResult:
    result = ReconcileResult()
    net_positions = net_positions or {}

    by_tag: dict[str, list[dict[str, Any]]] = {}
    for order in working_orders:
        tag = order_tag(order)
        if is_bot_tag(tag):
            by_tag.setdefault(tag, []).append(order)

    planned = {plan.trade.client_tag: plan for plan in plans if plan.is_armed}
    # Every row still present in the sheet is "wanted", even when it cannot be armed
    # right now: blocking must never cancel a resting order.
    wanted_tags = {plan.trade.client_tag for plan in plans}

    # 1) Cancel bot orders whose row is gone from the sheet, or that use a legacy tag.
    for tag, orders in by_tag.items():
        if not allow_cancel:
            continue
        if tag not in wanted_tags:
            result.actions.extend(
                _cancel(tag, order, reason="row_not_planned") for order in orders
            )
        elif is_legacy_tag(tag):
            result.actions.extend(
                _cancel(tag, order, reason="legacy_tag_migrated") for order in orders
            )

    # 2) Place for armed rows that have no matching working parent.
    for tag, plan in planned.items():
        # A legacy-tagged order was cancelled in step 1, so it never counts as "already armed".
        existing = [] if is_legacy_tag(tag) else by_tag.get(tag, [])
        matching = _matching_parent(existing, plan, tick_size)
        if matching is not None:
            result.note(tag, "already_armed", order_id=order_id_of(matching))
            continue

        if existing and allow_cancel:
            # A different level/side/qty is working: replace it.
            result.actions.extend(
                _cancel(tag, order, reason="plan_changed") for order in existing
            )
            if not allow_placement:
                continue
        if not allow_placement:
            result.note(tag, "placement_blocked")
            continue

        row = state.row(tag)
        if row.status == ROW_OPEN:
            result.note(tag, "position_open")
            continue

        symbol = plan.trade.tradovate_symbol
        if net_positions.get(symbol, 0) != 0 and row.status != ROW_OPEN:
            # Someone (or a previous run) already holds this contract: don't stack on it.
            result.note(tag, "untracked_position", symbol=symbol)
            continue

        result.actions.append(
            PlannedAction(
                action=ActionType.PLACE_BRACKET,
                reason="armed",
                plan=plan,
                symbol=symbol,
                details={
                    "tag": tag,
                    "side": plan.side.value if plan.side else None,
                    "entry": plan.entry,
                    "target": plan.target,
                    "stop_loss": plan.stop_loss,
                    "quantity": plan.trade.quantity,
                },
            )
        )

    return result


def _cancel(tag: str, order: dict[str, Any], *, reason: str) -> PlannedAction:
    return PlannedAction(
        action=ActionType.CANCEL_ORDER,
        reason=reason,
        order_id=order_id_of(order),
        symbol=str(order.get("symbol") or ""),
        details={"tag": tag},
    )


def _matching_parent(
    orders: list[dict[str, Any]],
    plan: EntryPlan,
    tick_size: float | None,
) -> dict[str, Any] | None:
    """The working order that already implements this plan, if any.

    An OSO puts several orders under one tag, so match on the side we would enter with
    and on the entry price rather than trusting order order.
    """
    wanted_action = plan.side.tradovate_action if plan.side else None
    if wanted_action is None or plan.entry is None:
        return None
    for order in orders:
        if str(order.get("action") or "") != wanted_action:
            continue
        if int(order.get("orderQty") or 0) != plan.trade.quantity:
            continue
        price = order.get("price")
        if price is None:
            continue
        if _same_price(float(price), plan.entry, tick_size):
            return order
    return None


def _same_price(left: float, right: float, tick_size: float | None) -> bool:
    tolerance = (tick_size or 0.01) / 2
    return abs(left - right) < tolerance
