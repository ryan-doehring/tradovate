"""Read the ``VP`` tab and turn active rows into trade plans."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build

from tradovate_bot.models import SHEET_WIDTH, ParsedRow, TradePlan, parse_trade_row

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


@dataclass
class SheetRead:
    """Active trade plans plus every row that was skipped, with the reason."""

    trades: list[TradePlan] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)


def column_letter(width: int) -> str:
    """1 -> A, 12 -> L."""
    if width < 1 or width > 26:
        raise ValueError(f"unsupported column width {width}")
    return chr(ord("A") + width - 1)


class SheetReader:
    def __init__(
        self,
        *,
        service_account_json: str,
        sheet_id: str,
        tab_name: str,
        default_contracts: int,
        default_closeness_factor: float,
        resolve_symbol,
    ) -> None:
        self._service_account_json = service_account_json
        self._sheet_id = sheet_id
        self._tab_name = tab_name
        self._default_contracts = default_contracts
        self._default_closeness_factor = default_closeness_factor
        self._resolve_symbol = resolve_symbol
        self._service: Any = None

    def _sheets(self) -> Any:
        if self._service is None:
            credentials = service_account.Credentials.from_service_account_info(
                json.loads(self._service_account_json),
                scopes=SCOPES,
            )
            self._service = build(
                "sheets",
                "v4",
                credentials=credentials,
                cache_discovery=False,
            )
        return self._service

    def _values(self, a1_range: str) -> list[list[Any]]:
        result = (
            self._sheets()
            .spreadsheets()
            .values()
            .get(spreadsheetId=self._sheet_id, range=a1_range)
            .execute()
        )
        return list(result.get("values", []))

    def fetch_plan(self) -> SheetRead:
        """Row 1 is the header; data starts at row 2."""
        last_column = column_letter(SHEET_WIDTH)
        values = self._values(f"'{self._tab_name}'!A:{last_column}")

        read = SheetRead()
        for index, row in enumerate(values[1:], start=2):
            padded = list(row) + [None] * max(0, SHEET_WIDTH - len(row))
            parsed: ParsedRow = parse_trade_row(
                index,
                padded,
                default_contracts=self._default_contracts,
                default_closeness_factor=self._default_closeness_factor,
                resolve_symbol=self._resolve_symbol,
            )
            if parsed.trade is not None:
                read.trades.append(parsed.trade)
            elif parsed.reason is not None:
                read.skipped.append({"row": index, "reason": parsed.reason.value})
        return read

    def read_cell(self, cell: str) -> Any:
        """Single cell on the configured tab (used by the optional price cell)."""
        rows = self._values(f"'{self._tab_name}'!{cell}")
        if not rows or not rows[0]:
            return None
        return rows[0][0]
