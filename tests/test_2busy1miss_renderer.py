from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from two_busy_one_miss.google_calendar import CalendarEvent, _event_links
from two_busy_one_miss.renderer import render_agenda, render_reminder
from two_busy_one_miss.rules import ReminderCandidate
from two_read_runtime.discord import chunk_text


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

    assert rendered.startswith("```text\n2busy1miss reminder · 5m before\nTIME        | EVENT")
    assert rendered.endswith("\n```")
    assert "@everyone" not in rendered
    assert "@here" not in rendered


def test_chunk_text_prefixes_long_messages() -> None:
    chunks = chunk_text("x" * 3000, limit=1000)

    assert len(chunks) == 4
    assert chunks[0].startswith("(1/4)")


def test_render_agenda_lists_events_and_disables_mentions() -> None:
    timezone = ZoneInfo("America/Montreal")
    start = datetime(2026, 7, 9, 7, 0, tzinfo=timezone)
    event = CalendarEvent(
        calendar_id="primary",
        calendar_name="Main",
        event_id="event-1",
        instance_id="event-1",
        title="@everyone French class",
        location="Zoom @here",
        start=start,
        end=start + timedelta(hours=3),
        all_day=False,
    )

    rendered = render_agenda(date(2026, 7, 9), [event])

    assert rendered.startswith("```text\n2busy1miss agenda · 2026-07-09\nTIME        | EVENT")
    assert "07:00-10:00 | @\u200beveryone French class (Main)" in rendered
    assert "            | Zoom @\u200bhere" in rendered
    assert rendered.endswith("\n```")
    assert "@everyone" not in rendered
    assert "@here" not in rendered


def test_urls_move_below_the_table_without_metadata_links() -> None:
    timezone = ZoneInfo("America/Montreal")
    start = datetime(2026, 7, 9, 7, 0, tzinfo=timezone)
    event = CalendarEvent(
        calendar_id="primary",
        calendar_name="Main https://calendar.example/view",
        event_id="event-1",
        instance_id="event-1",
        title="Review https://docs.example/brief",
        location="Meet https://meet.example/join.",
        start=start,
        end=start + timedelta(hours=1),
        all_day=False,
        links=("https://calendar.example/event",),
    )
    links = (
        "[Title](https://docs.example/brief) · [Calendar](https://calendar.example/view) · [Location](https://meet.example/join)"
    )

    agenda = render_agenda(date(2026, 7, 9), [event])
    assert "https://" not in agenda.split("```", 2)[1]
    assert agenda.endswith(f"```\n{links}")
    assert render_reminder(ReminderCandidate(event, "default-5m", "5m", start)).endswith(f"```\n{links}")


def test_calendar_event_links_include_event_meeting_and_description_urls() -> None:
    assert _event_links(
        {
            "htmlLink": "https://calendar.example/event",
            "hangoutLink": "https://meet.google.com/abc-defg-hij",
            "description": "Notes: https://docs.example/brief.",
            "conferenceData": {"entryPoints": [{"uri": "https://zoom.example/join"}]},
        }
    ) == (
        "https://calendar.example/event",
        "https://meet.google.com/abc-defg-hij",
        "https://docs.example/brief",
        "https://zoom.example/join",
    )


def test_render_agenda_marks_events_crossing_the_day_boundary() -> None:
    timezone = ZoneInfo("America/Montreal")
    day = date(2026, 7, 9)

    def event(title: str, start: datetime, end: datetime) -> CalendarEvent:
        return CalendarEvent("primary", "Main", title, title, title, "", start, end, False)

    rendered = render_agenda(
        day,
        [
            event("yesterday", datetime(2026, 7, 8, 21, tzinfo=timezone), datetime(2026, 7, 9, 4, tzinfo=timezone)),
            event("tomorrow", datetime(2026, 7, 9, 21, tzinfo=timezone), datetime(2026, 7, 10, 4, tzinfo=timezone)),
            event("midnight", datetime(2026, 7, 9, 21, tzinfo=timezone), datetime(2026, 7, 10, 0, tzinfo=timezone)),
            event("earlier", datetime(2026, 7, 7, 21, tzinfo=timezone), datetime(2026, 7, 9, 4, tzinfo=timezone)),
        ],
    )

    assert "Yesterday-04:00 | yesterday" in rendered
    assert "21:00-Tomorrow | tomorrow" in rendered
    assert "21:00-00:00 | midnight" in rendered
    assert "2026-07-07-04:00 | earlier" in rendered


def test_render_agenda_handles_empty_day() -> None:
    rendered = render_agenda(date(2026, 7, 9), [])

    assert rendered.startswith("```text\n2busy1miss agenda · 2026-07-09\nTIME        | EVENT\n------------+")
    assert "            | No events" in rendered
    assert rendered.endswith("\n```")


def test_render_agenda_keeps_event_text_inside_code_block() -> None:
    event = CalendarEvent(
        calendar_id="primary",
        calendar_name="Main",
        event_id="event-1",
        instance_id="event-1",
        title="Deploy ``` now",
        location="Room\n2",
        start=datetime(2026, 7, 9, 7, 0, tzinfo=ZoneInfo("America/Montreal")),
        end=datetime(2026, 7, 9, 8, 0, tzinfo=ZoneInfo("America/Montreal")),
        all_day=False,
    )

    rendered = render_agenda(date(2026, 7, 9), [event])

    assert "``` now" not in rendered
    assert "Deploy ˋˋˋ now" in rendered
    assert "Room 2" in rendered
