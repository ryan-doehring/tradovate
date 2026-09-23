from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from tradovate_bot.prices import (
    PriceError,
    Quote,
    SheetPriceProvider,
    YahooPriceProvider,
    freshness_reason,
    resolve_quote,
)

YAHOO_PAYLOAD = {
    "chart": {
        "result": [
            {
                "meta": {
                    "symbol": "ES=F",
                    "instrumentType": "FUTURE",
                    "regularMarketPrice": 7712.5,
                    "regularMarketTime": 1789765198,
                }
            }
        ],
        "error": None,
    }
}


def _boom(*args, **kwargs):
    raise httpx.ConnectError("boom")


def quote(age_minutes: float, *, source: str = "yahoo") -> Quote:
    return Quote(
        symbol="ES=F",
        price=100.0,
        as_of=datetime.now(UTC) - timedelta(minutes=age_minutes),
        source=source,
    )


def test_freshness_reason_flags_stale_quotes():
    assert freshness_reason(quote(5), max_age_minutes=15) is None
    reason = freshness_reason(quote(42), max_age_minutes=15)
    assert reason is not None
    assert "42" in reason
    assert freshness_reason(quote(999), max_age_minutes=0) is None


def test_yahoo_metadata_is_parsed(monkeypatch):
    class Response:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return YAHOO_PAYLOAD

    monkeypatch.setattr("tradovate_bot.prices.httpx.get", lambda *a, **k: Response())
    result = YahooPriceProvider().get_quote(root="ES", symbol="ESZ6")
    assert result.price == 7712.5
    assert result.source == "yahoo"
    assert result.symbol == "ES=F"
    assert result.as_of.year == 2026


def test_yahoo_failure_raises_price_error(monkeypatch):
    monkeypatch.setattr("tradovate_bot.prices.httpx.get", _boom)
    with pytest.raises(PriceError):
        YahooPriceProvider().get_quote(root="ES", symbol="ESZ6")


def test_sheet_cell_provider_reads_a_maintained_cell():
    cells = {"M1": "7712.50", "M2": 1789765198}
    provider = SheetPriceProvider(read_cell=cells.get, cell="M1", time_cell="M2")
    result = provider.get_quote(root="ES", symbol="ESZ6")
    assert result.price == 7712.5
    assert result.source == "sheet"


def test_sheet_cell_provider_rejects_junk():
    provider = SheetPriceProvider(read_cell=lambda cell: "#N/A", cell="M1")
    with pytest.raises(PriceError):
        provider.get_quote(root="ES", symbol="ESZ6")


def test_resolve_quote_falls_back_to_the_sheet_cell(monkeypatch):
    monkeypatch.setattr("tradovate_bot.prices.httpx.get", _boom)
    result = resolve_quote(
        root="ES",
        symbol="ESZ6",
        source="auto",
        read_cell=lambda cell: 7700.0,
        sheet_cell="M1",
    )
    assert result.source == "sheet"
    assert result.price == 7700.0


def test_resolve_quote_reports_every_failure(monkeypatch):
    monkeypatch.setattr("tradovate_bot.prices.httpx.get", _boom)
    with pytest.raises(PriceError) as excinfo:
        resolve_quote(root="ES", symbol="ESZ6", source="auto")
    assert "yahoo" in str(excinfo.value)