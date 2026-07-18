from __future__ import annotations

from datetime import date, datetime, time, timedelta
from hashlib import sha256
from zoneinfo import ZoneInfo

from common.discord import deliver
from common.locking import ProcessLock

from .config import RemindersConfig, Settings, load_reminders
from .google_calendar import CalendarClient, CalendarEvent, credentials
from .renderer import render_agenda, render_reminder
from .rules import ReminderCandidate, due_reminders, parse_offset, schedule_reminders
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
    return max(timedelta(days=days), *offsets)


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
    events = list_events_between(settings, config, start, end)
    content = render_agenda(day, events)
    if dry_run:
        return {"status": "ok", "day": day.isoformat(), "content": content, "events": [event_view(event) for event in events]}

    destination_hash = sha256(settings.discord_webhook_url.encode()).hexdigest()
    with ProcessLock(settings.lock_path):
        database = Database(settings.database_path)
        try:
            delivery_id = database.create_agenda_delivery(day, timezone.key, destination_hash, content, force=force)
            if delivery_id is None:
                return {"status": "ok", "day": day.isoformat(), "sent": 0, "skipped": 1, "events": len(events)}
            try:
                message_ids = deliver(settings.discord_webhook_url, content, settings.discord_username)
                database.finish_agenda_delivery(delivery_id, message_ids)
            except Exception:
                database.fail_agenda_delivery(delivery_id)
                raise
        finally:
            database.close()
    return {"status": "ok", "day": day.isoformat(), "sent": 1, "events": len(events)}


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
                try:
                    message_ids = deliver(settings.discord_webhook_url, str(delivery["content"]), settings.discord_username)
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
    candidates = due_reminders(
        schedule_reminders(
            config,
            list_events_between(settings, config, now, now + event_query_lookahead(config, settings.reminder_lookahead_days)),
        ),
        now,
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
                        attempt_id, deliver(settings.discord_webhook_url, content, settings.discord_username)
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
        with ProcessLock(settings.lock_path):
            for attempt in database.pending_attempts():
                attempt_id = int(attempt["id"])
                try:
                    message_ids = deliver(settings.discord_webhook_url, str(attempt["content"]), settings.discord_username)
                    database.finish_delivery(attempt_id, message_ids)
                    delivered += 1
                except Exception:
                    database.fail_delivery(attempt_id)
                    raise
    finally:
        database.close()
    return {"status": "ok", "delivered": delivered}
