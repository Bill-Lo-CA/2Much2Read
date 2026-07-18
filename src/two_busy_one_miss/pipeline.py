from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, time, timedelta
from hashlib import sha256
from zoneinfo import ZoneInfo

from common.discord import deliver
from common.locking import ProcessLock

from .config import RemindersConfig, Settings, load_reminders
from .google_calendar import CalendarClient, CalendarEvent, credentials
from .renderer import render_agenda, render_reminder
from .rules import ReminderCandidate, parse_offset, schedule_reminders
from .storage import Database


def calendar_client(settings: Settings, config: RemindersConfig) -> CalendarClient:
    with ProcessLock(settings.lock_path):
        credentials_value = credentials(
            settings.google_calendar_credentials_path,
            settings.google_calendar_token_path,
            settings.google_calendar_oauth_callback_port,
        )
    return CalendarClient(credentials_value, config.timezone or settings.reminder_timezone)


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


def event_query_lookahead(config: RemindersConfig, days: int) -> timedelta:
    offsets = [parse_offset(reminder.before) for reminder in config.default_rules]
    offsets.extend(parse_offset(reminder.before) for rule in config.rules for reminder in rule.reminders)
    return max([timedelta(days=days), *offsets])


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


def _message_ids(row: sqlite3.Row) -> list[str]:
    try:
        value = row["discord_message_ids_json"]
    except KeyError:
        return []
    return [str(item) for item in json.loads(str(value))] if value else []


def _sync_scheduled_reminders(
    database: Database,
    config: RemindersConfig,
    events: list[CalendarEvent],
    window_start: datetime,
    window_end: datetime,
) -> tuple[int, int]:
    candidates = schedule_reminders(config, events)
    created = sum(database.create_attempt(candidate, render_reminder(candidate)) is not None for candidate in candidates)
    return created, database.cancel_unmatched_attempts(candidates, window_start, window_end)


def _dispatch_due_reminders(database: Database, settings: Settings, now: datetime) -> tuple[int, int]:
    sent = 0
    expired = 0
    for attempt in database.due_attempts(now):
        attempt_id = int(attempt["id"])
        if datetime.fromisoformat(str(attempt["event_start_at"])) <= now:
            database.expire_attempt(attempt_id)
            expired += 1
            continue

        def save_progress(message_ids: list[str], target_id: int = attempt_id) -> None:
            database.record_delivery_progress(target_id, message_ids)

        try:
            message_ids = deliver(
                settings.discord_webhook_url,
                str(attempt["content"]),
                settings.discord_username,
                _message_ids(attempt),
                save_progress,
            )
            database.finish_delivery(attempt_id, message_ids)
            sent += 1
        except Exception:
            database.fail_delivery(attempt_id)
            raise
    return sent, expired


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
    end = datetime.combine(day + timedelta(days=1), time.min, timezone)
    events = list_events_between(settings, config, start, end)
    content = render_agenda(day, events)
    if dry_run:
        return {"status": "ok", "content": content, "events": [event_view(event) for event in events]}
    with ProcessLock(settings.lock_path):
        message_ids = deliver(settings.discord_webhook_url, content, settings.discord_username)
    return {"status": "ok", "sent": 1, "discord_message_ids": message_ids, "events": len(events)}


def next_day_agenda(settings: Settings, dry_run: bool, force: bool, *, scheduled: bool = False) -> dict[str, object]:
    config = load_reminders(settings.reminders_config_path)
    timezone = ZoneInfo(config.timezone or settings.reminder_timezone)
    now = datetime.now(timezone)
    day = now.date() + timedelta(days=1)
    if scheduled and now < datetime.combine(now.date(), time(21), timezone):
        return {"status": "ok", "day": day.isoformat(), "sent": 0, "skipped": 1, "reason": "before_schedule"}
    start = datetime.combine(day, time.min, timezone)
    end = datetime.combine(day + timedelta(days=1), time.min, timezone)
    sync_end = now + event_query_lookahead(config, settings.reminder_lookahead_days)
    events = list_events_between(settings, config, now, sync_end)
    agenda_events = [event for event in events if event.end > start and event.start < end]
    content = render_agenda(day, agenda_events)
    if dry_run:
        return {
            "status": "ok",
            "day": day.isoformat(),
            "content": content,
            "events": [event_view(event) for event in agenda_events],
            "scheduled_reminders": len(schedule_reminders(config, events)),
        }

    destination_hash = sha256(settings.discord_webhook_url.encode()).hexdigest()
    with ProcessLock(settings.lock_path):
        database = Database(settings.database_path)
        try:
            scheduled_reminders, cancelled_reminders = _sync_scheduled_reminders(database, config, events, now, sync_end)
            delivery_id = database.create_agenda_delivery(day, timezone.key, destination_hash, content, force=force)
            if delivery_id is None:
                return {
                    "status": "ok",
                    "day": day.isoformat(),
                    "sent": 0,
                    "skipped": 1,
                    "events": len(agenda_events),
                    "scheduled_reminders": scheduled_reminders,
                    "cancelled_reminders": cancelled_reminders,
                }

            def save_progress(message_ids: list[str]) -> None:
                database.record_agenda_delivery_progress(delivery_id, message_ids)

            try:
                message_ids = deliver(
                    settings.discord_webhook_url,
                    content,
                    settings.discord_username,
                    on_progress=save_progress,
                )
                database.finish_agenda_delivery(delivery_id, message_ids)
            except Exception:
                database.fail_agenda_delivery(delivery_id)
                raise
        finally:
            database.close()
    return {
        "status": "ok",
        "day": day.isoformat(),
        "sent": 1,
        "events": len(agenda_events),
        "scheduled_reminders": scheduled_reminders,
        "cancelled_reminders": cancelled_reminders,
    }


def retry_agenda(settings: Settings, day: date) -> dict[str, object]:
    config = load_reminders(settings.reminders_config_path)
    timezone = ZoneInfo(config.timezone or settings.reminder_timezone)
    destination_hash = sha256(settings.discord_webhook_url.encode()).hexdigest()
    delivered = 0
    with ProcessLock(settings.lock_path):
        database = Database(settings.database_path)
        try:
            for delivery in database.pending_agenda_deliveries(day, timezone.key, destination_hash):
                delivery_id = int(delivery["id"])

                def save_progress(message_ids: list[str], target_id: int = delivery_id) -> None:
                    database.record_agenda_delivery_progress(target_id, message_ids)

                try:
                    message_ids = deliver(
                        settings.discord_webhook_url,
                        str(delivery["content"]),
                        settings.discord_username,
                        _message_ids(delivery),
                        save_progress,
                    )
                    database.finish_agenda_delivery(delivery_id, message_ids)
                    delivered += 1
                except Exception:
                    database.fail_agenda_delivery(delivery_id)
                    raise
        finally:
            database.close()
    return {"status": "ok", "day": day.isoformat(), "delivered": delivered}


def run(settings: Settings, dry_run: bool) -> dict[str, object]:
    config = load_reminders(settings.reminders_config_path)
    timezone = ZoneInfo(config.timezone or settings.reminder_timezone)
    now = datetime.now(timezone)
    if dry_run and not settings.database_path.exists():
        return {"status": "ok", "due": []}
    database = Database(settings.database_path, read_only=dry_run)
    try:
        if dry_run:
            return {"status": "ok", "due": [str(row["content"]) for row in database.due_attempts(now)]}
        with ProcessLock(settings.lock_path):
            sent, expired = _dispatch_due_reminders(database, settings, now)
    finally:
        database.close()
    return {"status": "ok", "sent": sent, "expired": expired}


def retry_delivery(settings: Settings) -> dict[str, object]:
    config = load_reminders(settings.reminders_config_path)
    now = datetime.now(ZoneInfo(config.timezone or settings.reminder_timezone))
    database = Database(settings.database_path)
    try:
        with ProcessLock(settings.lock_path):
            delivered, expired = _dispatch_due_reminders(database, settings, now)
    finally:
        database.close()
    return {"status": "ok", "delivered": delivered, "expired": expired}
