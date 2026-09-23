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
    google_sheet_tab: str = "VP"

    default_contracts: int = 1
    # Used when the Closeness Factor cell is blank or negative.
    default_closeness_factor: float = 0.0

    # Flat 10 minutes before red news (cron drift tolerance), re-arm 30 minutes after.
    news_buffer_minutes: int = 10
    news_reopen_minutes: int = 30
    # Drop a news pause that somehow outlived its event, so a bracket never stays muted.
    news_pause_ttl_minutes: int = 120
    # Only scrape the calendar during plausible USD news hours (local time).
    news_scan_start_hour: int = 5
    news_scan_end_hour: int = 17

    # Flat + no working orders inside this many minutes of the session close.
    eod_minutes_before_close: int = 20
    timezone: str = "America/Chicago"
    # Prefer contracts with more than this many days until expiry (roll early).
    min_days_to_expiry: int = 14

    # Price feed: auto | tradovate | sheet | yahoo.
    price_source: str = "auto"
    # Optional live-price cell, e.g. "M1" on the sheet tab (user-maintained).
    sheet_price_cell: str = ""
    # Optional cell holding the quote timestamp (epoch seconds or ISO) for staleness checks.
    sheet_price_time_cell: str = ""
    # Refuse to arm on a quote older than this (fail safe, never guess).
    max_price_age_minutes: int = 15
    # Optional symbol overrides, e.g. "ES:ES=F,MES:MES=F".
    yahoo_price_symbols: str = ""

    tag_prefix: str = "vp"
    legacy_tag_prefixes: str = "vp2026"

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

    @field_validator("price_source", mode="before")
    @classmethod
    def normalize_price_source(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @property
    def writes_enabled(self) -> bool:
        return self.trading_mode in {TradingMode.DEMO, TradingMode.LIVE}

    @property
    def legacy_prefixes(self) -> tuple[str, ...]:
        return tuple(part.strip() for part in self.legacy_tag_prefixes.split(",") if part.strip())

    @property
    def yahoo_symbol_map(self) -> dict[str, str]:
        overrides: dict[str, str] = {}
        for pair in self.yahoo_price_symbols.split(","):
            if ":" not in pair:
                continue
            root, symbol = pair.split(":", 1)
            if root.strip() and symbol.strip():
                overrides[root.strip().upper()] = symbol.strip()
        return overrides

    def is_bot_tag(self, tag: str) -> bool:
        """True for tags this bot owns (current prefix or any legacy prefix)."""
        if not tag:
            return False
        if tag.startswith(f"{self.tag_prefix}:"):
            return True
        return bool(self.legacy_prefixes) and tag.startswith(self.legacy_prefixes)

    def is_legacy_tag(self, tag: str) -> bool:
        return bool(self.legacy_prefixes) and tag.startswith(self.legacy_prefixes)

    def resolve_symbol(self, sheet_ticker: str) -> str:
        """Normalize a sheet ticker to a product root (MES). Front month resolved later."""
        return normalize_product_root(sheet_ticker)


def load_settings() -> Settings:
    return Settings()
