from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

from .google_calendar import CalendarEvent
from .rules import ReminderCandidate

URL = re.compile(r"https?://[^\s<>()>]+")


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
    if calendar_name := _agenda_cell(event.calendar_name or ""):
        lines.append(f"{'':<11} | Calendar: {calendar_name}")
    if location := _agenda_cell(event.location):
        lines.append(f"{'':<11} | Location: {location}")
    return _with_links(lines, [event])


def _agenda_cell(value: str) -> str:
    return " ".join(URL.sub("", value).replace("@", "@\u200b").replace("`", "ˋ").split())


def _with_links(lines: list[str], events: list[CalendarEvent]) -> str:
    link_lines = []
    for event in events:
        links = dict.fromkeys((url.rstrip(".,;:!?") for url in event.links[:1]), "Event")
        for label, value in (("Title", event.title), ("Calendar", event.calendar_name or ""), ("Location", event.location)):
            for url in URL.findall(value):
                links.setdefault(url.rstrip(".,;:!?"), label)
        if links:
            link_lines.append(" · ".join(f"[{label}]({url})" for url, label in links.items()))
    return "\n".join([*lines, "```", *link_lines])


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
        return _with_links([*lines, f"{'':<11} | No events"], events)
    for event in sorted(events, key=lambda item: (item.start, item.calendar_id, item.instance_id)):
        when = _agenda_when(day, event)
        summary = _agenda_cell(event.title)
        if calendar_name := _agenda_cell(event.calendar_name or ""):
            summary += f" ({calendar_name})"
        lines.append(f"{when:<11} | {summary}")
        if location := _agenda_cell(event.location):
            lines.append(f"{'':<11} | {location}")
    return _with_links(lines, events)
