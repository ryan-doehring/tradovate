from __future__ import annotations

import json
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build

from tradovate_bot.models import TradePlan, parse_trade_row


class SheetReader:
    def __init__(
        self,
        *,
        service_account_json: str,
        sheet_id: str,
        tab_name: str,
        default_contracts: int,
        resolve_symbol,
    ) -> None:
        self._sheet_id = sheet_id
        self._tab_name = tab_name
        self._default_contracts = default_contracts
        self._resolve_symbol = resolve_symbol
        self._credentials = service_account.Credentials.from_service_account_info(
            json.loads(service_account_json),
            scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
        )

    def fetch_active_trades(self) -> list[TradePlan]:
        service = build("sheets", "v4", credentials=self._credentials, cache_discovery=False)
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=self._sheet_id, range=f"'{self._tab_name}'!A:Z")
            .execute()
        )
        values: list[list[Any]] = result.get("values", [])
        if not values:
            return []

        trades: list[TradePlan] = []
        for idx, row in enumerate(values[1:], start=2):
            padded = row + [None] * max(0, 10 - len(row))
            trade = parse_trade_row(
                idx,
                padded,
                default_contracts=self._default_contracts,
                resolve_symbol=self._resolve_symbol,
            )
            if trade:
                trades.append(trade)
        return trades
