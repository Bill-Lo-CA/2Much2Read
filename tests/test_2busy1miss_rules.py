from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from two_busy_one_miss.config import EventMatch, RemindersConfig, ReminderSpec, RuleConfig
from two_busy_one_miss.google_calendar import CalendarEvent
from two_busy_one_miss.rules import matches, parse_offset, schedule_reminders


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
