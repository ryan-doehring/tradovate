from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal


@dataclass(frozen=True)
class SessionInfo:
    """CME_Equity session for one calendar date.

    ``is_closed`` means no session is scheduled for that date (weekend, holiday, or
    full-day closure). It does not describe whether the market is open right now —
    use :meth:`MarketCalendar.is_market_open` for that.
    """

    trading_date: date
    market_open: datetime
    market_close: datetime
    is_early_close: bool
    is_closed: bool


class MarketCalendar:
    """CME equity index futures schedule via pandas_market_calendars."""

    def __init__(self, timezone: str = "America/Chicago") -> None:
        self._tz = ZoneInfo(timezone)
        self._calendar = mcal.get_calendar("CME_Equity")

    def local_now(self) -> datetime:
        return datetime.now(self._tz)

    def session_for_today(self) -> SessionInfo:
        return self.session_for(self.local_now().date())

    def session_for(self, day: date) -> SessionInfo:
        schedule = self._calendar.schedule(
            start_date=day.isoformat(),
            end_date=day.isoformat(),
            tz=self._tz.key,
        )
        if schedule.empty:
            return SessionInfo(
                trading_date=day,
                market_open=datetime.min.replace(tzinfo=self._tz),
                market_close=datetime.min.replace(tzinfo=self._tz),
                is_early_close=False,
                is_closed=True,
            )

        row = schedule.iloc[0]
        market_open = row.market_open.to_pydatetime()
        market_close = row.market_close.to_pydatetime()
        early = self._calendar.early_closes(schedule)
        is_early = not early.empty

        return SessionInfo(
            trading_date=day,
            market_open=market_open,
            market_close=market_close,
            is_early_close=is_early,
            is_closed=False,
        )

    def eod_flat_time(self, day: date, *, minutes_before_close: int) -> datetime | None:
        session = self.session_for(day)
        if session.is_closed:
            return None
        return session.market_close - timedelta(minutes=minutes_before_close)

    def is_market_open(self, now: datetime) -> bool:
        """True when ``now`` falls inside a CME_Equity session.

        Sessions run 17:00 CT -> 16:00 CT, so the overnight reopen belongs to the
        *next* calendar day's session. Both today and tomorrow are checked, which
        keeps 03:00 and 19:00 entries working while the 16:00-17:00 maintenance
        halt (and weekends/holidays) read as closed.
        """
        local_now = now.astimezone(self._tz)
        for day in (local_now.date(), local_now.date() + timedelta(days=1)):
            session = self.session_for(day)
            if session.is_closed:
                continue
            if session.market_open <= local_now < session.market_close:
                return True
        return False

    def is_pre_close(self, now: datetime, *, minutes_before_close: int) -> bool:
        """True from ``minutes_before_close`` until the session close.

        Every tick inside this window re-asserts flat, so a missed run self-heals.
        """
        local_now = now.astimezone(self._tz)
        flat_at = self.eod_flat_time(local_now.date(), minutes_before_close=minutes_before_close)
        if flat_at is None:
            return False
        session = self.session_for(local_now.date())
        return flat_at <= local_now < session.market_close

    def is_eod_window(self, now: datetime, *, minutes_before_close: int) -> bool:
        local_now = now.astimezone(self._tz)
        flat_at = self.eod_flat_time(local_now.date(), minutes_before_close=minutes_before_close)
        if flat_at is None:
            return False
        # Wide window so a once-daily GitHub cron still hits the flatten window.
        return flat_at - timedelta(minutes=15) <= local_now <= flat_at + timedelta(minutes=15)
