from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from urllib.parse import urlsplit

from two_read_runtime.discord import sanitize_discord_text

from .google_calendar import CalendarEvent
from .rules import ReminderCandidate

URL = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)


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
    return " ".join(
        sanitize_discord_text(URL.sub("", value).replace("@", "＠").replace("`", "ˋ")).replace("＠", "@\u200b").split()
    )


def _valid_link(raw_url: str) -> tuple[str, str] | None:
    url = raw_url.rstrip(".,;:!?)]}")
    if not url.isprintable():
        return None
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        return None
    if port is not None and not 0 <= port <= 65_535:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    if any(character in hostname for character in "`*_~|[]()<>\\"):
        return None
    return url, hostname


def _event_links(event: CalendarEvent) -> list[tuple[str, str, str]]:
    links: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for label, value in (
        ("Title", event.title),
        ("Calendar", event.calendar_name or ""),
        ("Location", event.location),
    ):
        for raw_url in URL.findall(value):
            link = _valid_link(raw_url)
            if link is None or link[0] in seen:
                continue
            seen.add(link[0])
            links.append((label, *link))
            if len(links) == 3:
                return links
    return links


def _with_links(lines: list[str], events: list[CalendarEvent]) -> str:
    link_lines: list[str] = []
    seen: set[str] = set()
    for event in events:
        for label, url, hostname in _event_links(event):
            if url in seen:
                continue
            seen.add(url)
            if len(link_lines) == 20:
                return "\n".join([*lines, "```", *link_lines])
            link_lines.append(f"{label} link - {hostname}: <{url}>")
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
    ordered_events = sorted(events, key=lambda item: (item.start, item.calendar_id, item.instance_id))
    lines = [
        "```text",
        f"2busy1miss agenda · {day.isoformat()}",
        "TIME        | EVENT",
        "------------+-------------------------------------------",
    ]
    if not events:
        return _with_links([*lines, f"{'':<11} | No events"], events)
    for event in ordered_events:
        when = _agenda_when(day, event)
        summary = _agenda_cell(event.title)
        if calendar_name := _agenda_cell(event.calendar_name or ""):
            summary += f" ({calendar_name})"
        lines.append(f"{when:<11} | {summary}")
        if location := _agenda_cell(event.location):
            lines.append(f"{'':<11} | {location}")
    return _with_links(lines, ordered_events)
