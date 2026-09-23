from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


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
        app_version: str = "0.1.0",
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
        self._auth = TradovateAuth(access_token=data["accessToken"], user_id=data.get("userId"))
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

    def suggest_contracts(self, text: str) -> list[dict[str, Any]]:
        data = self.get("/contract/suggest", params={"t": text})
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return list(data.get("contracts") or data.get("items") or [])
        return []

    def roll_contract(self, name: str) -> dict[str, Any]:
        return self.post(
            "/contract/rollcontract",
            {"name": name, "forward": True, "ifExpired": True},
        )

    def post(self, path: str, json: dict[str, Any]) -> Any:
        response = self._client.post(path, headers=self._headers(), json=json)
        response.raise_for_status()
        return response.json()

    def list_working_orders(self) -> list[dict[str, Any]]:
        return self.get("/order/list")

    def list_positions(self) -> list[dict[str, Any]]:
        return self.get("/position/list")

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
        trade,
        custom_tag: str,
        expire_time: str | None = None,
    ) -> dict[str, Any]:
        """Place entry limit with TP/SL via OSO (bracket1=TP, bracket2=SL as OCO)."""
        exit_action = "Sell" if trade.side.tradovate_action == "Buy" else "Buy"
        payload: dict[str, Any] = {
            "accountSpec": account_spec,
            "accountId": account_id,
            "action": trade.side.tradovate_action,
            "symbol": trade.tradovate_symbol,
            "orderQty": trade.quantity,
            "orderType": "Limit",
            "price": trade.entry,
            "timeInForce": "GTC",
            "isAutomated": True,
            "customTag50": custom_tag[:50],
            "bracket1": {
                "action": exit_action,
                "orderType": "Limit",
                "price": trade.target,
                "timeInForce": "GTC",
            },
            "bracket2": {
                "action": exit_action,
                "orderType": "Stop",
                "stopPrice": trade.stop_loss,
                "timeInForce": "GTC",
            },
        }
        if expire_time:
            payload["expireTime"] = expire_time
        return self.post("/order/placeOSO", payload)

    def get_primary_account(self) -> dict[str, Any]:
        accounts = self.get("/account/list")
        if not accounts:
            raise RuntimeError("No Tradovate accounts found")
        return accounts[0]
