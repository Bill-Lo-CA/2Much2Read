from __future__ import annotations

from datetime import date, datetime, time, timedelta
from hashlib import sha256
from pathlib import Path
from unittest.mock import MagicMock, call
from zoneinfo import ZoneInfo

import pytest

from two_busy_one_miss import pipeline
from two_busy_one_miss.config import EventMatch, RemindersConfig, ReminderSpec, RuleConfig, Settings
from two_busy_one_miss.google_calendar import CalendarClient, CalendarEvent
from two_busy_one_miss.pipeline import event_query_lookahead
from two_busy_one_miss.renderer import render_agenda
from two_busy_one_miss.rules import ReminderCandidate
from two_busy_one_miss.storage import Database
from two_read_runtime.discord import DiscordDeliveryError


class FakeReminderDatabase:
    def __init__(self, attempts: list[dict[str, object]]) -> None:
        self.attempts = attempts
        self.failed: list[tuple[int, str]] = []
        self.finished: list[tuple[int, list[str]]] = []
        self.progress: list[tuple[int, list[str]]] = []
        self.closed = False

    def due_attempts(self, now: datetime) -> list[dict[str, object]]:
        return self.attempts

    def delivery_checkpoint(self, attempt_id: int, destination_key: str) -> object:
        return next(attempt for attempt in self.attempts if attempt["id"] == attempt_id)["discord_message_ids_json"]

    def record_delivery_progress(self, attempt_id: int, message_ids: list[str], destination_key: str | None = None) -> None:
        self.progress.append((attempt_id, message_ids))

    def finish_delivery(self, attempt_id: int, message_ids: list[str], destination_key: str | None = None) -> None:
        self.finished.append((attempt_id, message_ids))

    def fail_delivery(self, attempt_id: int, error_code: str) -> None:
        self.failed.append((attempt_id, error_code))

    def close(self) -> None:
        self.closed = True


class RecordingLock:
    def __init__(self) -> None:
        self.entered = False

    def __enter__(self) -> RecordingLock:
        self.entered = True
        return self

    def __exit__(self, *args: object) -> None:
        pass


def unexpected(*args: object, **kwargs: object) -> None:
    raise AssertionError("unexpected runtime call")


def test_event_query_lookahead_covers_longest_reminder() -> None:
    config = RemindersConfig(
        calendars=[{"id": "primary"}],
        default_rules=[ReminderSpec(before="5m")],
        rules=[RuleConfig(id="long-reminder", match=EventMatch(), reminders=[ReminderSpec(before="10d")])],
    )

    assert event_query_lookahead(config, 7) == timedelta(days=10)


def test_calendar_client_lists_events() -> None:
    timezone = ZoneInfo("America/Montreal")
    client = CalendarClient.__new__(CalendarClient)
    client.service = MagicMock()
    client.timezone = timezone
    client.service.events.return_value.list.return_value.execute.return_value = {"items": []}
    start = datetime(2026, 7, 9, 10, tzinfo=timezone)
    end = start + timedelta(hours=1)

    assert client.list_events("primary", "Main", start, end) == []
    client.service.events.return_value.list.assert_called_once_with(
        calendarId="primary",
        timeMin=start.isoformat(),
        timeMax=end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
        pageToken=None,
    )


def test_list_events_between_deduplicates_by_instance_id(monkeypatch: pytest.MonkeyPatch) -> None:
    timezone = ZoneInfo("America/Montreal")
    start = datetime(2026, 7, 27, 17, tzinfo=timezone)
    duplicate = CalendarEvent("primary", "Main", "one", "one", "Cloud CTF", "KPMG", start, start + timedelta(hours=5), False)
    repeated = CalendarEvent("primary", "Main", "one", "one", "Cloud CTF", "KPMG", start, start + timedelta(hours=5), False)
    copied = CalendarEvent("primary", "Main", "two", "two", "Cloud CTF", "KPMG", start, start + timedelta(hours=5), False)
    config = RemindersConfig(calendars=[{"id": "primary", "name": "Main"}], timezone=timezone.key)

    class FakeCalendarClient:
        def list_events(self, *args: object) -> list[CalendarEvent]:
            return [duplicate, repeated, copied]

    monkeypatch.setattr(pipeline, "calendar_client", lambda *args: FakeCalendarClient())

    assert pipeline.list_events_between(Settings(), config, start, start + timedelta(days=1)) == [repeated, copied]


def test_calendar_client_lists_all_calendar_pages() -> None:
    client = CalendarClient.__new__(CalendarClient)
    client.service = MagicMock()
    client.service.calendarList.return_value.list.return_value.execute.side_effect = [
        {"items": [{"id": "primary", "summary": "Primary"}], "nextPageToken": "next"},
        {"items": [{"id": "team"}]},
    ]

    assert client.list_calendars() == [{"id": "primary", "name": "Primary"}, {"id": "team", "name": "team"}]
    assert client.service.calendarList.return_value.list.call_args_list == [
        call(maxResults=250, pageToken=None),
        call(maxResults=250, pageToken="next"),
    ]


def test_retry_delivery_holds_process_lock(calendar_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = calendar_settings
    config = RemindersConfig(calendars=[{"id": "primary"}], timezone="America/Montreal")
    database = FakeReminderDatabase(
        [
            {
                "id": 1,
                "content": "bad",
                "event_start_at": "2099-01-01T10:00:00+00:00",
                "discord_message_ids_json": None,
            },
            {
                "id": 2,
                "content": "good",
                "event_start_at": "2099-01-01T10:00:00+00:00",
                "discord_message_ids_json": None,
            },
        ]
    )
    lock = RecordingLock()
    monkeypatch.setattr(pipeline, "Database", lambda _: database)
    monkeypatch.setattr(pipeline, "ProcessLock", lambda _: lock)
    monkeypatch.setattr(pipeline, "load_reminders", lambda _: config)

    def fake_deliver(*args: object) -> list[str]:
        assert lock.entered
        if args[1] == "bad":
            raise DiscordDeliveryError("delivery failed")
        return ["discord-id"]

    monkeypatch.setattr(pipeline, "deliver", fake_deliver)

    assert pipeline.retry_delivery(settings).model_dump() == {
        "status": "partial",
        "delivered": 1,
        "failed": 1,
        "failed_by_error_code": {"DISCORD_DELIVERY_FAILED": 1},
        "expired": 0,
    }
    assert database.failed == [(1, "DISCORD_DELIVERY_FAILED")]
    assert database.finished == [(2, ["discord-id"])]
    assert database.closed


def test_retry_delivery_preserves_legacy_bot_checkpoint_in_both_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    start = datetime(2026, 7, 8, 10, tzinfo=ZoneInfo("America/Montreal"))
    item = ReminderCandidate(
        CalendarEvent("primary", "Main", "legacy", "legacy", "Legacy", "", start, start + timedelta(hours=1), False),
        "default-5m",
        "5m",
        start - timedelta(minutes=5),
    )
    config = RemindersConfig(calendars=[{"id": "primary"}], timezone="America/Montreal")
    settings = Settings(
        reminders_config_path=tmp_path / "reminders.yaml",
        database_path=tmp_path / "reminders.sqlite3",
        lock_path=tmp_path / "reminders.lock",
        discord_delivery_mode="both",
        discord_webhook_url="https://discord.com/api/webhooks/123456789012345678/test-webhook-token",
        discord_bot_token="token",
        discord_bot_channel_id="123",
    )
    database = Database(settings.database_path)
    attempt_id = database.create_attempt(item, "legacy reminder")
    assert attempt_id is not None
    bot_key = settings.discord_destinations()[1].key
    database.record_delivery_progress(attempt_id, ["old-bot-message"], bot_key)
    database.fail_delivery(attempt_id)
    database.close()
    calls: list[tuple[str, list[str] | None]] = []

    def fake_deliver(destination, _content, _username, message_ids, *_args, **_kwargs):
        calls.append((destination.transport, message_ids))
        return [*(message_ids or []), f"new-{destination.transport}-message"]

    monkeypatch.setattr(pipeline, "load_reminders", lambda _: config)
    monkeypatch.setattr(pipeline, "deliver", fake_deliver)

    result = pipeline.retry_delivery(settings, now=item.reminder_time + timedelta(minutes=1))

    assert result.model_dump() == {
        "status": "ok",
        "delivered": 2,
        "failed": 0,
        "failed_by_error_code": {},
        "expired": 0,
    }
    assert calls == [("webhook", []), ("bot", ["old-bot-message"])]
    database = Database(settings.database_path)
    assert database.attempt_state(attempt_id) == "delivered"
    assert database.counts()["reminder_deliveries"] == 2
    database.close()


def test_retry_retires_a_removed_failed_destination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    timezone = ZoneInfo("America/Montreal")
    now = datetime(2026, 7, 9, 9, 55, tzinfo=timezone)
    item = ReminderCandidate(
        CalendarEvent(
            "primary", "Main", "event", "event", "Event", "", now + timedelta(hours=1), now + timedelta(hours=2), False
        ),
        "default-5m",
        "5m",
        now,
    )
    config = RemindersConfig(calendars=[{"id": "primary"}], timezone=timezone.key)
    both_settings = Settings(
        database_path=tmp_path / "reminders.sqlite3",
        lock_path=tmp_path / "reminders.lock",
        discord_delivery_mode="both",
        discord_webhook_url="https://discord.com/api/webhooks/123456789012345678/test-webhook-token",
        discord_bot_token="token",
        discord_bot_channel_id="123",
    )
    settings = both_settings.model_copy(update={"discord_delivery_mode": "webhook"})
    both_destinations = both_settings.discord_destinations()
    database = Database(settings.database_path)
    attempt_id = database.create_attempt(item, "reminder", both_destinations)
    assert attempt_id is not None
    webhook, bot = database.due_reminder_deliveries(now, both_destinations)
    database.finish_reminder_delivery(int(webhook["id"]), ["webhook-message"], both_destinations)
    database.fail_reminder_delivery(int(bot["id"]), "DISCORD_BOT_FORBIDDEN", both_destinations)
    database.close()
    monkeypatch.setattr(pipeline, "load_reminders", lambda _: config)

    assert pipeline.retry_delivery(settings, now=now).model_dump() == {
        "status": "ok",
        "delivered": 0,
        "failed": 0,
        "failed_by_error_code": {},
        "expired": 0,
    }
    database = Database(settings.database_path)
    assert database.attempt_state(attempt_id) == "delivered"
    retired_at = database.connection.execute(
        "SELECT retired_at FROM reminder_deliveries WHERE id=?", (int(bot["id"]),)
    ).fetchone()[0]
    assert retired_at is not None
    database.close()


def test_reset_legacy_corrupt_reminder_checkpoint(tmp_path: Path) -> None:
    start = datetime(2026, 7, 8, 10, tzinfo=ZoneInfo("America/Montreal"))
    item = ReminderCandidate(
        CalendarEvent("primary", "Main", "legacy", "legacy", "Legacy", "", start, start + timedelta(hours=1), False),
        "default-5m",
        "5m",
        start - timedelta(minutes=5),
    )
    settings = Settings(
        database_path=tmp_path / "reminders.sqlite3",
        lock_path=tmp_path / "reminders.lock",
        discord_webhook_url="https://discord.com/api/webhooks/123456789012345678/test-webhook-token",
    )
    database = Database(settings.database_path)
    attempt_id = database.create_attempt(item, "legacy reminder")
    assert attempt_id is not None
    database.record_delivery_progress(attempt_id, ["partial"])
    database.fail_delivery(attempt_id, "DISCORD_MESSAGE_IDS_CORRUPT")
    database.close()

    assert pipeline.reset_reminder_checkpoint(settings, attempt_id).model_dump() == {"status": "ok", "delivery_id": attempt_id}

    database = Database(settings.database_path)
    row = database.pending_attempts()[0]
    assert (row["discord_message_ids_json"], row["last_error_code"]) == (None, None)
    database.close()


def test_reset_reminder_does_not_fall_back_to_a_colliding_legacy_attempt(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "reminders.sqlite3",
        lock_path=tmp_path / "reminders.lock",
        discord_webhook_url="https://discord.com/api/webhooks/123456789012345678/test-webhook-token",
    )
    destinations = settings.discord_destinations()
    start = datetime(2026, 7, 8, 10, tzinfo=ZoneInfo("America/Montreal"))

    def candidate(event_id: str) -> ReminderCandidate:
        event = CalendarEvent("primary", "Main", event_id, event_id, event_id, "", start, start + timedelta(hours=1), False)
        return ReminderCandidate(event, "default-5m", "5m", start - timedelta(minutes=5))

    database = Database(settings.database_path)
    legacy_id = database.create_attempt(candidate("legacy"), "legacy")
    assert legacy_id is not None
    database.record_delivery_progress(legacy_id, ["partial"])
    database.fail_delivery(legacy_id, "DISCORD_MESSAGE_IDS_CORRUPT")
    attempt_id = database.create_attempt(candidate("current"), "current", destinations)
    assert attempt_id is not None
    delivery_id = int(
        database.connection.execute("SELECT id FROM reminder_deliveries WHERE reminder_attempt_id=?", (attempt_id,)).fetchone()[
            "id"
        ]
    )
    assert delivery_id == legacy_id
    database.close()

    with pytest.raises(ValueError, match="not a failed corrupt checkpoint"):
        pipeline.reset_reminder_checkpoint(settings, delivery_id)

    database = Database(settings.database_path)
    row = database.connection.execute("SELECT last_error_code FROM reminder_attempts WHERE id=?", (legacy_id,)).fetchone()
    assert row["last_error_code"] == "DISCORD_MESSAGE_IDS_CORRUPT"
    database.close()


def test_next_day_agenda_uses_local_day_and_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    timezone = ZoneInfo("America/Montreal")
    config = RemindersConfig(calendars=[{"id": "primary"}], timezone=timezone.key)
    settings = Settings(
        database_path=tmp_path / "reminders.sqlite3",
        lock_path=tmp_path / "reminders.lock",
        discord_webhook_url="https://discord.com/api/webhooks/123456789012345678/test-webhook-token",
    )
    windows: list[tuple[datetime, datetime]] = []
    deliveries: list[str] = []

    def list_events(*args: object) -> list[object]:
        windows.append((args[-2], args[-1]))
        return []

    def deliver(*args: object, **kwargs: object) -> list[str]:
        deliveries.append(str(args[1]))
        return ["discord-id"]

    monkeypatch.setattr(pipeline, "load_reminders", lambda _: config)
    monkeypatch.setattr(pipeline, "list_events_between", list_events)
    monkeypatch.setattr(pipeline, "deliver", deliver)

    now = datetime(2026, 3, 8, 21, tzinfo=timezone)
    assert pipeline.next_day_agenda(settings, dry_run=False, force=False, scheduled=True, now=now).day == date(2026, 3, 9)
    assert pipeline.next_day_agenda(settings, dry_run=False, force=False, scheduled=True, now=now).skipped == 1
    assert pipeline.next_day_agenda(settings, dry_run=False, force=True, scheduled=True, now=now).sent == 1
    assert deliveries == [render_agenda(date(2026, 3, 9), [])] * 2
    assert windows[0] == (datetime(2026, 3, 8, 21, tzinfo=timezone), datetime(2026, 3, 15, 21, tzinfo=timezone))


def test_next_day_agenda_saves_reminders_before_invalid_discord_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    timezone = ZoneInfo("America/Montreal")
    now = datetime(2026, 7, 9, 21, tzinfo=timezone)
    config = RemindersConfig(calendars=[{"id": "primary"}], timezone=timezone.key, default_rules=[ReminderSpec(before="5m")])
    settings = Settings(
        database_path=tmp_path / "reminders.sqlite3",
        lock_path=tmp_path / "reminders.lock",
        discord_delivery_mode="bot",
    )
    event = CalendarEvent(
        "primary", "Main", "event", "event", "Event", "", now + timedelta(hours=1), now + timedelta(hours=2), False
    )
    monkeypatch.setattr(pipeline, "load_reminders", lambda _: config)
    monkeypatch.setattr(pipeline, "list_events_between", lambda *args: [event])

    with pytest.raises(DiscordDeliveryError, match="DISCORD_CONFIG_INVALID"):
        pipeline.next_day_agenda(settings, dry_run=False, force=False, now=now)

    database = Database(settings.database_path)
    assert len(database.pending_attempts()) == 1
    database.close()


def test_manual_agenda_is_idempotent_and_forceable(tmp_path: Path, monkeypatch) -> None:
    timezone = ZoneInfo("America/Montreal")
    config = RemindersConfig(calendars=[{"id": "primary"}], timezone=timezone.key)
    settings = Settings(
        database_path=tmp_path / "reminders.sqlite3",
        lock_path=tmp_path / "reminders.lock",
        discord_webhook_url="https://discord.com/api/webhooks/123456789012345678/test-webhook-token",
    )
    delivered: list[str] = []

    monkeypatch.setattr(pipeline, "load_reminders", lambda _: config)
    monkeypatch.setattr(pipeline, "list_events_between", lambda *args: [])
    monkeypatch.setattr(pipeline, "deliver", lambda *args, **kwargs: delivered.append(str(args[1])) or ["discord-id"])

    assert pipeline.agenda(settings, date(2026, 7, 9), dry_run=False).model_dump(exclude_none=True) == {
        "status": "ok",
        "sent": 1,
        "discord_message_ids": ["discord-id"],
        "events": 0,
        "delivery_succeeded": 1,
        "delivery_failed": 0,
    }
    assert pipeline.agenda(settings, date(2026, 7, 9), dry_run=False).model_dump(exclude_none=True) == {
        "status": "ok",
        "sent": 0,
        "skipped": 1,
        "events": 0,
        "delivery_succeeded": 0,
        "delivery_failed": 0,
    }
    assert pipeline.agenda(settings, date(2026, 7, 9), dry_run=False, force=True).sent == 1
    assert len(delivered) == 2


def test_agenda_retries_only_the_failed_destination(tmp_path: Path, monkeypatch) -> None:
    timezone = ZoneInfo("America/Montreal")
    config = RemindersConfig(calendars=[{"id": "primary"}], timezone=timezone.key)
    settings = Settings(
        database_path=tmp_path / "reminders.sqlite3",
        lock_path=tmp_path / "reminders.lock",
        discord_delivery_mode="both",
        discord_webhook_url="https://discord.com/api/webhooks/123456789012345678/test-webhook-token",
        discord_bot_token="token",
        discord_bot_channel_id="123",
    )
    calls: list[str] = []

    def fail_bot(destination, *args, **kwargs):
        calls.append(destination.transport)
        if destination.transport == "bot":
            raise DiscordDeliveryError("DISCORD_BOT_FORBIDDEN")
        return ["webhook-message"]

    monkeypatch.setattr(pipeline, "load_reminders", lambda _: config)
    monkeypatch.setattr(pipeline, "list_events_between", lambda *args: [])
    monkeypatch.setattr(pipeline, "deliver", fail_bot)

    result = pipeline.agenda(settings, date(2026, 7, 9), dry_run=False)
    assert (result.status, result.sent, result.delivery_failed) == ("partial", 1, 1)
    assert calls == ["webhook", "bot"]

    monkeypatch.setattr(
        pipeline, "deliver", lambda destination, *args, **kwargs: calls.append(destination.transport) or ["bot-message"]
    )
    assert pipeline.retry_agenda(settings, date(2026, 7, 9)).model_dump() == {
        "status": "ok",
        "day": date(2026, 7, 9),
        "delivered": 1,
        "failed": 0,
        "failed_by_error_code": {},
    }
    assert calls == ["webhook", "bot", "bot"]


def test_scheduled_next_day_agenda_before_2100_is_noop(calendar_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    timezone = ZoneInfo("America/Montreal")
    config = RemindersConfig(calendars=[{"id": "primary"}], timezone=timezone.key)
    settings = calendar_settings

    monkeypatch.setattr(pipeline, "load_reminders", lambda _: config)
    monkeypatch.setattr(pipeline, "list_events_between", unexpected)
    monkeypatch.setattr(pipeline, "Database", unexpected)
    monkeypatch.setattr(pipeline, "deliver", unexpected)

    assert pipeline.next_day_agenda(
        settings, dry_run=False, force=False, scheduled=True, now=datetime(2026, 3, 8, 20, 59, tzinfo=timezone)
    ).model_dump(mode="json", exclude_none=True) == {
        "status": "ok",
        "day": "2026-03-09",
        "sent": 0,
        "skipped": 1,
        "reason": "before_schedule",
        "delivery_succeeded": 0,
        "delivery_failed": 0,
    }


def test_scheduled_next_day_agenda_uses_configured_time(tmp_path: Path, monkeypatch) -> None:
    timezone = ZoneInfo("America/Montreal")
    config = RemindersConfig(calendars=[{"id": "primary"}], timezone=timezone.key)
    settings = Settings(
        database_path=tmp_path / "reminders.sqlite3",
        lock_path=tmp_path / "reminders.lock",
        agenda_schedule_time=time(20, 30),
    )

    monkeypatch.setattr(pipeline, "load_reminders", lambda _: config)
    monkeypatch.setattr(pipeline, "list_events_between", lambda *args: [])

    assert (
        pipeline.next_day_agenda(
            settings, dry_run=False, force=False, scheduled=True, now=datetime(2026, 3, 8, 20, 29, tzinfo=timezone)
        ).reason
        == "before_schedule"
    )
    result = pipeline.next_day_agenda(
        settings, dry_run=True, force=False, scheduled=True, now=datetime(2026, 3, 8, 20, 30, tzinfo=timezone)
    )
    assert isinstance(result, pipeline.AgendaPreviewResult)
    assert result.day == date(2026, 3, 9)


@pytest.mark.parametrize(
    ("now", "expected_day"),
    [
        (datetime(2026, 3, 8, 21, tzinfo=ZoneInfo("America/Montreal")), date(2026, 3, 9)),
        (datetime(2026, 12, 31, 21, tzinfo=ZoneInfo("America/Montreal")), date(2027, 1, 1)),
    ],
)
def test_next_day_agenda_uses_the_next_local_date(tmp_path: Path, monkeypatch, now: datetime, expected_day: date) -> None:
    config = RemindersConfig(calendars=[{"id": "primary"}], timezone="America/Montreal")
    settings = Settings(database_path=tmp_path / "reminders.sqlite3", lock_path=tmp_path / "reminders.lock")
    monkeypatch.setattr(pipeline, "load_reminders", lambda _: config)
    monkeypatch.setattr(pipeline, "list_events_between", lambda *args: [])

    result = pipeline.next_day_agenda(settings, dry_run=True, force=False, now=now)

    assert result.day == expected_day


def test_discover_returns_a_typed_result(calendar_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    config = RemindersConfig(calendars=[{"id": "primary"}], timezone="America/Montreal")
    settings = calendar_settings
    monkeypatch.setattr(pipeline, "load_reminders", lambda _: config)
    monkeypatch.setattr(pipeline, "list_events", lambda *args: [])

    result = pipeline.discover(settings, 7)

    assert isinstance(result, pipeline.DiscoverResult)
    assert result.model_dump() == {"status": "ok", "events": []}


def test_next_day_agenda_dry_run_skips_database_and_discord(calendar_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    config = RemindersConfig(calendars=[{"id": "primary"}], timezone="America/Montreal")
    settings = calendar_settings

    monkeypatch.setattr(pipeline, "load_reminders", lambda _: config)
    monkeypatch.setattr(pipeline, "list_events_between", lambda *args: [])
    monkeypatch.setattr(pipeline, "Database", unexpected)
    monkeypatch.setattr(pipeline, "deliver", unexpected)

    assert pipeline.next_day_agenda(settings, dry_run=True, force=False).status == "ok"


def test_next_day_agenda_includes_overlapping_events(tmp_path: Path, monkeypatch) -> None:
    timezone = ZoneInfo("America/Montreal")
    config = RemindersConfig(calendars=[{"id": "primary"}], timezone=timezone.key)
    settings = Settings(database_path=tmp_path / "reminders.sqlite3", lock_path=tmp_path / "reminders.lock")

    def event(instance_id: str, start: datetime, end: datetime) -> pipeline.CalendarEvent:
        return pipeline.CalendarEvent("primary", "Main", instance_id, instance_id, instance_id, "", start, end, False)

    events = [
        event("overlap", datetime(2026, 3, 8, 21, tzinfo=timezone), datetime(2026, 3, 9, 4, tzinfo=timezone)),
        event("within-day", datetime(2026, 3, 9, 9, tzinfo=timezone), datetime(2026, 3, 9, 10, tzinfo=timezone)),
        event("ends-at-start", datetime(2026, 3, 8, 22, tzinfo=timezone), datetime(2026, 3, 9, 0, tzinfo=timezone)),
        event("starts-at-end", datetime(2026, 3, 10, 0, tzinfo=timezone), datetime(2026, 3, 10, 1, tzinfo=timezone)),
    ]
    monkeypatch.setattr(pipeline, "load_reminders", lambda _: config)
    monkeypatch.setattr(pipeline, "list_events_between", lambda *args: events)

    result = pipeline.next_day_agenda(settings, dry_run=True, force=False, now=datetime(2026, 3, 8, 21, tzinfo=timezone))

    assert [item.title for item in result.events] == ["overlap", "within-day"]


def test_resync_cancels_overdue_job_after_an_event_changes(calendar_database: Database) -> None:
    timezone = ZoneInfo("America/Montreal")
    config = RemindersConfig(calendars=[{"id": "primary"}], default_rules=[ReminderSpec(id="default-5m", before="5m")])
    database = calendar_database
    original = pipeline.CalendarEvent(
        "primary",
        "Main",
        "event",
        "instance",
        "Original",
        "",
        datetime(2026, 7, 9, 10, tzinfo=timezone),
        datetime(2026, 7, 9, 11, tzinfo=timezone),
        False,
    )
    old_attempt = database.create_attempt(
        ReminderCandidate(original, "default-5m", "5m", datetime(2026, 7, 9, 9, 55, tzinfo=timezone)), "old content"
    )
    assert old_attempt is not None
    database.fail_delivery(old_attempt)
    updated = pipeline.CalendarEvent(
        "primary",
        "Main",
        "event",
        "instance",
        "Updated",
        "",
        datetime(2026, 7, 9, 10, 15, tzinfo=timezone),
        datetime(2026, 7, 9, 11, 15, tzinfo=timezone),
        False,
    )
    now = datetime(2026, 7, 9, 9, 56, tzinfo=timezone)

    created, cancelled = pipeline._sync_scheduled_reminders(
        database, config, [updated], now, datetime(2026, 7, 9, 12, tzinfo=timezone)
    )

    assert (created, cancelled) == (1, 1)
    assert database.attempt_state(old_attempt) == "cancelled"
    assert database.due_attempts(now) == []


def test_retry_agenda_delivers_only_the_current_destination(tmp_path: Path, monkeypatch) -> None:
    timezone = "America/Montreal"
    config = RemindersConfig(calendars=[{"id": "primary"}], timezone=timezone)
    settings = Settings(
        database_path=tmp_path / "reminders.sqlite3",
        lock_path=tmp_path / "reminders.lock",
        discord_webhook_url="https://discord.com/api/webhooks/123456789012345678/test-webhook-token",
    )
    day = date(2026, 7, 9)
    database = Database(settings.database_path)
    delivery_id = database.create_agenda_delivery(
        day, timezone, sha256(settings.discord_webhook_url.encode()).hexdigest(), "agenda"
    )
    assert delivery_id is not None
    database.fail_agenda_delivery(delivery_id)
    database.close()

    monkeypatch.setattr(pipeline, "load_reminders", lambda _: config)
    monkeypatch.setattr(pipeline, "deliver", lambda *args, **kwargs: ["discord-id"])

    assert pipeline.retry_agenda(settings, day).model_dump(mode="json") == {
        "status": "ok",
        "day": "2026-07-09",
        "delivered": 1,
        "failed": 0,
        "failed_by_error_code": {},
    }


def test_reset_agenda_checkpoint_allows_retry(tmp_path: Path, monkeypatch) -> None:
    timezone = "America/Montreal"
    config = RemindersConfig(calendars=[{"id": "primary"}], timezone=timezone)
    settings = Settings(
        database_path=tmp_path / "reminders.sqlite3",
        lock_path=tmp_path / "reminders.lock",
        discord_webhook_url="https://discord.com/api/webhooks/123456789012345678/test-webhook-token",
    )
    day = date(2026, 7, 9)
    database = Database(settings.database_path)
    delivery_id = database.create_agenda_delivery(
        day, timezone, sha256(settings.discord_webhook_url.encode()).hexdigest(), "agenda"
    )
    assert delivery_id is not None
    database.record_agenda_delivery_progress(delivery_id, ["partial"])
    database.fail_agenda_delivery(delivery_id, "DISCORD_MESSAGE_IDS_CORRUPT")
    database.close()

    assert pipeline.reset_agenda_checkpoint(settings, delivery_id).model_dump() == {
        "status": "ok",
        "delivery_id": delivery_id,
    }
    monkeypatch.setattr(pipeline, "load_reminders", lambda _: config)
    monkeypatch.setattr(pipeline, "deliver", lambda *args, **kwargs: ["discord-id"])

    assert pipeline.retry_agenda(settings, day).delivered == 1


def test_run_reads_scheduled_jobs_without_calendar_and_expires_started_events(tmp_path: Path, monkeypatch) -> None:
    timezone = ZoneInfo("America/Montreal")
    now = datetime(2026, 7, 9, 9, 56, tzinfo=timezone)
    config = RemindersConfig(calendars=[{"id": "primary"}], timezone=timezone.key)
    settings = Settings(
        database_path=tmp_path / "reminders.sqlite3",
        lock_path=tmp_path / "reminders.lock",
        discord_webhook_url="https://discord.com/api/webhooks/123456789012345678/test-webhook-token",
    )
    database = Database(settings.database_path)
    current = datetime(2026, 7, 9, 10, 0, tzinfo=timezone)
    future = pipeline.CalendarEvent("primary", "Main", "future", "future", "Future", "", current, current, False)
    past = pipeline.CalendarEvent("primary", "Main", "past", "past", "Past", "", now - timedelta(minutes=1), now, False)
    future_id = database.create_attempt(ReminderCandidate(future, "default-5m", "5m", now - timedelta(minutes=1)), "future")
    past_id = database.create_attempt(ReminderCandidate(past, "default-5m", "5m", now - timedelta(minutes=2)), "past")
    assert future_id is not None and past_id is not None
    database.close()

    delivered: list[str] = []
    monkeypatch.setattr(pipeline, "load_reminders", lambda _: config)
    monkeypatch.setattr(pipeline, "list_events_between", lambda *args: (_ for _ in ()).throw(AssertionError("no Calendar read")))
    monkeypatch.setattr(pipeline, "deliver", lambda *args, **kwargs: delivered.append(str(args[1])) or ["1"])

    assert pipeline.run(settings, dry_run=False, now=now).model_dump() == {
        "status": "ok",
        "sent": 1,
        "failed": 0,
        "failed_by_error_code": {},
        "expired": 1,
    }
    assert delivered == ["future"]
    database = Database(settings.database_path)
    assert database.attempt_state(future_id) == "delivered"
    assert database.attempt_state(past_id) == "expired"
    database.close()


def test_run_expires_started_events_without_discord_config(tmp_path: Path, monkeypatch) -> None:
    timezone = ZoneInfo("America/Montreal")
    now = datetime(2026, 7, 9, 9, 56, tzinfo=timezone)
    config = RemindersConfig(calendars=[{"id": "primary"}], timezone=timezone.key)
    settings = Settings(
        database_path=tmp_path / "reminders.sqlite3",
        lock_path=tmp_path / "reminders.lock",
        discord_delivery_mode="bot",
    )
    database = Database(settings.database_path)
    event = pipeline.CalendarEvent("primary", "Main", "past", "past", "Past", "", now - timedelta(minutes=1), now, False)
    attempt_id = database.create_attempt(ReminderCandidate(event, "default-5m", "5m", now - timedelta(minutes=2)), "past")
    assert attempt_id is not None
    database.close()

    monkeypatch.setattr(pipeline, "load_reminders", lambda _: config)

    assert pipeline.run(settings, dry_run=False, now=now).model_dump() == {
        "status": "ok",
        "sent": 0,
        "failed": 0,
        "failed_by_error_code": {},
        "expired": 1,
    }
    database = Database(settings.database_path)
    assert database.attempt_state(attempt_id) == "expired"
    database.close()
