from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from tradovate_bot.news.forexfactory import (
    NewsEvent,
    NewsScrapeError,
    in_blackout,
    in_flat_window,
    parse_usd_high_impact_events,
    should_reopen,
)

CALENDAR_HTML = """
<html><body><table>
<tr class="calendar__row">
  <td class="calendar__date">Jul 15 2026</td>
  <td class="calendar__time">8:30am</td>
  <td class="calendar__currency">USD</td>
  <td class="calendar__impact"><span class="icon" title="High Impact Expected"></span></td>
  <td class="calendar__event">Core CPI m/m</td>
</tr>
<tr class="calendar__row">
  <td class="calendar__time">9:00am</td>
  <td class="calendar__currency">EUR</td>
  <td class="calendar__impact"><span title="High Impact Expected"></span></td>
  <td class="calendar__event">German Factory Orders</td>
</tr>
<tr class="calendar__row">
  <td class="calendar__time">10:00am</td>
  <td class="calendar__currency">USD</td>
  <td class="calendar__impact"><span title="Low Impact Expected"></span></td>
  <td class="calendar__event">Wholesale Inventories</td>
</tr>
</table></body></html>
"""


def test_parses_only_usd_high_impact_events():
    events = parse_usd_high_impact_events(
        CALENDAR_HTML,
        target_date=date(2026, 7, 15),
        timezone="America/Chicago",
    )
    assert len(events) == 1
    event = events[0]
    assert event.title == "Core CPI m/m"
    assert event.currency == "USD"
    # 8:30am CT in July is 13:30 UTC.
    assert event.event_time == datetime(2026, 7, 15, 13, 30, tzinfo=UTC)
    assert event.event_id == "2026-07-15-usd-core-cpi-m-m"


def test_page_without_calendar_rows_fails_loudly():
    with pytest.raises(NewsScrapeError):
        parse_usd_high_impact_events(
            "<html><body>blocked</body></html>",
            target_date=date(2026, 7, 15),
        )


def test_rows_without_recognisable_cells_fail_loudly():
    html = "<table><tr class='calendar__row'><td>x</td></tr></table>"
    with pytest.raises(NewsScrapeError):
        parse_usd_high_impact_events(html, target_date=date(2026, 7, 15))


def test_flat_window_spans_before_and_after_the_event():
    event = NewsEvent(
        event_id="nfp",
        title="Non-Farm Payrolls",
        currency="USD",
        impact="high",
        event_time=datetime(2026, 7, 15, 13, 30, tzinfo=UTC),
    )
    before = datetime(2026, 7, 15, 13, 25, tzinfo=UTC)
    after = datetime(2026, 7, 15, 13, 55, tzinfo=UTC)
    too_late = datetime(2026, 7, 15, 14, 5, tzinfo=UTC)

    assert in_flat_window(event, before, buffer_minutes=10, reopen_minutes=30)
    assert in_flat_window(event, after, buffer_minutes=10, reopen_minutes=30)
    assert not in_flat_window(event, too_late, buffer_minutes=10, reopen_minutes=30)

    assert in_blackout(event, before, buffer_minutes=10)
    assert not in_blackout(event, after, buffer_minutes=10)
    reopen = datetime(2026, 7, 15, 14, 0, tzinfo=UTC)
    assert should_reopen(event, reopen, reopen_minutes=30)


def test_event_round_trips_through_state_dict():
    event = NewsEvent(
        event_id="nfp",
        title="Non-Farm Payrolls",
        currency="USD",
        impact="high",
        event_time=datetime(2026, 7, 15, 13, 30, tzinfo=UTC),
    )
    assert NewsEvent.from_dict(event.to_dict()) == event
    assert NewsEvent.from_dict({}) is None