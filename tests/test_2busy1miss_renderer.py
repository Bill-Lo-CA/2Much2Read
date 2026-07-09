from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from two_busy_one_miss.google_calendar import CalendarEvent
from two_busy_one_miss.renderer import chunk_text, render_reminder
from two_busy_one_miss.rules import ReminderCandidate


def test_render_reminder_disables_mentions() -> None:
    timezone = ZoneInfo("America/Montreal")
    start = datetime(2026, 7, 8, 10, 0, tzinfo=timezone)
    event = CalendarEvent(
        calendar_id="primary",
        calendar_name="Main",
        event_id="event-1",
        instance_id="event-1",
        title="@everyone French class",
        location="Room @here",
        start=start,
        end=start + timedelta(hours=1),
        all_day=False,
    )
    candidate = ReminderCandidate(event, "default-5m", "5m", start - timedelta(minutes=5))

    rendered = render_reminder(candidate)

    assert "@everyone" not in rendered
    assert "@here" not in rendered


def test_chunk_text_prefixes_long_messages() -> None:
    chunks = chunk_text("x" * 3000, limit=1000)

    assert len(chunks) == 4
    assert chunks[0].startswith("(1/4)")
