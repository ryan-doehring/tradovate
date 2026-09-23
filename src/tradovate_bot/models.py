"""Google Sheet (``VP`` tab) row model.

Column layout (letters -> 0-based indexes):

    A Date Added | B Ticker | C Type | D Upper Range | E Lower Range | F Mid Range
    G Entry Type | H # Contracts | I Outcome | J Target Profit | K SL | L Closeness Factor

``Type`` (C) is a human descriptor and is not used for decisions. Direction and the entry
price are deliberately *not* in the sheet: they are derived from the live price at plan
time (see :mod:`tradovate_bot.planner`). A row is active only while Outcome is empty.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from enum import Enum
from typing import Any

COL_DATE_ADDED = 0
COL_TICKER = 1
COL_TYPE = 2
COL_UPPER = 3
COL_LOWER = 4
COL_MID = 5
COL_ENTRY_TYPE = 6
COL_CONTRACTS = 7
COL_OUTCOME = 8
COL_TARGET_POINTS = 9
COL_STOP_POINTS = 10
COL_CLOSENESS = 11

SHEET_WIDTH = 12


class Side(str, Enum):
    LONG = "Long"
    SHORT = "Short"

    @property
    def tradovate_action(self) -> str:
        return "Buy" if self is Side.LONG else "Sell"

    @property
    def exit_action(self) -> str:
        return "Sell" if self is Side.LONG else "Buy"


class EntryType(str, Enum):
    OUTER = "outer"
    MID = "mid"

    @classmethod
    def parse(cls, value: Any) -> EntryType | None:
        if value is None:
            return None
        text = str(value).strip().lower()
        if text in {"outer", "out"}:
            return cls.OUTER
        if text in {"mid", "middle"}:
            return cls.MID
        return None

    @property
    def label(self) -> str:
        return "Outer" if self is EntryType.OUTER else "Mid"


class SkipReason(str, Enum):
    """Why a sheet row produced no trade plan."""

    OUTCOME_SET = "outcome_set"
    MISSING_TICKER = "missing_ticker"
    INVALID_ENTRY_TYPE = "invalid_entry_type"
    MISSING_RANGE = "missing_range"
    INVALID_RANGE = "invalid_range"
    MID_OUTSIDE_RANGE = "mid_outside_range"
    MISSING_TARGET = "missing_target"
    MISSING_STOP = "missing_stop"
    NON_POSITIVE_POINTS = "non_positive_points"


@dataclass(frozen=True)
class TradePlan:
    """One active sheet row, exactly as typed in the sheet."""

    row_number: int
    date_added: date | None
    ticker: str
    setup_type: str
    upper: float
    lower: float
    mid: float
    entry_type: EntryType
    quantity: int
    target_points: float
    stop_points: float
    closeness_factor: float
    tradovate_symbol: str

    @property
    def product_root(self) -> str:
        from tradovate_bot.tradovate.contracts import normalize_product_root

        return normalize_product_root(self.ticker)

    @property
    def closeness_band(self) -> tuple[float, float]:
        """Price band in which this row must not be armed: [lower - CF, upper + CF]."""
        return (self.lower - self.closeness_factor, self.upper + self.closeness_factor)

    @property
    def client_tag(self) -> str:
        """Stable broker tag (``customTag50``, max 50 chars) identifying this row."""
        return f"vp:r{self.row_number}:{self.product_root}"

    def with_symbol(self, symbol: str) -> TradePlan:
        return replace(self, tradovate_symbol=symbol)


@dataclass(frozen=True)
class ParsedRow:
    """Result of parsing one sheet row: either a trade or a reason it was skipped."""

    row_number: int
    trade: TradePlan | None = None
    reason: SkipReason | None = None

    @property
    def is_trade(self) -> bool:
        return self.trade is not None


def row_is_active(row: list[Any]) -> bool:
    """Active while Outcome is empty. Any value (Win / Loss / Miss / ...) deactivates."""
    if len(row) <= COL_OUTCOME:
        return False
    outcome = row[COL_OUTCOME]
    return outcome is None or str(outcome).strip() == ""


def parse_trade_row(
    row_number: int,
    row: list[Any],
    *,
    default_contracts: int,
    default_closeness_factor: float,
    resolve_symbol,
) -> ParsedRow:
    if not row_is_active(row):
        return ParsedRow(row_number, reason=SkipReason.OUTCOME_SET)

    ticker = _text(row[COL_TICKER])
    if not ticker:
        return ParsedRow(row_number, reason=SkipReason.MISSING_TICKER)

    entry_type = EntryType.parse(row[COL_ENTRY_TYPE])
    if entry_type is None:
        return ParsedRow(row_number, reason=SkipReason.INVALID_ENTRY_TYPE)

    upper = _parse_float(row[COL_UPPER])
    lower = _parse_float(row[COL_LOWER])
    if upper is None or lower is None:
        return ParsedRow(row_number, reason=SkipReason.MISSING_RANGE)
    if upper <= lower:
        return ParsedRow(row_number, reason=SkipReason.INVALID_RANGE)

    mid = _parse_float(row[COL_MID])
    if mid is None:
        mid = (upper + lower) / 2
    if not lower <= mid <= upper:
        return ParsedRow(row_number, reason=SkipReason.MID_OUTSIDE_RANGE)

    target_points = _parse_float(row[COL_TARGET_POINTS])
    if target_points is None:
        return ParsedRow(row_number, reason=SkipReason.MISSING_TARGET)
    stop_points = _parse_float(row[COL_STOP_POINTS])
    if stop_points is None:
        return ParsedRow(row_number, reason=SkipReason.MISSING_STOP)
    if target_points <= 0 or stop_points <= 0:
        return ParsedRow(row_number, reason=SkipReason.NON_POSITIVE_POINTS)

    closeness = _parse_float(row[COL_CLOSENESS])
    if closeness is None or closeness < 0:
        closeness = default_closeness_factor

    return ParsedRow(
        row_number,
        trade=TradePlan(
            row_number=row_number,
            date_added=_parse_date(row[COL_DATE_ADDED]),
            ticker=ticker,
            setup_type=_text(row[COL_TYPE]),
            upper=upper,
            lower=lower,
            mid=mid,
            entry_type=entry_type,
            quantity=_parse_int(row[COL_CONTRACTS], default_contracts),
            target_points=target_points,
            stop_points=stop_points,
            closeness_factor=closeness,
            tradovate_symbol=resolve_symbol(ticker),
        ),
    )


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


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
