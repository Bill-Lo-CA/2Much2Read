from datetime import date, datetime, time, timedelta
from hashlib import sha256
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from two_busy_one_miss import pipeline
from two_busy_one_miss.config import EventMatch, RemindersConfig, ReminderSpec, RuleConfig, Settings
from two_busy_one_miss.google_calendar import CalendarClient
from two_busy_one_miss.pipeline import event_query_lookahead
from two_busy_one_miss.renderer import render_agenda
from two_busy_one_miss.rules import ReminderCandidate
from two_busy_one_miss.storage import Database
from two_read_runtime.discord import DiscordDeliveryError


def test_event_query_lookahead_covers_longest_reminder() -> None:
    config = RemindersConfig(
        calendars=[{"id": "primary"}],
        default_rules=[ReminderSpec(before="5m")],
        rules=[RuleConfig(id="long-reminder", match=EventMatch(), reminders=[ReminderSpec(before="10d")])],
    )

    assert event_query_lookahead(config, 7) == timedelta(days=10)


def test_calendar_client_requests_conference_data() -> None:
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
        conferenceDataVersion=1,
        pageToken=None,
    )


def test_retry_delivery_holds_process_lock(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(database_path=tmp_path / "reminders.sqlite3", lock_path=tmp_path / "reminders.lock")
    config = RemindersConfig(calendars=[{"id": "primary"}], timezone="America/Montreal")
    database = MagicMock()
    database.due_attempts.return_value = [
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
    lock = MagicMock()
    process_lock = MagicMock(return_value=lock)
    monkeypatch.setattr(pipeline, "Database", MagicMock(return_value=database))
    monkeypatch.setattr(pipeline, "ProcessLock", process_lock)
    monkeypatch.setattr(pipeline, "load_reminders", lambda _: config)

    def fake_deliver(*args: object) -> list[str]:
        assert lock.__enter__.called
        if args[1] == "bad":
            raise DiscordDeliveryError("delivery failed")
        return ["discord-id"]

    monkeypatch.setattr(pipeline, "deliver", fake_deliver)

    assert pipeline.retry_delivery(settings).model_dump() == {
        "status": "ok",
        "delivered": 1,
        "failed": 1,
        "failed_by_error_code": {"DISCORD_DELIVERY_FAILED": 1},
        "expired": 0,
    }
    process_lock.assert_called_once_with(settings.lock_path)
    database.fail_delivery.assert_called_once_with(1, "DISCORD_DELIVERY_FAILED")
    database.finish_delivery.assert_called_once_with(2, ["discord-id"])


def test_next_day_agenda_uses_local_day_and_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    timezone = ZoneInfo("America/Montreal")
    config = RemindersConfig(calendars=[{"id": "primary"}], timezone=timezone.key)
    settings = Settings(
        database_path=tmp_path / "reminders.sqlite3",
        lock_path=tmp_path / "reminders.lock",
        discord_webhook_url="https://busy.example/webhook",
    )
    windows: list[tuple[datetime, datetime]] = []
    deliveries: list[str] = []

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 3, 8, 21, tzinfo=tz)

    def list_events(*args: object) -> list[object]:
        windows.append((args[-2], args[-1]))
        return []

    def deliver(*args: object, **kwargs: object) -> list[str]:
        deliveries.append(str(args[1]))
        return ["discord-id"]

    monkeypatch.setattr(pipeline, "datetime", FixedDatetime)
    monkeypatch.setattr(pipeline, "load_reminders", lambda _: config)
    monkeypatch.setattr(pipeline, "list_events_between", list_events)
    monkeypatch.setattr(pipeline, "deliver", deliver)

    assert pipeline.next_day_agenda(settings, dry_run=False, force=False, scheduled=True).day == date(2026, 3, 9)
    assert pipeline.next_day_agenda(settings, dry_run=False, force=False, scheduled=True).skipped == 1
    assert pipeline.next_day_agenda(settings, dry_run=False, force=True, scheduled=True).sent == 1
    assert deliveries == [render_agenda(date(2026, 3, 9), [])] * 2
    assert windows[0] == (datetime(2026, 3, 8, 21, tzinfo=timezone), datetime(2026, 3, 15, 21, tzinfo=timezone))


def test_manual_agenda_is_idempotent_and_forceable(tmp_path: Path, monkeypatch) -> None:
    timezone = ZoneInfo("America/Montreal")
    config = RemindersConfig(calendars=[{"id": "primary"}], timezone=timezone.key)
    settings = Settings(
        database_path=tmp_path / "reminders.sqlite3",
        lock_path=tmp_path / "reminders.lock",
        discord_webhook_url="https://busy.example/webhook",
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
    }
    assert pipeline.agenda(settings, date(2026, 7, 9), dry_run=False).model_dump(exclude_none=True) == {
        "status": "ok",
        "sent": 0,
        "skipped": 1,
        "events": 0,
    }
    assert pipeline.agenda(settings, date(2026, 7, 9), dry_run=False, force=True).sent == 1
    assert len(delivered) == 2


def test_scheduled_next_day_agenda_before_2100_is_noop(tmp_path: Path, monkeypatch) -> None:
    timezone = ZoneInfo("America/Montreal")
    config = RemindersConfig(calendars=[{"id": "primary"}], timezone=timezone.key)
    settings = Settings(database_path=tmp_path / "reminders.sqlite3", lock_path=tmp_path / "reminders.lock")
    calendar = MagicMock()
    database = MagicMock()
    deliver = MagicMock()

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 3, 8, 8, tzinfo=tz)

    monkeypatch.setattr(pipeline, "datetime", FixedDatetime)
    monkeypatch.setattr(pipeline, "load_reminders", lambda _: config)
    monkeypatch.setattr(pipeline, "list_events_between", calendar)
    monkeypatch.setattr(pipeline, "Database", database)
    monkeypatch.setattr(pipeline, "deliver", deliver)

    assert pipeline.next_day_agenda(settings, dry_run=False, force=False, scheduled=True).model_dump(
        mode="json", exclude_none=True
    ) == {
        "status": "ok",
        "day": "2026-03-09",
        "sent": 0,
        "skipped": 1,
        "reason": "before_schedule",
    }
    calendar.assert_not_called()
    database.assert_not_called()
    deliver.assert_not_called()


def test_scheduled_next_day_agenda_uses_configured_time(tmp_path: Path, monkeypatch) -> None:
    timezone = ZoneInfo("America/Montreal")
    config = RemindersConfig(calendars=[{"id": "primary"}], timezone=timezone.key)
    settings = Settings(
        database_path=tmp_path / "reminders.sqlite3",
        lock_path=tmp_path / "reminders.lock",
        agenda_schedule_time=time(20, 30),
    )

    class FixedDatetime(datetime):
        current = datetime(2026, 3, 8, 20, 29, tzinfo=timezone)

        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz)

    monkeypatch.setattr(pipeline, "datetime", FixedDatetime)
    monkeypatch.setattr(pipeline, "load_reminders", lambda _: config)
    monkeypatch.setattr(pipeline, "list_events_between", lambda *args: [])

    assert pipeline.next_day_agenda(settings, dry_run=False, force=False, scheduled=True).reason == "before_schedule"
    FixedDatetime.current = datetime(2026, 3, 8, 20, 30, tzinfo=timezone)
    result = pipeline.next_day_agenda(settings, dry_run=True, force=False, scheduled=True)
    assert isinstance(result, pipeline.AgendaPreviewResult)
    assert result.day == date(2026, 3, 9)


def test_discover_returns_a_typed_result(tmp_path: Path, monkeypatch) -> None:
    config = RemindersConfig(calendars=[{"id": "primary"}], timezone="America/Montreal")
    settings = Settings(database_path=tmp_path / "reminders.sqlite3", lock_path=tmp_path / "reminders.lock")
    monkeypatch.setattr(pipeline, "load_reminders", lambda _: config)
    monkeypatch.setattr(pipeline, "list_events", lambda *args: [])

    result = pipeline.discover(settings, 7)

    assert isinstance(result, pipeline.DiscoverResult)
    assert result.model_dump() == {"status": "ok", "events": []}


def test_next_day_agenda_dry_run_skips_database_and_discord(tmp_path: Path, monkeypatch) -> None:
    config = RemindersConfig(calendars=[{"id": "primary"}], timezone="America/Montreal")
    settings = Settings(database_path=tmp_path / "reminders.sqlite3", lock_path=tmp_path / "reminders.lock")
    database = MagicMock()
    deliver = MagicMock()

    monkeypatch.setattr(pipeline, "load_reminders", lambda _: config)
    monkeypatch.setattr(pipeline, "list_events_between", lambda *args: [])
    monkeypatch.setattr(pipeline, "Database", database)
    monkeypatch.setattr(pipeline, "deliver", deliver)

    assert pipeline.next_day_agenda(settings, dry_run=True, force=False).status == "ok"
    database.assert_not_called()
    deliver.assert_not_called()


def test_next_day_agenda_includes_overlapping_events(tmp_path: Path, monkeypatch) -> None:
    timezone = ZoneInfo("America/Montreal")
    config = RemindersConfig(calendars=[{"id": "primary"}], timezone=timezone.key)
    settings = Settings(database_path=tmp_path / "reminders.sqlite3", lock_path=tmp_path / "reminders.lock")

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 3, 8, 21, tzinfo=tz)

    def event(instance_id: str, start: datetime, end: datetime) -> pipeline.CalendarEvent:
        return pipeline.CalendarEvent("primary", "Main", instance_id, instance_id, instance_id, "", start, end, False)

    events = [
        event("overlap", datetime(2026, 3, 8, 21, tzinfo=timezone), datetime(2026, 3, 9, 4, tzinfo=timezone)),
        event("within-day", datetime(2026, 3, 9, 9, tzinfo=timezone), datetime(2026, 3, 9, 10, tzinfo=timezone)),
        event("ends-at-start", datetime(2026, 3, 8, 22, tzinfo=timezone), datetime(2026, 3, 9, 0, tzinfo=timezone)),
        event("starts-at-end", datetime(2026, 3, 10, 0, tzinfo=timezone), datetime(2026, 3, 10, 1, tzinfo=timezone)),
    ]
    monkeypatch.setattr(pipeline, "datetime", FixedDatetime)
    monkeypatch.setattr(pipeline, "load_reminders", lambda _: config)
    monkeypatch.setattr(pipeline, "list_events_between", lambda *args: events)

    result = pipeline.next_day_agenda(settings, dry_run=True, force=False)

    assert [item.title for item in result.events] == ["overlap", "within-day"]


def test_resync_cancels_overdue_job_after_an_event_changes(tmp_path: Path) -> None:
    timezone = ZoneInfo("America/Montreal")
    config = RemindersConfig(calendars=[{"id": "primary"}], default_rules=[ReminderSpec(id="default-5m", before="5m")])
    database = Database(tmp_path / "reminders.sqlite3")
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
    database.close()


def test_retry_agenda_delivers_only_the_current_destination(tmp_path: Path, monkeypatch) -> None:
    timezone = "America/Montreal"
    config = RemindersConfig(calendars=[{"id": "primary"}], timezone=timezone)
    settings = Settings(
        database_path=tmp_path / "reminders.sqlite3",
        lock_path=tmp_path / "reminders.lock",
        discord_webhook_url="https://busy.example/webhook",
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


def test_run_reads_scheduled_jobs_without_calendar_and_expires_started_events(tmp_path: Path, monkeypatch) -> None:
    timezone = ZoneInfo("America/Montreal")
    now = datetime(2026, 7, 9, 9, 56, tzinfo=timezone)
    config = RemindersConfig(calendars=[{"id": "primary"}], timezone=timezone.key)
    settings = Settings(
        database_path=tmp_path / "reminders.sqlite3",
        lock_path=tmp_path / "reminders.lock",
        discord_webhook_url="https://busy.example/webhook",
    )
    database = Database(settings.database_path)
    current = datetime(2026, 7, 9, 10, 0, tzinfo=timezone)
    future = pipeline.CalendarEvent("primary", "Main", "future", "future", "Future", "", current, current, False)
    past = pipeline.CalendarEvent("primary", "Main", "past", "past", "Past", "", now - timedelta(minutes=1), now, False)
    future_id = database.create_attempt(ReminderCandidate(future, "default-5m", "5m", now - timedelta(minutes=1)), "future")
    past_id = database.create_attempt(ReminderCandidate(past, "default-5m", "5m", now - timedelta(minutes=2)), "past")
    assert future_id is not None and past_id is not None
    database.close()

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    delivered: list[str] = []
    monkeypatch.setattr(pipeline, "datetime", FixedDatetime)
    monkeypatch.setattr(pipeline, "load_reminders", lambda _: config)
    monkeypatch.setattr(pipeline, "list_events_between", lambda *args: (_ for _ in ()).throw(AssertionError("no Calendar read")))
    monkeypatch.setattr(pipeline, "deliver", lambda *args, **kwargs: delivered.append(str(args[1])) or ["1"])

    assert pipeline.run(settings, dry_run=False).model_dump() == {
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
