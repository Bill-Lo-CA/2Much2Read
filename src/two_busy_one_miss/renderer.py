from __future__ import annotations

from datetime import datetime

from .rules import ReminderCandidate


def chunk_text(text: str, limit: int = 2000) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        cut = min(limit - 12, len(remaining))
        if cut < len(remaining):
            boundary = max(remaining.rfind("\n\n", 0, cut), remaining.rfind("\n", 0, cut))
            cut = boundary if boundary > limit // 2 else cut
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    total = len(chunks)
    return [f"({index}/{total}) {chunk}" for index, chunk in enumerate(chunks, 1)]


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
