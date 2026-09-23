from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from tradovate_bot.tradovate.contracts import normalize_product_root


class TradingMode(str, Enum):
    DRY_RUN = "dry_run"
    DEMO = "demo"
    LIVE = "live"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    trading_mode: TradingMode = TradingMode.DRY_RUN

    tradovate_username: str = ""
    tradovate_password: str = ""
    tradovate_app_id: str = ""
    tradovate_cid: int = 0
    tradovate_sec: str = ""
    tradovate_api_url: str = "https://demo.tradovateapi.com/v1"

    google_service_account_json: str = ""
    google_sheet_id: str = ""
    google_sheet_tab: str = "VP 2026"

    default_contracts: int = 1
    news_buffer_minutes: int = 5
    news_reopen_minutes: int = 30
    # Drop a news pause that somehow outlived its event (keeps a bracket from
    # staying muted forever if a reopen run is missed).
    news_pause_ttl_minutes: int = 120
    eod_minutes_before_close: int = 5
    timezone: str = "America/Chicago"
    # Prefer contracts with more than this many days until expiry (roll early).
    min_days_to_expiry: int = 14

    @field_validator("trading_mode", mode="before")
    @classmethod
    def normalize_trading_mode(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator("tradovate_cid", mode="before")
    @classmethod
    def empty_cid_to_zero(cls, value: Any) -> Any:
        if value is None or value == "":
            return 0
        return value

    @property
    def writes_enabled(self) -> bool:
        return self.trading_mode in {TradingMode.DEMO, TradingMode.LIVE}

    def resolve_symbol(self, sheet_ticker: str) -> str:
        """Normalize sheet ticker to product root (MES). Front-month resolved later."""
        return normalize_product_root(sheet_ticker)


def load_settings() -> Settings:
    return Settings()
