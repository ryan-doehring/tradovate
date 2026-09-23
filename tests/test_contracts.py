from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from tradovate_bot.tradovate.contracts import (
    normalize_product_root,
    resolve_active_contract,
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
    now = datetime.now(timezone.utc)
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
