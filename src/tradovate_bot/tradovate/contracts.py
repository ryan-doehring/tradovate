from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx

# CME Globex product IDs (from product-slate). Used for discovery only.
CME_PRODUCT_IDS: dict[str, int] = {
    "MES": 8667,
    "MNQ": 8668,
    "ES": 133,
    "NQ": 146,
}

# CME tick sizes (in points) used to validate sheet levels/points before ordering.
# The broker-reported ``tickSize`` wins when available; this is the offline fallback.
TICK_SIZES: dict[str, float] = {
    "ES": 0.25,
    "MES": 0.25,
    "NQ": 0.25,
    "MNQ": 0.25,
    "YM": 1.0,
    "MYM": 1.0,
    "RTY": 0.1,
    "M2K": 0.1,
    "GC": 0.1,
    "MGC": 0.1,
    "CL": 0.01,
    "MCL": 0.01,
}

# Futures month codes: F G H J K M N Q U V X Z
_MONTH_CODE = r"[FGHJKMNQUVXZ]"
_FULL_CONTRACT = re.compile(rf"^([A-Z0-9]+)({_MONTH_CODE}\d{{1,2}})$")


class SupportsContractLookup(Protocol):
    def get(self, path: str, **kwargs: Any) -> Any: ...
    def post(self, path: str, json: dict[str, Any]) -> Any: ...


def normalize_product_root(ticker: str) -> str:
    """Sheet ticker → product root. MES, /MES, mesu6 → MES."""
    cleaned = ticker.strip().upper().lstrip("/@")
    match = _FULL_CONTRACT.match(cleaned)
    if match:
        return match.group(1)
    return cleaned


def is_full_contract_symbol(symbol: str) -> bool:
    return bool(_FULL_CONTRACT.match(symbol.strip().upper().lstrip("/@")))


def extract_full_contract_if_present(ticker: str) -> str | None:
    cleaned = ticker.strip().upper().lstrip("/@")
    if is_full_contract_symbol(cleaned):
        return cleaned
    return None


def resolve_active_contract(
    client: SupportsContractLookup,
    product_root: str,
    *,
    min_days_to_expiry: int = 14,
) -> str:
    """Pick the most appropriate maturity for a product root (e.g. MES → MESU6).

    Strategy (free, no CME volume endpoint required):
    1. Use Tradovate ``/contract/suggest`` (returns maturities ordered best-first).
    2. Prefer the first contract with ``> min_days_to_expiry`` days left
       (avoids stale/low-liquidity front months near roll).
    3. Fall back to ``/contract/rollcontract``.
    4. Fall back to the first suggested contract.

    Note: CME's old volume-by-contract HTTP APIs are no longer available.
    Tradovate's suggest ordering is the practical free proxy for \"most active\"
    for equity index micros; near-roll we skip contracts within 14 days of expiry.
    """
    root = normalize_product_root(product_root)

    suggested = client.get("/contract/suggest", params={"t": root})
    if isinstance(suggested, dict):
        suggested = suggested.get("contracts") or suggested.get("items") or []

    candidates: list[dict[str, Any]] = list(suggested or [])
    if not candidates:
        rolled = client.post(
            "/contract/rollcontract",
            {"name": root, "forward": True, "ifExpired": True},
        )
        contract = (rolled or {}).get("contract") or {}
        name = contract.get("name")
        if name:
            return str(name)
        raise RuntimeError(f"No contracts found for product root {root!r}")

    now = datetime.now(UTC)
    for candidate in candidates:
        name = candidate.get("name")
        maturity_id = candidate.get("contractMaturityId")
        if not name:
            continue
        if maturity_id is None:
            return str(name)
        try:
            maturity = client.get("/contractMaturity/item", params={"id": maturity_id})
        except Exception:  # noqa: BLE001
            return str(name)
        expiration = maturity.get("expirationDate")
        if not expiration:
            if maturity.get("isFront"):
                return str(name)
            continue
        exp_dt = datetime.fromisoformat(str(expiration).replace("Z", "+00:00"))
        if exp_dt - now > timedelta(days=min_days_to_expiry):
            return str(name)

    # Last resort: first suggestion or rollcontract
    first_name = candidates[0].get("name")
    if first_name:
        return str(first_name)

    rolled = client.post(
        "/contract/rollcontract",
        {"name": root, "forward": True, "ifExpired": True},
    )
    name = ((rolled or {}).get("contract") or {}).get("name")
    if not name:
        raise RuntimeError(f"Unable to resolve active contract for {root!r}")
    return str(name)


def resolve_trade_symbol(client: SupportsContractLookup | None, sheet_ticker: str) -> str:
    """Resolve a sheet ticker to a tradeable Tradovate contract symbol."""
    full = extract_full_contract_if_present(sheet_ticker)
    if full:
        return full

    root = normalize_product_root(sheet_ticker)
    if client is None:
        return root
    return resolve_active_contract(client, root)


def tick_size_for(product_root: str) -> float | None:
    """Offline fallback tick size for a product root (broker value wins when available)."""
    return TICK_SIZES.get(normalize_product_root(product_root))


def fetch_tick_size(client: SupportsContractLookup, symbol: str) -> float | None:
    """Broker-reported tick size for a resolved contract symbol, when available."""
    try:
        data = client.get("/contract/find", params={"name": symbol})
    except Exception:  # noqa: BLE001 - tick size is advisory, never block a run
        return None
    contract = data
    if isinstance(data, list):
        contract = data[0] if data else None
    if not isinstance(contract, dict):
        return None
    try:
        return float(contract["tickSize"])
    except (KeyError, TypeError, ValueError):
        return None


def try_cme_product_id(product_root: str) -> int | None:
    """Lookup known CME product IDs (for future volume integrations / debugging)."""
    return CME_PRODUCT_IDS.get(normalize_product_root(product_root))


def fetch_cme_product_slate_id(product_root: str) -> int | None:
    """Optional free CME product-slate lookup (product level, not contract month volume)."""
    root = normalize_product_root(product_root)
    try:
        response = httpx.get(
            "https://www.cmegroup.com/services/product-slate",
            params={
                "sortAsc": "false",
                "sortField": "vol",
                "pageNumber": 1,
                "venues": 3,
                "cleared": "Futures",
                "pageSize": 100,
            },
            headers={"User-Agent": "tradovate-bot/0.1", "Accept": "application/json"},
            timeout=20.0,
        )
        response.raise_for_status()
        for product in response.json().get("products", []):
            code = (product.get("globex") or product.get("prodCode") or "").upper()
            if code == root:
                return int(product["id"])
    except Exception:  # noqa: BLE001
        return try_cme_product_id(root)
    return try_cme_product_id(root)
