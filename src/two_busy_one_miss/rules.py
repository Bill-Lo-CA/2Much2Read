from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import EventMatch, RemindersConfig
from .google_calendar import CalendarEvent


@dataclass(frozen=True)
class ReminderCandidate:
    event: CalendarEvent
    rule_id: str
    before: str
    reminder_time: datetime


def parse_offset(value: str) -> timedelta:
    match = re.fullmatch(r"([1-9]\d*)([mhd])", value)
    if not match:
        raise ValueError(f"invalid reminder offset {value!r}; expected 5m, 2h, or 1d")
    amount = int(match.group(1))
    unit = match.group(2)
    if unit == "m":
        return timedelta(minutes=amount)
    if unit == "h":
        return timedelta(hours=amount)
    return timedelta(days=amount)


def _contains_any(value: str, needles: list[str]) -> bool:
    folded = value.casefold()
    return any(needle.casefold() in folded for needle in needles)


def matches(event: CalendarEvent, rule: EventMatch) -> bool:
    if rule.calendar_id is not None and event.calendar_id != rule.calendar_id:
        return False
    if rule.has_location is not None and bool(event.location.strip()) is not rule.has_location:
        return False
    if rule.all_day is not None and event.all_day is not rule.all_day:
        return False
    if rule.title_contains and not _contains_any(event.title, rule.title_contains):
        return False
    return not (rule.location_contains and not _contains_any(event.location, rule.location_contains))


def schedule_reminders(config: RemindersConfig, events: list[CalendarEvent]) -> list[ReminderCandidate]:
    scheduled: dict[tuple[str, str, datetime], ReminderCandidate] = {}
    for event in events:
        for reminder in config.default_rules:
            rule_id = reminder.id or f"default:{reminder.before}"
            candidate = ReminderCandidate(event, rule_id, reminder.before, event.start - parse_offset(reminder.before))
            scheduled[event.calendar_id, event.instance_id, candidate.reminder_time] = candidate
        for rule in config.rules:
            if not matches(event, rule.match):
                continue
            for reminder in rule.reminders:
                rule_id = reminder.id or f"{rule.id}:{reminder.before}"
                candidate = ReminderCandidate(event, rule_id, reminder.before, event.start - parse_offset(reminder.before))
                scheduled[event.calendar_id, event.instance_id, candidate.reminder_time] = candidate
    return sorted(scheduled.values(), key=lambda item: (item.reminder_time, item.event.start, item.rule_id))


def due_reminders(candidates: list[ReminderCandidate], now: datetime) -> list[ReminderCandidate]:
    return [item for item in candidates if item.reminder_time <= now <= item.event.start]
