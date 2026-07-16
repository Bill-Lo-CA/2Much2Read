from __future__ import annotations

from datetime import date, datetime

from .google_calendar import CalendarEvent
from .rules import ReminderCandidate


def _when(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M %Z").strip()


def render_reminder(candidate: ReminderCandidate) -> str:
    event = candidate.event
    lines = [
        f"2busy1miss: {event.title}".replace("@", "@\u200b"),
        f"Starts: {_when(event.start)}",
        f"Reminder: {candidate.before} before",
    ]
    if event.calendar_name:
        lines.append(f"Calendar: {event.calendar_name}")
    if event.location:
        lines.append(f"Location: {event.location}".replace("@", "@\u200b"))
    return "\n".join(lines)


def render_agenda(day: date, events: list[CalendarEvent]) -> str:
    header = f"2busy1miss agenda: {day.isoformat()}"
    if not events:
        return f"{header}\nNo events"
    lines = [header]
    for event in sorted(events, key=lambda item: (item.start, item.calendar_id, item.instance_id)):
        when = "All day" if event.all_day else f"{event.start:%H:%M}-{event.end:%H:%M}"
        summary = f"- {when} {event.title}".replace("@", "@\u200b")
        if event.calendar_name:
            summary += f" ({event.calendar_name})"
        lines.append(summary)
        if event.location:
            lines.append(f"  Location: {event.location}".replace("@", "@\u200b"))
    return "\n".join(lines)
