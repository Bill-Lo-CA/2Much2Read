from __future__ import annotations

from datetime import date, datetime, time, timedelta
from hashlib import sha256
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from two_read_runtime.discord import DiscordDeliveryError, deliver, deliver_resumable, delivery_error_code
from two_read_runtime.locking import ProcessLock

from .config import RemindersConfig, Settings, load_reminders
from .google_calendar import CalendarClient, CalendarEvent, credentials
from .renderer import render_agenda, render_reminder
from .rules import ReminderCandidate, parse_offset, schedule_reminders
from .storage import Database


class EventView(BaseModel):
    calendar_id: str
    calendar_name: str | None
    event_id: str
    instance_id: str
    title: str
    location: str
    start: datetime
    end: datetime
    all_day: bool


class ReminderView(BaseModel):
    rule_id: str
    before: str
    reminder_time: datetime
    event: EventView


class DiscoverResult(BaseModel):
    status: Literal["ok"] = "ok"
    events: list[EventView]


class RulesTestResult(BaseModel):
    status: Literal["ok"] = "ok"
    reminders: list[ReminderView]


class AgendaPreviewResult(BaseModel):
    status: Literal["ok"] = "ok"
    content: str
    events: list[EventView]
    day: date | None = None
    scheduled_reminders: int | None = None


class AgendaDeliveryResult(BaseModel):
    status: Literal["ok"] = "ok"
    sent: int
    events: int | None = None
    day: date | None = None
    skipped: int | None = None
    reason: str | None = None
    discord_message_ids: list[str] | None = None
    scheduled_reminders: int | None = None
    cancelled_reminders: int | None = None


class AgendaRetryResult(BaseModel):
    status: Literal["ok"] = "ok"
    day: date
    delivered: int
    failed: int
    failed_by_error_code: dict[str, int]


class ReminderRunResult(BaseModel):
    status: Literal["ok"] = "ok"
    sent: int
    failed: int
    failed_by_error_code: dict[str, int]
    expired: int


class ReminderDryRunResult(BaseModel):
    status: Literal["ok"] = "ok"
    due: list[str]


class ReminderRetryResult(BaseModel):
    status: Literal["ok"] = "ok"
    delivered: int
    failed: int
    failed_by_error_code: dict[str, int]
    expired: int


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
    unique = {(event.calendar_id, event.instance_id): event for event in events}
    return sorted(unique.values(), key=lambda event: (event.start, event.calendar_id, event.instance_id))


def event_query_lookahead(config: RemindersConfig, days: int) -> timedelta:
    offsets = [parse_offset(reminder.before) for reminder in config.default_rules]
    offsets.extend(parse_offset(reminder.before) for rule in config.rules for reminder in rule.reminders)
    return max([timedelta(days=days), *offsets])


def event_view(event: CalendarEvent) -> EventView:
    return EventView(
        calendar_id=event.calendar_id,
        calendar_name=event.calendar_name,
        event_id=event.event_id,
        instance_id=event.instance_id,
        title=event.title,
        location=event.location,
        start=event.start,
        end=event.end,
        all_day=event.all_day,
    )


def reminder_view(candidate: ReminderCandidate) -> ReminderView:
    return ReminderView(
        rule_id=candidate.rule_id,
        before=candidate.before,
        reminder_time=candidate.reminder_time,
        event=event_view(candidate.event),
    )


def _sync_scheduled_reminders(
    database: Database,
    config: RemindersConfig,
    events: list[CalendarEvent],
    window_start: datetime,
    window_end: datetime,
) -> tuple[int, int]:
    candidates = schedule_reminders(config, events)
    created = database.create_attempts([(candidate, render_reminder(candidate)) for candidate in candidates])
    return created, database.cancel_unmatched_attempts(candidates, events, window_start, window_end)


def _create_agenda_delivery(
    database: Database, settings: Settings, day: date, timezone: ZoneInfo, content: str, *, force: bool
) -> int | None:
    destination_hash = sha256(settings.discord_webhook_url.encode()).hexdigest()
    return database.create_agenda_delivery(day, timezone.key, destination_hash, content, force=force)


def _deliver_agenda(
    database: Database, settings: Settings, delivery_id: int, content: str, stored_message_ids: object
) -> list[str]:
    def save_progress(message_ids: list[str]) -> None:
        database.record_agenda_delivery_progress(delivery_id, message_ids)

    try:
        return deliver_resumable(
            settings.discord_webhook_url,
            content,
            settings.discord_username,
            stored_message_ids,
            save_progress,
            lambda message_ids: database.finish_agenda_delivery(delivery_id, message_ids),
            sender=deliver,
        )
    except DiscordDeliveryError as error:
        database.fail_agenda_delivery(delivery_id, delivery_error_code(error))
        raise


def _dispatch_due_reminders(database: Database, settings: Settings, now: datetime) -> tuple[int, int, int, dict[str, int]]:
    sent = 0
    failed = 0
    expired = 0
    failed_by_error_code: dict[str, int] = {}
    for attempt in database.due_attempts(now):
        attempt_id = int(attempt["id"])
        if datetime.fromisoformat(str(attempt["event_start_at"])) <= now:
            database.expire_attempt(attempt_id)
            expired += 1
            continue

        def save_progress(message_ids: list[str], target_id: int = attempt_id) -> None:
            database.record_delivery_progress(target_id, message_ids)

        def finish_delivery(message_ids: list[str], target_id: int = attempt_id) -> None:
            database.finish_delivery(target_id, message_ids)

        try:
            deliver_resumable(
                settings.discord_webhook_url,
                str(attempt["content"]),
                settings.discord_username,
                attempt["discord_message_ids_json"],
                save_progress,
                finish_delivery,
                sender=deliver,
            )
            sent += 1
        except DiscordDeliveryError as error:
            error_code = delivery_error_code(error)
            database.fail_delivery(attempt_id, error_code)
            failed += 1
            failed_by_error_code[error_code] = failed_by_error_code.get(error_code, 0) + 1
    return sent, failed, expired, failed_by_error_code


def discover(settings: Settings, days: int) -> DiscoverResult:
    config = load_reminders(settings.reminders_config_path)
    events = list_events(settings, config, days)
    return DiscoverResult(events=[event_view(event) for event in events])


def test_rules(settings: Settings, days: int) -> RulesTestResult:
    config = load_reminders(settings.reminders_config_path)
    candidates = schedule_reminders(config, list_events(settings, config, days))
    return RulesTestResult(reminders=[reminder_view(candidate) for candidate in candidates])


def agenda(settings: Settings, day: date, dry_run: bool, force: bool = False) -> AgendaPreviewResult | AgendaDeliveryResult:
    config = load_reminders(settings.reminders_config_path)
    timezone = ZoneInfo(config.timezone or settings.reminder_timezone)
    start = datetime.combine(day, time.min, timezone)
    end = datetime.combine(day + timedelta(days=1), time.min, timezone)
    events = list_events_between(settings, config, start, end)
    content = render_agenda(day, events)
    if dry_run:
        return AgendaPreviewResult(content=content, events=[event_view(event) for event in events])
    with ProcessLock(settings.lock_path):
        database = Database(settings.database_path)
        try:
            delivery_id = _create_agenda_delivery(database, settings, day, timezone, content, force=force)
            if delivery_id is None:
                return AgendaDeliveryResult(sent=0, skipped=1, events=len(events))
            message_ids = _deliver_agenda(database, settings, delivery_id, content, None)
        finally:
            database.close()
    return AgendaDeliveryResult(sent=1, discord_message_ids=message_ids, events=len(events))


def next_day_agenda(
    settings: Settings, dry_run: bool, force: bool, *, scheduled: bool = False, now: datetime | None = None
) -> AgendaPreviewResult | AgendaDeliveryResult:
    config = load_reminders(settings.reminders_config_path)
    timezone = ZoneInfo(config.timezone or settings.reminder_timezone)
    now = (now or datetime.now(timezone)).astimezone(timezone)
    day = now.date() + timedelta(days=1)
    if scheduled and now < datetime.combine(now.date(), settings.agenda_schedule_time, timezone):
        return AgendaDeliveryResult(day=day, sent=0, skipped=1, reason="before_schedule")
    start = datetime.combine(day, time.min, timezone)
    end = datetime.combine(day + timedelta(days=1), time.min, timezone)
    sync_end = now + event_query_lookahead(config, settings.reminder_lookahead_days)
    events = list_events_between(settings, config, now, sync_end)
    agenda_events = [event for event in events if event.end > start and event.start < end]
    content = render_agenda(day, agenda_events)
    if dry_run:
        return AgendaPreviewResult(
            day=day,
            content=content,
            events=[event_view(event) for event in agenda_events],
            scheduled_reminders=len(schedule_reminders(config, events)),
        )

    with ProcessLock(settings.lock_path):
        database = Database(settings.database_path)
        try:
            scheduled_reminders, cancelled_reminders = _sync_scheduled_reminders(database, config, events, now, sync_end)
            delivery_id = _create_agenda_delivery(database, settings, day, timezone, content, force=force)
            if delivery_id is None:
                return AgendaDeliveryResult(
                    day=day,
                    sent=0,
                    skipped=1,
                    events=len(agenda_events),
                    scheduled_reminders=scheduled_reminders,
                    cancelled_reminders=cancelled_reminders,
                )
            _deliver_agenda(database, settings, delivery_id, content, None)
        finally:
            database.close()
    return AgendaDeliveryResult(
        day=day,
        sent=1,
        events=len(agenda_events),
        scheduled_reminders=scheduled_reminders,
        cancelled_reminders=cancelled_reminders,
    )


def retry_agenda(settings: Settings, day: date) -> AgendaRetryResult:
    config = load_reminders(settings.reminders_config_path)
    timezone = ZoneInfo(config.timezone or settings.reminder_timezone)
    destination_hash = sha256(settings.discord_webhook_url.encode()).hexdigest()
    delivered = 0
    failed = 0
    failed_by_error_code: dict[str, int] = {}
    with ProcessLock(settings.lock_path):
        database = Database(settings.database_path)
        try:
            for delivery in database.pending_agenda_deliveries(day, timezone.key, destination_hash):
                delivery_id = int(delivery["id"])

                try:
                    _deliver_agenda(
                        database, settings, delivery_id, str(delivery["content"]), delivery["discord_message_ids_json"]
                    )
                    delivered += 1
                except DiscordDeliveryError as error:
                    error_code = delivery_error_code(error)
                    failed += 1
                    failed_by_error_code[error_code] = failed_by_error_code.get(error_code, 0) + 1
        finally:
            database.close()
    return AgendaRetryResult(day=day, delivered=delivered, failed=failed, failed_by_error_code=failed_by_error_code)


def run(settings: Settings, dry_run: bool, *, now: datetime | None = None) -> ReminderRunResult | ReminderDryRunResult:
    config = load_reminders(settings.reminders_config_path)
    timezone = ZoneInfo(config.timezone or settings.reminder_timezone)
    now = (now or datetime.now(timezone)).astimezone(timezone)
    if dry_run and not settings.database_path.exists():
        return ReminderDryRunResult(due=[])
    database = Database(settings.database_path, read_only=dry_run)
    try:
        if dry_run:
            return ReminderDryRunResult(due=[str(row["content"]) for row in database.due_attempts(now)])
        with ProcessLock(settings.lock_path):
            sent, failed, expired, failed_by_error_code = _dispatch_due_reminders(database, settings, now)
    finally:
        database.close()
    return ReminderRunResult(sent=sent, failed=failed, failed_by_error_code=failed_by_error_code, expired=expired)


def retry_delivery(settings: Settings, *, now: datetime | None = None) -> ReminderRetryResult:
    config = load_reminders(settings.reminders_config_path)
    timezone = ZoneInfo(config.timezone or settings.reminder_timezone)
    now = (now or datetime.now(timezone)).astimezone(timezone)
    database = Database(settings.database_path)
    try:
        with ProcessLock(settings.lock_path):
            delivered, failed, expired, failed_by_error_code = _dispatch_due_reminders(database, settings, now)
    finally:
        database.close()
    return ReminderRetryResult(delivered=delivered, failed=failed, failed_by_error_code=failed_by_error_code, expired=expired)
