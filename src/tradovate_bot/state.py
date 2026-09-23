"""Run-to-run state: news pauses, per-row order lifecycle, and the news cache."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROW_IDLE = "idle"
ROW_ARMED = "armed"
ROW_OPEN = "open"
ROW_FLAT = "flat"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _parse_dt(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


@dataclass
class RowState:
    """Lifecycle of one sheet row's order, keyed by its client tag."""

    tag: str
    status: str = ROW_IDLE
    order_id: int | None = None
    symbol: str = ""
    side: str = ""
    entry: float | None = None
    target: float | None = None
    stop_loss: float | None = None
    quantity: int | None = None
    updated_at: datetime | None = None

    @property
    def holds_order(self) -> bool:
        return self.status in {ROW_ARMED, ROW_OPEN}

    def to_dict(self) -> dict:
        return {
            "tag": self.tag,
            "status": self.status,
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side,
            "entry": self.entry,
            "target": self.target,
            "stop_loss": self.stop_loss,
            "quantity": self.quantity,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    @classmethod
    def from_dict(cls, data: object) -> RowState | None:
        if not isinstance(data, dict) or not data.get("tag"):
            return None
        order_id = data.get("order_id")
        return cls(
            tag=str(data["tag"]),
            status=str(data.get("status") or ROW_IDLE),
            order_id=int(order_id) if isinstance(order_id, (int, float)) else None,
            symbol=str(data.get("symbol") or ""),
            side=str(data.get("side") or ""),
            entry=_as_float(data.get("entry")),
            target=_as_float(data.get("target")),
            stop_loss=_as_float(data.get("stop_loss")),
            quantity=int(data["quantity"]) if isinstance(data.get("quantity"), int) else None,
            updated_at=_parse_dt(data.get("updated_at")),
        )


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class BotState:
    """Everything that has to survive between runs."""

    rows: dict[str, RowState] = field(default_factory=dict)
    news_events: list[dict] = field(default_factory=list)
    news_fetched_at: datetime | None = None

    # --- news cache (avoids scraping the calendar on every tick) -------------------
    def store_news(self, events: list[dict], *, now: datetime | None = None) -> None:
        self.news_events = list(events)
        self.news_fetched_at = now or _utc_now()

    def cached_news(self, now: datetime, *, max_age_minutes: int) -> list[dict] | None:
        if self.news_fetched_at is None or not self.news_events:
            return None
        if now - self.news_fetched_at > timedelta(minutes=max_age_minutes):
            return None
        return list(self.news_events)

    # --- row lifecycle -------------------------------------------------------------
    def row(self, tag: str) -> RowState:
        if tag not in self.rows:
            self.rows[tag] = RowState(tag=tag)
        return self.rows[tag]

    def mark_armed(
        self,
        tag: str,
        *,
        order_id: int | None,
        symbol: str,
        side: str,
        entry: float | None,
        target: float | None,
        stop_loss: float | None,
        quantity: int | None,
        now: datetime | None = None,
    ) -> None:
        row = self.row(tag)
        row.status = ROW_ARMED
        row.order_id = order_id
        row.symbol = symbol
        row.side = side
        row.entry = entry
        row.target = target
        row.stop_loss = stop_loss
        row.quantity = quantity
        row.updated_at = now or _utc_now()

    def mark_open(self, tag: str, *, now: datetime | None = None) -> None:
        row = self.row(tag)
        row.status = ROW_OPEN
        row.updated_at = now or _utc_now()

    def mark_flat(self, tag: str, *, now: datetime | None = None) -> None:
        row = self.row(tag)
        row.status = ROW_FLAT
        row.order_id = None
        row.updated_at = now or _utc_now()

    def mark_idle(self, tag: str, *, now: datetime | None = None) -> None:
        row = self.row(tag)
        row.status = ROW_IDLE
        row.order_id = None
        row.updated_at = now or _utc_now()

    def forget(self, tag: str) -> None:
        self.rows.pop(tag, None)

    def tracked_orders(self) -> dict[int, RowState]:
        """Order id -> row state, for rows we believe currently hold an order."""
        tracked: dict[int, RowState] = {}
        for row in self.rows.values():
            if row.order_id is not None and row.holds_order:
                tracked[row.order_id] = row
        return tracked

    # --- serialisation -------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "rows": {tag: row.to_dict() for tag, row in self.rows.items()},
            "news_events": self.news_events,
            "news_fetched_at": self.news_fetched_at.isoformat() if self.news_fetched_at else None,
            "updated_at": _utc_now().isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> BotState:
        rows: dict[str, RowState] = {}
        for tag, raw in (data.get("rows") or {}).items():
            row = RowState.from_dict(raw)
            if row is not None:
                rows[str(tag)] = row

        events = [event for event in (data.get("news_events") or []) if isinstance(event, dict)]
        return cls(
            rows=rows,
            news_events=events,
            news_fetched_at=_parse_dt(data.get("news_fetched_at")),
        )


def load_state(path: Path) -> BotState:
    if not path.exists():
        return BotState()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # A corrupt state file must not block trading: start clean and let the
        # reconciler re-place whatever the sheet still wants.
        return BotState()
    if not isinstance(data, dict):
        return BotState()
    return BotState.from_dict(data)


def save_state(path: Path, state: BotState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")
