from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

FOREX_FACTORY_URL = "https://www.forexfactory.com/calendar"


class NewsScrapeError(RuntimeError):
    """Raised when the Forex Factory calendar cannot be fetched or parsed.

    The workflow treats this as a hard failure on purpose: a silent empty result
    would look like "no news today" and skip the blackout flatten entirely.
    The raw page is carried along so it can be uploaded as a debug artifact.
    """

    def __init__(self, detail: str, *, html: str = "", status_code: int | None = None) -> None:
        super().__init__(f"Forex Factory calendar scrape failed: {detail}")
        self.detail = detail
        self.html = html
        self.status_code = status_code



@dataclass(frozen=True)
class NewsEvent:
    event_id: str
    title: str
    currency: str
    impact: str
    event_time: datetime

    def blackout_start(self, buffer_minutes: int = 5) -> datetime:
        return self.event_time - timedelta(minutes=buffer_minutes)

    def reopen_time(self, reopen_minutes: int = 30) -> datetime:
        return self.event_time + timedelta(minutes=reopen_minutes)

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "title": self.title,
            "currency": self.currency,
            "impact": self.impact,
            "event_time": self.event_time.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> NewsEvent | None:
        try:
            event_time = datetime.fromisoformat(str(data["event_time"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            return None
        if event_time.tzinfo is None:
            event_time = event_time.replace(tzinfo=ZoneInfo("UTC"))
        return cls(
            event_id=str(data.get("event_id") or ""),
            title=str(data.get("title") or ""),
            currency=str(data.get("currency") or ""),
            impact=str(data.get("impact") or ""),
            event_time=event_time,
        )


def _parse_event_time(day_label: str, time_label: str, tz: ZoneInfo) -> datetime | None:
    time_label = time_label.strip()
    if not time_label or time_label.lower() in {"all day", "tentative", "day"}:
        return None
    try:
        combined = f"{day_label} {time_label}"
        parsed = date_parser.parse(combined, fuzzy=True)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tz)
        return parsed.astimezone(ZoneInfo("UTC"))
    except (ValueError, OverflowError):
        return None


def _impact_is_high(impact_cell: Any) -> bool:
    """Forex Factory flags impact on a ``<span>`` inside the impact cell.

    Check the icon title, its CSS classes and the visible text so a change to any
    single one of them cannot silently drop red (high impact) events.
    """
    signals = [impact_cell.get_text(" ", strip=True)]
    icon = impact_cell.select_one("span")
    if icon is not None:
        signals.append(str(icon.get("title", "")))
        classes = icon.get("class") or []
        signals.extend(classes if isinstance(classes, list) else [str(classes)])
    return any("high" in signal.lower() for signal in signals)


def parse_usd_high_impact_events(
    html: str,
    *,
    target_date: date,
    timezone: str = "America/Chicago",
) -> list[NewsEvent]:
    """Parse a Forex Factory calendar page for one day's USD high-impact events."""
    tz = ZoneInfo(timezone)
    soup = BeautifulSoup(html, "lxml")

    rows = soup.select("tr.calendar__row")
    if not rows:
        raise NewsScrapeError("no tr.calendar__row elements found", html=html)

    events: list[NewsEvent] = []
    current_day = target_date.isoformat()
    recognized_rows = 0

    for row in rows:
        day_cell = row.select_one("td.calendar__date")
        if day_cell and day_cell.get_text(strip=True):
            day_text = day_cell.get_text(" ", strip=True)
            try:
                parsed_day = date_parser.parse(day_text, fuzzy=True).date()
                current_day = parsed_day.isoformat()
            except (ValueError, OverflowError):
                pass

        currency_cell = row.select_one("td.calendar__currency")
        impact_cell = row.select_one("td.calendar__impact")
        event_cell = row.select_one("td.calendar__event")
        time_cell = row.select_one("td.calendar__time")

        if currency_cell is None or event_cell is None:
            continue
        recognized_rows += 1

        if current_day != target_date.isoformat():
            continue
        if currency_cell.get_text(strip=True).upper() != "USD":
            continue
        if impact_cell is None or not _impact_is_high(impact_cell):
            continue

        time_label = time_cell.get_text(strip=True) if time_cell else ""
        event_time = _parse_event_time(current_day, time_label, tz)
        if event_time is None:
            continue

        title = event_cell.get_text(" ", strip=True)
        event_id = re.sub(r"[^a-z0-9]+", "-", f"{current_day}-USD-{title}".lower()).strip("-")

        events.append(
            NewsEvent(
                event_id=event_id,
                title=title,
                currency="USD",
                impact="high",
                event_time=event_time,
            )
        )

    if recognized_rows == 0:
        raise NewsScrapeError("calendar rows found but no currency/event cells matched", html=html)

    return events


def fetch_usd_high_impact_events(
    *,
    target_date: date | None = None,
    timezone: str = "America/Chicago",
    attempts: int = 2,
) -> list[NewsEvent]:
    """Fetch and parse USD high-impact events from the Forex Factory calendar.

    There is no official free API; this parses the public HTML calendar page.
    Transient network/HTTP failures are retried once, then surfaced as
    :class:`NewsScrapeError` so the workflow fails loudly with a debug artifact
    instead of quietly reporting "no news".
    """
    tz = ZoneInfo(timezone)
    target_date = target_date or datetime.now(tz).date()

    headers = {
        "User-Agent": "tradovate-bot/0.1 (+https://github.com/ryan-doehring/tradovate)",
    }

    last_error: NewsScrapeError | None = None
    for attempt in range(max(1, attempts)):
        if attempt:
            time.sleep(2)
        try:
            response = httpx.get(
                FOREX_FACTORY_URL, headers=headers, timeout=30.0, follow_redirects=True
            )
        except httpx.HTTPError as exc:
            last_error = NewsScrapeError(f"request failed: {exc}")
            continue

        if response.status_code >= 400:
            last_error = NewsScrapeError(
                f"HTTP {response.status_code}",
                html=response.text,
                status_code=response.status_code,
            )
            continue

        return parse_usd_high_impact_events(
            response.text,
            target_date=target_date,
            timezone=timezone,
        )

    assert last_error is not None
    raise last_error


def active_news_window(
    events: list[NewsEvent],
    now: datetime,
    *,
    buffer_minutes: int,
    reopen_minutes: int,
) -> NewsEvent | None:
    """Return the news event whose blackout or reopen window contains `now`."""
    for event in events:
        blackout_start = event.event_time - timedelta(minutes=buffer_minutes)
        reopen_time = event.event_time + timedelta(minutes=reopen_minutes)
        if blackout_start <= now <= reopen_time:
            return event
    return None


def in_flat_window(
    event: NewsEvent,
    now: datetime,
    *,
    buffer_minutes: int,
    reopen_minutes: int,
) -> bool:
    """True from ``buffer`` minutes before the event until ``reopen`` minutes after.

    Everything inside this window is flat: no positions, no working orders.
    """
    start = event.event_time - timedelta(minutes=buffer_minutes)
    end = event.event_time + timedelta(minutes=reopen_minutes)
    return start <= now <= end


def in_blackout(event: NewsEvent, now: datetime, *, buffer_minutes: int) -> bool:
    start = event.event_time - timedelta(minutes=buffer_minutes)
    return start <= now < event.event_time


def should_reopen(event: NewsEvent, now: datetime, *, reopen_minutes: int) -> bool:
    reopen_at = event.event_time + timedelta(minutes=reopen_minutes)
    # Allow re-open for 5 minutes after reopen_at (workflow cadence tolerance)
    return reopen_at <= now <= reopen_at + timedelta(minutes=5)
