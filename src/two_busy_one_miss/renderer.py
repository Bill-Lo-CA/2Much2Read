from __future__ import annotations

from datetime import date, datetime, time, timedelta

from .google_calendar import CalendarEvent
from .rules import ReminderCandidate


def _when(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M %Z").strip()


def render_reminder(candidate: ReminderCandidate) -> str:
    event = candidate.event
    when = "All day" if event.all_day else f"{event.start:%H:%M}-{event.end:%H:%M}"
    lines = [
        "```text",
        f"2busy1miss reminder · {candidate.before} before",
        "TIME        | EVENT",
        "------------+-------------------------------------------",
        f"{when:<11} | {_agenda_cell(event.title)}",
        f"{'':<11} | Starts: {_when(event.start)}",
    ]
    if event.calendar_name:
        lines.append(f"{'':<11} | Calendar: {_agenda_cell(event.calendar_name)}")
    if event.location:
        lines.append(f"{'':<11} | Location: {_agenda_cell(event.location)}")
    return "\n".join([*lines, "```"])


def _agenda_cell(value: str) -> str:
    return " ".join(value.replace("@", "@\u200b").replace("`", "ˋ").split())


def _agenda_when(day: date, event: CalendarEvent) -> str:
    if event.all_day:
        return "All day"
    start = "Yesterday" if event.start.date() == day - timedelta(days=1) else f"{event.start:%H:%M}"
    if event.start.date() < day - timedelta(days=1):
        start = event.start.date().isoformat()
    end = "Tomorrow" if event.end.date() == day + timedelta(days=1) and event.end.time() != time.min else f"{event.end:%H:%M}"
    if event.end.date() > day + timedelta(days=1):
        end = event.end.date().isoformat()
    return f"{start}-{end}"


def render_agenda(day: date, events: list[CalendarEvent]) -> str:
    lines = [
        "```text",
        f"2busy1miss agenda · {day.isoformat()}",
        "TIME        | EVENT",
        "------------+-------------------------------------------",
    ]
    if not events:
        return "\n".join([*lines, f"{'':<11} | No events", "```"])
    for event in sorted(events, key=lambda item: (item.start, item.calendar_id, item.instance_id)):
        when = _agenda_when(day, event)
        summary = _agenda_cell(event.title)
        if event.calendar_name:
            summary += f" ({_agenda_cell(event.calendar_name)})"
        lines.append(f"{when:<11} | {summary}")
        if event.location:
            lines.append(f"{'':<11} | {_agenda_cell(event.location)}")
    return "\n".join([*lines, "```"])
