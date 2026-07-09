from __future__ import annotations

import os
import time as time_module
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import TracebackType
from zoneinfo import ZoneInfo

import httpx

from .config import RemindersConfig, Settings, load_reminders
from .google_calendar import CalendarClient, CalendarEvent, credentials
from .renderer import chunk_text, render_agenda, render_reminder
from .rules import ReminderCandidate, due_reminders, schedule_reminders
from .storage import Database


class ProcessLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> ProcessLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(self.fd, str(os.getpid()).encode())
        except FileExistsError:
            raise RuntimeError("LOCK_CONTENDED") from None
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.fd is not None:
            os.close(self.fd)
        self.path.unlink(missing_ok=True)


def discord_deliver(webhook_url: str, content: str, username: str) -> list[str]:
    if not webhook_url:
        raise ValueError("DISCORD_WEBHOOK_URL is required")
    message_ids: list[str] = []
    for chunk in chunk_text(content):
        for attempt in range(4):
            response = httpx.post(
                webhook_url,
                params={"wait": "true"},
                json={"content": chunk, "username": username, "allowed_mentions": {"parse": []}},
                timeout=30,
            )
            if response.status_code == 429:
                time_module.sleep(float(response.headers.get("Retry-After", "1")))
                continue
            if response.status_code >= 500 and attempt < 3:
                time_module.sleep(2**attempt)
                continue
            response.raise_for_status()
            message_ids.append(str(response.json()["id"]))
            break
        else:
            raise RuntimeError("DISCORD_DELIVERY_FAILED")
    return message_ids


def calendar_client(settings: Settings, config: RemindersConfig) -> CalendarClient:
    return CalendarClient(
        credentials(
            settings.google_calendar_credentials_path,
            settings.google_calendar_token_path,
            settings.google_calendar_oauth_callback_port,
        ),
        config.timezone or settings.reminder_timezone,
    )


def list_events(settings: Settings, config: RemindersConfig, days: int) -> list[CalendarEvent]:
    timezone = ZoneInfo(config.timezone or settings.reminder_timezone)
    now = datetime.now(timezone)
    return list_events_between(settings, config, now, now + timedelta(days=days))


def list_events_between(
    settings: Settings, config: RemindersConfig, time_min: datetime, time_max: datetime
) -> list[CalendarEvent]:
    client = calendar_client(settings, config)
    events: list[CalendarEvent] = []
    for calendar in config.enabled_calendars:
        events.extend(client.list_events(calendar.id, calendar.name, time_min, time_max))
    return sorted(events, key=lambda event: (event.start, event.calendar_id, event.instance_id))


def event_view(event: CalendarEvent) -> dict[str, object]:
    return {
        "calendar_id": event.calendar_id,
        "calendar_name": event.calendar_name,
        "event_id": event.event_id,
        "instance_id": event.instance_id,
        "title": event.title,
        "location": event.location,
        "start": event.start.isoformat(),
        "end": event.end.isoformat(),
        "all_day": event.all_day,
    }


def reminder_view(candidate: ReminderCandidate) -> dict[str, object]:
    return {
        "rule_id": candidate.rule_id,
        "before": candidate.before,
        "reminder_time": candidate.reminder_time.isoformat(),
        "event": event_view(candidate.event),
    }


def discover(settings: Settings, days: int) -> dict[str, object]:
    config = load_reminders(settings.reminders_config_path)
    events = list_events(settings, config, days)
    return {"status": "ok", "events": [event_view(event) for event in events]}


def test_rules(settings: Settings, days: int) -> dict[str, object]:
    config = load_reminders(settings.reminders_config_path)
    candidates = schedule_reminders(config, list_events(settings, config, days))
    return {"status": "ok", "reminders": [reminder_view(candidate) for candidate in candidates]}


def agenda(settings: Settings, day: date, dry_run: bool) -> dict[str, object]:
    config = load_reminders(settings.reminders_config_path)
    timezone = ZoneInfo(config.timezone or settings.reminder_timezone)
    start = datetime.combine(day, time.min, timezone)
    end = start + timedelta(days=1)
    events = list_events_between(settings, config, start, end)
    content = render_agenda(day, events)
    if dry_run:
        return {"status": "ok", "content": content, "events": [event_view(event) for event in events]}
    message_ids = discord_deliver(settings.discord_webhook_url, content, settings.discord_username)
    return {"status": "ok", "sent": 1, "discord_message_ids": message_ids, "events": len(events)}


def run(settings: Settings, dry_run: bool) -> dict[str, object]:
    config = load_reminders(settings.reminders_config_path)
    timezone = ZoneInfo(config.timezone or settings.reminder_timezone)
    candidates = due_reminders(
        schedule_reminders(config, list_events(settings, config, settings.reminder_lookahead_days)), datetime.now(timezone)
    )
    if dry_run:
        return {"status": "ok", "due": [render_reminder(candidate) for candidate in candidates]}

    sent = 0
    skipped = 0
    database = Database(settings.database_path)
    try:
        with ProcessLock(settings.lock_path):
            for candidate in candidates:
                content = render_reminder(candidate)
                attempt_id = database.create_attempt(candidate, content)
                if attempt_id is None:
                    skipped += 1
                    continue
                try:
                    database.finish_delivery(
                        attempt_id, discord_deliver(settings.discord_webhook_url, content, settings.discord_username)
                    )
                    sent += 1
                except Exception:
                    database.fail_delivery(attempt_id)
                    raise
    finally:
        database.close()
    return {"status": "ok", "sent": sent, "skipped": skipped}


def retry_delivery(settings: Settings) -> dict[str, object]:
    database = Database(settings.database_path)
    delivered = 0
    try:
        for attempt in database.pending_attempts():
            attempt_id = int(attempt["id"])
            try:
                message_ids = discord_deliver(settings.discord_webhook_url, str(attempt["content"]), settings.discord_username)
                database.finish_delivery(attempt_id, message_ids)
                delivered += 1
            except Exception:
                database.fail_delivery(attempt_id)
                raise
    finally:
        database.close()
    return {"status": "ok", "delivered": delivered}
