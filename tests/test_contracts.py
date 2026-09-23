from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from tradovate_bot.tradovate.contracts import (
    fetch_tick_size,
    normalize_product_root,
    resolve_active_contract,
    tick_size_for,
)


class FakeClient:
    def __init__(self, contracts: list[dict[str, Any]], maturities: dict[int, dict[str, Any]]):
        self.contracts = contracts
        self.maturities = maturities

    def get(self, path: str, **kwargs: Any) -> Any:
        if path == "/contract/suggest":
            return self.contracts
        if path == "/contractMaturity/item":
            maturity_id = kwargs.get("params", {}).get("id")
            return self.maturities[maturity_id]
        raise AssertionError(path)

    def post(self, path: str, json: dict[str, Any]) -> Any:
        return {"contract": {"name": "MESZ6", "contractMaturityId": 2}}


def test_normalize_product_root():
    assert normalize_product_root("MES") == "MES"
    assert normalize_product_root("/mes") == "MES"
    assert normalize_product_root("MESU6") == "MES"


def test_resolve_skips_near_expiry():
    now = datetime.now(UTC)
    near = (now + timedelta(days=5)).isoformat().replace("+00:00", "Z")
    far = (now + timedelta(days=45)).isoformat().replace("+00:00", "Z")
    client = FakeClient(
        contracts=[
            {"name": "MESH6", "contractMaturityId": 1},
            {"name": "MESU6", "contractMaturityId": 2},
        ],
        maturities={
            1: {"expirationDate": near, "isFront": True},
            2: {"expirationDate": far, "isFront": False},
        },
    )
    assert resolve_active_contract(client, "MES", min_days_to_expiry=14) == "MESU6"


def test_tick_size_lookup():
    assert tick_size_for("ES") == 0.25
    assert tick_size_for("/mes") == 0.25
    assert tick_size_for("ZZZ") is None


def test_broker_tick_size_beats_the_static_map():
    class Client:
        def get(self, path: str, **kwargs: Any) -> Any:
            if path == "/contract/find":
                return {"name": "ESZ6", "tickSize": 0.5}
            raise AssertionError(path)

        def post(self, path: str, json: dict[str, Any]) -> Any:
            raise AssertionError(path)

    assert fetch_tick_size(Client(), "ESZ6") == 0.5


def test_tick_size_lookup_never_blocks_a_run():
    class Client:
        def get(self, path: str, **kwargs: Any) -> Any:
            raise RuntimeError("no entitlement")

        def post(self, path: str, json: dict[str, Any]) -> Any:
            raise AssertionError(path)

    assert fetch_tick_size(Client(), "ESZ6") is None
