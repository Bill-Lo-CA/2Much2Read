from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from two_busy_one_miss.config import EventMatch, RemindersConfig, ReminderSpec, RuleConfig
from two_busy_one_miss.google_calendar import CalendarEvent, _parse_datetime
from two_busy_one_miss.rules import matches, parse_offset, schedule_reminders


def event_at(start: datetime, *, all_day: bool = False) -> CalendarEvent:
    return CalendarEvent(
        calendar_id="primary",
        calendar_name="Main",
        event_id="event-1",
        instance_id="event-1",
        title="Flight",
        location="YUL",
        start=start,
        end=start + timedelta(hours=2),
        all_day=all_day,
    )


def event(title: str = "French class", location: str = "Room 1") -> CalendarEvent:
    timezone = ZoneInfo("America/Montreal")
    start = datetime(2026, 7, 8, 10, 0, tzinfo=timezone)
    return CalendarEvent(
        calendar_id="primary",
        calendar_name="Main",
        event_id="event-1",
        instance_id="event-1",
        title=title,
        location=location,
        start=start,
        end=start + timedelta(hours=1),
        all_day=False,
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [("5m", timedelta(minutes=5)), ("2h", timedelta(hours=2)), ("1d", timedelta(days=1))],
)
def test_parse_offset(value: str, expected: timedelta) -> None:
    assert parse_offset(value) == expected


def test_matches_supported_fields() -> None:
    item = event()

    assert matches(item, EventMatch(title_contains=["French"], has_location=True, all_day=False))
    assert not matches(item, EventMatch(location_contains=["Library"]))


def test_schedules_defaults_and_matching_rules() -> None:
    config = RemindersConfig(
        calendars=[{"id": "primary"}],
        default_rules=[ReminderSpec(id="default-30m", before="30m"), ReminderSpec(id="default-5m", before="5m")],
        rules=[
            RuleConfig(
                id="french-class",
                match=EventMatch(title_contains=["French"]),
                reminders=[ReminderSpec(before="2h"), ReminderSpec(before="30m")],
            )
        ],
    )

    scheduled = schedule_reminders(config, [event()])

    assert [item.rule_id for item in scheduled] == ["french-class:2h", "french-class:30m", "default-5m"]


@pytest.mark.parametrize(
    ("raw_start", "before", "expected"),
    [
        # Montreal falls back on 2026-11-01. A reminder a day before an event that morning has to
        # cross the boundary, and `start - timedelta` on a zone-aware value walks the wall clock
        # rather than the clock face - so it landed 25 hours early.
        ("2026-11-01T10:00:00-05:00", "1d", timedelta(days=1)),
        ("2026-11-01T03:30:00-05:00", "2h", timedelta(hours=2)),
        # ...and 23 hours in March, when the hour goes the other way.
        ("2026-03-08T10:00:00-04:00", "1d", timedelta(days=1)),
    ],
)
def test_a_reminder_offset_that_crosses_a_dst_change_is_still_that_long(raw_start: str, before: str, expected: timedelta) -> None:
    """`before: 1d` means a day, and a day does not become 25 hours because a zone changed.

    CalendarEvent carries instants for this reason. The moment one carries a zone instead,
    subtracting an offset stops being absolute arithmetic and this reads an hour out twice a year.
    """
    timezone = ZoneInfo("America/Montreal")
    start = _parse_datetime(raw_start, timezone)
    config = RemindersConfig(calendars=[{"id": "primary"}], default_rules=[ReminderSpec(before=before)])

    [candidate] = schedule_reminders(config, [event_at(start)])

    assert start.astimezone(UTC) - candidate.reminder_time.astimezone(UTC) == expected


def test_an_all_day_reminder_offset_crosses_the_change_too() -> None:
    """All-day events reached CalendarEvent as a zone-aware midnight even before this branch.

    So they had the same defect on main, and `_parse_datetime` returning an instant is what fixes
    both at once rather than only the timed half.
    """
    timezone = ZoneInfo("America/Montreal")
    start = _parse_datetime("2026-11-02", timezone)
    config = RemindersConfig(calendars=[{"id": "primary"}], default_rules=[ReminderSpec(before="1d")])

    [candidate] = schedule_reminders(config, [event_at(start, all_day=True)])

    assert start.astimezone(UTC) - candidate.reminder_time.astimezone(UTC) == timedelta(days=1)
