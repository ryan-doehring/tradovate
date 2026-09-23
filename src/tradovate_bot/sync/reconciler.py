from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from tradovate_bot.models import TradePlan


class ActionType(str, Enum):
    PLACE_BRACKET = "place_bracket"
    CANCEL_ORDER = "cancel_order"
    FLATTEN_POSITION = "flatten_position"


@dataclass
class PlannedAction:
    action: ActionType
    reason: str
    trade: TradePlan | None = None
    order_id: int | None = None
    symbol: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


def _order_match_key(order: dict[str, Any]) -> tuple[str, int, float] | None:
    from tradovate_bot.tradovate.contracts import normalize_product_root

    symbol = order.get("symbol")
    qty = order.get("orderQty")
    price = order.get("price")
    if symbol is None or qty is None or price is None:
        return None
    return (normalize_product_root(str(symbol)), int(qty), float(price))


def _is_working_parent(order: dict[str, Any]) -> bool:
    status = str(order.get("ordStatus", "")).lower()
    if status in {"filled", "cancelled", "canceled", "rejected", "expired"}:
        return False
    # Parent entry orders only (brackets are linked separately)
    return order.get("orderType") in {"Limit", "Stop", "Market", "StopLimit"}


def reconcile_trades(
    sheet_trades: list[TradePlan],
    open_orders: list[dict[str, Any]],
    *,
    paused_tags: set[str] | None = None,
) -> list[PlannedAction]:
    """Sheet is source of truth. Match on symbol, quantity, entry price."""
    paused_tags = paused_tags or set()
    actions: list[PlannedAction] = []

    sheet_by_key = {trade.match_key: trade for trade in sheet_trades}
    working_orders = [o for o in open_orders if _is_working_parent(o)]

    open_by_key: dict[tuple[str, int, float], dict[str, Any]] = {}
    for order in working_orders:
        key = _order_match_key(order)
        if key:
            open_by_key[key] = order

    for key, trade in sheet_by_key.items():
        if trade.client_tag in paused_tags:
            continue
        if key not in open_by_key:
            actions.append(
                PlannedAction(
                    action=ActionType.PLACE_BRACKET,
                    reason="missing_open_order",
                    trade=trade,
                    symbol=trade.tradovate_symbol,
                    details={
                        "entry": trade.entry,
                        "target": trade.target,
                        "stop_loss": trade.stop_loss,
                        "quantity": trade.quantity,
                    },
                )
            )

    for key, order in open_by_key.items():
        if key not in sheet_by_key:
            order_id = order.get("id") or order.get("orderId")
            actions.append(
                PlannedAction(
                    action=ActionType.CANCEL_ORDER,
                    reason="not_in_sheet",
                    order_id=int(order_id) if order_id is not None else None,
                    symbol=str(order.get("symbol", "")),
                )
            )

    return actions
