from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any


class Side(str, Enum):
    LONG = "Long"
    SHORT = "Short"

    @property
    def tradovate_action(self) -> str:
        return "Buy" if self is Side.LONG else "Sell"


@dataclass(frozen=True)
class TradePlan:
    """One active row from the VP 2026 sheet."""

    row_number: int
    date_added: date | None
    ticker: str
    side: Side
    quantity: int
    entry: float
    target: float
    stop_loss: float
    tradovate_symbol: str

    @property
    def product_root(self) -> str:
        from tradovate_bot.tradovate.contracts import normalize_product_root

        return normalize_product_root(self.ticker)

    @property
    def match_key(self) -> tuple[str, int, float]:
        # Match on product root so MESU6 vs MES still aligns with sheet MES.
        return (self.product_root, self.quantity, self.entry)

    @property
    def client_tag(self) -> str:
        date_part = self.date_added.isoformat() if self.date_added else f"row{self.row_number}"
        return f"vp2026:{date_part}:{self.product_root}:{self.entry}"


def _parse_side(value: Any) -> Side | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text == "long":
        return Side.LONG
    if text == "short":
        return Side.SHORT
    return None


def _parse_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text or text.startswith("="):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_int(value: Any, default: int) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(str(value).strip())
    except ValueError:
        return default


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def row_is_active(row: list[Any]) -> bool:
    """Active when Outcome is empty. Win / Loss / Miss (or any value) deactivates."""
    if len(row) < 6:
        return False
    outcome = row[5]
    return outcome is None or str(outcome).strip() == ""


def parse_trade_row(
    row_number: int,
    row: list[Any],
    *,
    default_contracts: int,
    resolve_symbol,
) -> TradePlan | None:
    if not row_is_active(row):
        return None

    ticker = row[1]
    side = _parse_side(row[3])
    entry = _parse_float(row[6])
    target = _parse_float(row[8])
    stop_loss = _parse_float(row[9])

    if not ticker or side is None or entry is None or target is None or stop_loss is None:
        return None

    ticker_str = str(ticker).strip()
    # Empty # Contracts → default (1); otherwise use the sheet value.
    quantity = _parse_int(row[4], default_contracts)

    return TradePlan(
        row_number=row_number,
        date_added=_parse_date(row[0]),
        ticker=ticker_str,
        side=side,
        quantity=quantity,
        entry=entry,
        target=target,
        stop_loss=stop_loss,
        tradovate_symbol=resolve_symbol(ticker_str),
    )
