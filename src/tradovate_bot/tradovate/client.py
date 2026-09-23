"""Minimal Tradovate REST client.

Only the free endpoints are used: auth, account/order/position reads and order entry.
Market data (websocket) is deliberately not touched - see :mod:`tradovate_bot.prices`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

WORKING_STATUSES = {"working", "pending", "suspend"}


@dataclass
class TradovateAuth:
    access_token: str
    user_id: int | None = None


class TradovateClient:
    def __init__(self, *, base_url: str, timeout: float = 30.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(base_url=self._base_url, timeout=timeout)
        self._auth: TradovateAuth | None = None

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> TradovateClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def authenticate(
        self,
        *,
        username: str,
        password: str,
        app_id: str,
        cid: int,
        sec: str,
        app_version: str = "0.2.0",
    ) -> TradovateAuth:
        payload = {
            "name": username,
            "password": password,
            "appId": app_id,
            "appVersion": app_version,
            "cid": cid,
            "sec": sec,
        }
        response = self._client.post("/auth/accesstokenrequest", json=payload)
        response.raise_for_status()
        data = response.json()
        token = data.get("accessToken")
        if not token:
            reason = data.get("errorText") or data.get("errorCode") or "no accessToken in response"
            raise RuntimeError(f"Tradovate authentication failed: {reason}")
        self._auth = TradovateAuth(
            access_token=token,
            user_id=data.get("userId"),
        )
        return self._auth

    def _headers(self) -> dict[str, str]:
        if not self._auth:
            raise RuntimeError("TradovateClient is not authenticated")
        return {
            "Authorization": f"Bearer {self._auth.access_token}",
            "Content-Type": "application/json",
        }

    def get(self, path: str, **kwargs: Any) -> Any:
        response = self._client.get(path, headers=self._headers(), **kwargs)
        response.raise_for_status()
        return response.json()

    def post(self, path: str, json: dict[str, Any]) -> Any:
        response = self._client.post(path, headers=self._headers(), json=json)
        response.raise_for_status()
        return response.json()

    # --- accounts -------------------------------------------------------------------
    def get_primary_account(self) -> dict[str, Any]:
        accounts = self.get("/account/list")
        if not accounts:
            raise RuntimeError("No Tradovate accounts found")
        return accounts[0]

    # --- orders and positions -------------------------------------------------------
    def list_orders(self) -> list[dict[str, Any]]:
        data = self.get("/order/list")
        return data if isinstance(data, list) else []

    def list_working_orders(self) -> list[dict[str, Any]]:
        return [order for order in self.list_orders() if is_working(order)]

    def list_positions(self) -> list[dict[str, Any]]:
        data = self.get("/position/list")
        return data if isinstance(data, list) else []

    def net_positions(self) -> dict[str, int]:
        net: dict[str, int] = {}
        for position in self.list_positions():
            symbol = str(position.get("symbol") or "")
            qty = int(position.get("netPos") or 0)
            if symbol and qty:
                net[symbol] = qty
        return net

    def cancel_order(self, order_id: int) -> dict[str, Any]:
        return self.post("/order/cancelorder", {"orderId": order_id})

    def liquidate_position(self, *, account_id: int, symbol: str) -> dict[str, Any]:
        return self.post(
            "/order/liquidateposition",
            {"accountId": account_id, "symbol": symbol},
        )

    def place_bracket_order(
        self,
        *,
        account_spec: str,
        account_id: int,
        symbol: str,
        action: str,
        exit_action: str,
        quantity: int,
        entry: float,
        target: float,
        stop_loss: float,
        custom_tag: str,
        time_in_force: str = "Day",
    ) -> dict[str, Any]:
        """Entry limit with take-profit (bracket1) and stop (bracket2) as an OSO."""
        payload: dict[str, Any] = {
            "accountSpec": account_spec,
            "accountId": account_id,
            "action": action,
            "symbol": symbol,
            "orderQty": quantity,
            "orderType": "Limit",
            "price": entry,
            "timeInForce": time_in_force,
            "isAutomated": True,
            "customTag50": custom_tag[:50],
            "bracket1": {
                "action": exit_action,
                "orderType": "Limit",
                "price": target,
                "timeInForce": time_in_force,
            },
            "bracket2": {
                "action": exit_action,
                "orderType": "Stop",
                "stopPrice": stop_loss,
                "timeInForce": time_in_force,
            },
        }
        return self.post("/order/placeOSO", payload)


def is_working(order: dict[str, Any]) -> bool:
    status = str(order.get("ordStatus") or "").strip().lower()
    if not status:
        # Unknown status: treat as working so we never double-place.
        return True
    terminal = {"filled", "cancelled", "canceled", "rejected", "expired"}
    return status not in terminal