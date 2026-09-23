"""Turn a sheet row plus the live price into a concrete bracket-order plan.

Rules:

* ``Outer`` - the level is the boundary price is beyond: above -> Upper Range,
  below -> Lower Range. Price inside the range gives no level at all.
* ``Mid`` - the level is always Mid Range.
* Side is the opposite of the direction price must travel to reach the level:
  price above the level -> LONG (buy limit below market), price below -> SHORT
  (sell limit above market). Both rest until price returns to the level, so the
  resting side is guaranteed by construction.
* Target / Stop are points measured from the entry level.
* Closeness factor: while price is inside ``[lower - CF, upper + CF]`` the row is not
  armed. Price must leave that band before the retest order is placed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from tradovate_bot.models import EntryType, Side, TradePlan


class Decision(str, Enum):
    ARM = "arm"
    BLOCKED_CLOSENESS = "blocked_closeness"
    NO_SETUP = "no_setup"
    OFF_TICK = "off_tick"
    NO_PRICE = "no_price"
    INVALID = "invalid"


@dataclass(frozen=True)
class EntryPlan:
    """What should happen to one sheet row this run."""

    trade: TradePlan
    decision: Decision
    price: float | None = None
    reason: str = ""
    level: float | None = None
    side: Side | None = None
    entry: float | None = None
    target: float | None = None
    stop_loss: float | None = None

    @property
    def is_armed(self) -> bool:
        return self.decision is Decision.ARM

    def to_dict(self) -> dict:
        return {
            "row": self.trade.row_number,
            "tag": self.trade.client_tag,
            "ticker": self.trade.ticker,
            "symbol": self.trade.tradovate_symbol,
            "entry_type": self.trade.entry_type.label,
            "decision": self.decision.value,
            "reason": self.reason,
            "price": self.price,
            "level": self.level,
            "side": self.side.value if self.side else None,
            "entry": self.entry,
            "target": self.target,
            "stop_loss": self.stop_loss,
            "quantity": self.trade.quantity,
        }


def plan_entry(trade: TradePlan, *, price: float, tick_size: float | None = None) -> EntryPlan:
    """Decide whether and where to place a resting bracket for one sheet row."""
    band_low, band_high = trade.closeness_band
    if band_low <= price <= band_high:
        return EntryPlan(
            trade=trade,
            decision=Decision.BLOCKED_CLOSENESS,
            price=price,
            reason=f"price inside closeness band [{band_low:g}, {band_high:g}]",
        )

    level = _level_for(trade, price)
    if level is None:
        return EntryPlan(
            trade=trade,
            decision=Decision.NO_SETUP,
            price=price,
            reason=f"outer entry needs price outside [{trade.lower:g}, {trade.upper:g}]",
        )

    side = Side.LONG if price > level else Side.SHORT
    entry = level
    if side is Side.LONG:
        target = entry + trade.target_points
        stop_loss = entry - trade.stop_points
    else:
        target = entry - trade.target_points
        stop_loss = entry + trade.stop_points

    if (side is Side.LONG and entry >= price) or (side is Side.SHORT and entry <= price):
        # Never build a limit that would fill instantly: the resting retest is the point.
        return EntryPlan(
            trade=trade,
            decision=Decision.INVALID,
            price=price,
            reason=f"{side.value} limit at {entry:g} would fill immediately",
            level=level,
            side=side,
            entry=entry,
            target=target,
            stop_loss=stop_loss,
        )

    off_tick = _off_tick_values(entry, target, stop_loss, tick_size)
    if off_tick:
        return EntryPlan(
            trade=trade,
            decision=Decision.OFF_TICK,
            price=price,
            reason=f"not on the {tick_size:g} tick grid: {', '.join(off_tick)}",
            level=level,
            side=side,
            entry=entry,
            target=target,
            stop_loss=stop_loss,
        )

    return EntryPlan(
        trade=trade,
        decision=Decision.ARM,
        price=price,
        reason=f"{trade.entry_type.label} retest at {level:g}",
        level=level,
        side=side,
        entry=entry,
        target=target,
        stop_loss=stop_loss,
    )


def plan_without_price(trade: TradePlan, *, reason: str) -> EntryPlan:
    """No usable price this run: report the row but place nothing."""
    return EntryPlan(trade=trade, decision=Decision.NO_PRICE, reason=reason)


def tick_aligned(value: float, tick_size: float | None) -> bool:
    if tick_size is None or tick_size <= 0:
        return True
    steps = value / tick_size
    return abs(steps - round(steps)) < 1e-6


def _level_for(trade: TradePlan, price: float) -> float | None:
    if trade.entry_type is EntryType.MID:
        return trade.mid
    if price > trade.upper:
        return trade.upper
    if price < trade.lower:
        return trade.lower
    return None


def _off_tick_values(
    entry: float,
    target: float,
    stop_loss: float,
    tick_size: float | None,
) -> list[str]:
    if tick_size is None or tick_size <= 0:
        return []
    checked = (("entry", entry), ("target", target), ("stop", stop_loss))
    return [name for name, value in checked if not tick_aligned(value, tick_size)]
