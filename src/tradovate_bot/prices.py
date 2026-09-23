"""Live price lookup used to decide whether a row may be armed.

Tradovate market data is websocket-only and gated behind a paid add-on, so the default
chain is: broker REST probe (free if it works) -> a sheet cell you maintain -> Yahoo's
public chart endpoint. Every quote carries a timestamp so callers can refuse to act on
stale data instead of guessing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
USER_AGENT = "tradovate-bot/0.2 (+https://github.com/ryan-doehring/tradovate)"

DEFAULT_YAHOO_SYMBOLS: dict[str, str] = {
    "ES": "ES=F",
    "MES": "MES=F",
    "NQ": "NQ=F",
    "MNQ": "MNQ=F",
    "YM": "YM=F",
    "MYM": "MYM=F",
    "RTY": "RTY=F",
    "M2K": "M2K=F",
    "GC": "GC=F",
    "MGC": "MGC=F",
    "CL": "CL=F",
    "MCL": "MCL=F",
}


class PriceError(RuntimeError):
    """Raised when no usable price could be obtained."""


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: float
    as_of: datetime
    source: str

    def age_minutes(self, now: datetime | None = None) -> float:
        reference = now or datetime.now(UTC)
        return (reference - self.as_of).total_seconds() / 60.0

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "price": self.price,
            "as_of": self.as_of.isoformat(),
            "source": self.source,
        }


class PriceProvider(Protocol):
    name: str

    def get_quote(self, *, root: str, symbol: str) -> Quote: ...


class YahooPriceProvider:
    """Public Yahoo Finance chart endpoint (free, unauthenticated, ~10 min delayed)."""

    name = "yahoo"

    def __init__(
        self,
        *,
        symbols: dict[str, str] | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._symbols = {**DEFAULT_YAHOO_SYMBOLS, **(symbols or {})}
        self._timeout = timeout

    def get_quote(self, *, root: str, symbol: str) -> Quote:
        yahoo_symbol = self._symbols.get(root.upper()) or f"{root.upper()}=F"
        url = YAHOO_URL.format(symbol=yahoo_symbol)
        try:
            response = httpx.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=self._timeout,
                follow_redirects=True,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PriceError(f"yahoo request failed for {yahoo_symbol}: {exc}") from exc

        meta = _first_chart_meta(payload)
        price = meta.get("regularMarketPrice")
        if price is None:
            raise PriceError(f"yahoo returned no price for {yahoo_symbol}")
        stamp = meta.get("regularMarketTime")
        as_of = _from_epoch(stamp) or datetime.now(UTC)
        return Quote(symbol=yahoo_symbol, price=float(price), as_of=as_of, source=self.name)


class SheetPriceProvider:
    """Reads a price cell you maintain on the sheet (any formula/source you like)."""

    name = "sheet"

    def __init__(
        self,
        *,
        read_cell: Callable[[str], Any],
        cell: str,
        time_cell: str = "",
    ) -> None:
        self._read_cell = read_cell
        self._cell = cell
        self._time_cell = time_cell

    def get_quote(self, *, root: str, symbol: str) -> Quote:
        try:
            raw = self._read_cell(self._cell)
        except Exception as exc:  # noqa: BLE001 - surface as PriceError for the chain
            raise PriceError(f"sheet price cell {self._cell} unreadable: {exc}") from exc
        price = _to_float(raw)
        if price is None:
            raise PriceError(f"sheet price cell {self._cell} has no numeric value")

        as_of = datetime.now(UTC)
        if self._time_cell:
            stamp = _to_float(self._read_cell(self._time_cell))
            parsed = _from_epoch(stamp)
            if parsed is not None:
                as_of = parsed
        return Quote(symbol=symbol or root, price=price, as_of=as_of, source=self.name)


class TradovatePriceProvider:
    """Free REST probe: /quote/list sometimes returns data without the paid add-on."""

    name = "tradovate"

    def __init__(self, client: Any) -> None:
        self._client = client

    def get_quote(self, *, root: str, symbol: str) -> Quote:
        try:
            data = self._client.get("/quote/list", params={"symbol": symbol})
        except Exception as exc:  # noqa: BLE001
            raise PriceError(f"tradovate quote probe failed: {exc}") from exc

        quote = _first_quote(data)
        price = _quote_price(quote)
        if price is None:
            raise PriceError("tradovate /quote/list returned no usable price")
        as_of = _from_iso(quote.get("timestamp")) or datetime.now(UTC)
        return Quote(symbol=symbol, price=price, as_of=as_of, source=self.name)


PROVIDER_ORDER: dict[str, tuple[str, ...]] = {
    "auto": ("tradovate", "sheet", "yahoo"),
    "tradovate": ("tradovate",),
    "sheet": ("sheet",),
    "yahoo": ("yahoo",),
}


class QuoteCache:
    """One quote per product root per run."""

    def __init__(self) -> None:
        self._quotes: dict[str, Quote] = {}

    def get(self, root: str) -> Quote | None:
        return self._quotes.get(root.upper())

    def put(self, root: str, quote: Quote) -> None:
        self._quotes[root.upper()] = quote


def resolve_quote(
    *,
    root: str,
    symbol: str,
    source: str = "auto",
    client: Any | None = None,
    read_cell: Callable[[str], Any] | None = None,
    sheet_cell: str = "",
    sheet_time_cell: str = "",
    yahoo_symbols: dict[str, str] | None = None,
) -> Quote:
    """Return the first usable quote from the configured provider chain."""
    errors: list[str] = []
    for name in PROVIDER_ORDER.get(source, PROVIDER_ORDER["auto"]):
        if name == "tradovate" and client is None:
            continue
        if name == "sheet" and not (read_cell and sheet_cell):
            continue
        provider: PriceProvider
        if name == "tradovate":
            provider = TradovatePriceProvider(client)
        elif name == "sheet":
            provider = SheetPriceProvider(
                read_cell=read_cell,
                cell=sheet_cell,
                time_cell=sheet_time_cell,
            )
        else:
            provider = YahooPriceProvider(symbols=yahoo_symbols)
        try:
            return provider.get_quote(root=root, symbol=symbol)
        except PriceError as exc:
            errors.append(str(exc))
    raise PriceError("; ".join(errors) or "no price provider available")


def freshness_reason(
    quote: Quote,
    *,
    max_age_minutes: int,
    now: datetime | None = None,
) -> str | None:
    """Reason the quote is too old to act on, or None when it is fresh enough."""
    if max_age_minutes <= 0:
        return None
    age = quote.age_minutes(now)
    if age <= max_age_minutes:
        return None
    return f"quote from {quote.source} is {age:.1f} min old (limit {max_age_minutes} min)"


def _first_chart_meta(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise PriceError("yahoo payload was not an object")
    results = (payload.get("chart") or {}).get("result") or []
    if not results:
        raise PriceError("yahoo returned no chart result")
    meta = results[0].get("meta")
    if not isinstance(meta, dict):
        raise PriceError("yahoo result had no meta block")
    return meta


def _first_quote(data: Any) -> dict:
    if isinstance(data, dict):
        data = data.get("quotes") or data.get("items") or []
    if not isinstance(data, list) or not data:
        raise PriceError("quote list was empty")
    quote = data[0]
    if not isinstance(quote, dict):
        raise PriceError("quote entry was not an object")
    return quote


def _quote_price(quote: dict) -> float | None:
    entries = quote.get("entries")
    if isinstance(entries, dict):
        for key in ("Trade", "Bid", "Offer", "SettlementPrice", "OpeningPrice"):
            entry = entries.get(key)
            if isinstance(entry, dict):
                price = _to_float(entry.get("price"))
                if price is not None:
                    return price
    for key in ("price", "last", "lastPrice"):
        price = _to_float(quote.get(key))
        if price is not None:
            return price
    return None


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _from_epoch(value: Any) -> datetime | None:
    seconds = _to_float(value)
    if seconds is None or seconds <= 0:
        return None
    return datetime.fromtimestamp(seconds, tz=UTC)


def _from_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed
