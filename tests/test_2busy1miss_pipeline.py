from datetime import date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from two_busy_one_miss import pipeline
from two_busy_one_miss.config import EventMatch, RemindersConfig, ReminderSpec, RuleConfig, Settings
from two_busy_one_miss.pipeline import event_query_lookahead
from two_busy_one_miss.renderer import render_agenda
from two_busy_one_miss.storage import Database


def test_event_query_lookahead_covers_longest_reminder() -> None:
    config = RemindersConfig(
        calendars=[{"id": "primary"}],
        default_rules=[ReminderSpec(before="5m")],
        rules=[RuleConfig(id="long-reminder", match=EventMatch(), reminders=[ReminderSpec(before="10d")])],
    )

    assert event_query_lookahead(config, 7) == timedelta(days=10)


def test_retry_delivery_holds_process_lock(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(database_path=tmp_path / "reminders.sqlite3", lock_path=tmp_path / "reminders.lock")
    database = MagicMock()
    database.pending_attempts.return_value = [{"id": 1, "content": "content"}]
    lock = MagicMock()
    process_lock = MagicMock(return_value=lock)
    monkeypatch.setattr(pipeline, "Database", MagicMock(return_value=database))
    monkeypatch.setattr(pipeline, "ProcessLock", process_lock)

    def fake_deliver(*args: object) -> list[str]:
        assert lock.__enter__.called
        return ["discord-id"]

    monkeypatch.setattr(pipeline, "deliver", fake_deliver)

    assert pipeline.retry_delivery(settings) == {"status": "ok", "delivered": 1}
    process_lock.assert_called_once_with(settings.lock_path)
    database.finish_delivery.assert_called_once_with(1, ["discord-id"])


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

    def deliver(*args: object) -> list[str]:
        deliveries.append(str(args[1]))
        return ["discord-id"]

    monkeypatch.setattr(pipeline, "datetime", FixedDatetime)
    monkeypatch.setattr(pipeline, "load_reminders", lambda _: config)
    monkeypatch.setattr(pipeline, "list_events_between", list_events)
    monkeypatch.setattr(pipeline, "deliver", deliver)

    assert pipeline.next_day_agenda(settings, dry_run=False, force=False, scheduled=True)["day"] == "2026-03-09"
    assert pipeline.next_day_agenda(settings, dry_run=False, force=False, scheduled=True)["skipped"] == 1
    assert pipeline.next_day_agenda(settings, dry_run=False, force=True, scheduled=True)["sent"] == 1
    assert deliveries == [render_agenda(date(2026, 3, 9), [])] * 2
    assert windows[0] == (
        datetime(2026, 3, 9, 0, tzinfo=timezone),
        datetime(2026, 3, 10, 0, tzinfo=timezone),
    )


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

    assert pipeline.next_day_agenda(settings, dry_run=False, force=False, scheduled=True) == {
        "status": "ok",
        "day": "2026-03-09",
        "sent": 0,
        "skipped": 1,
        "reason": "before_schedule",
    }
    calendar.assert_not_called()
    database.assert_not_called()
    deliver.assert_not_called()


def test_next_day_agenda_dry_run_skips_database_and_discord(tmp_path: Path, monkeypatch) -> None:
    config = RemindersConfig(calendars=[{"id": "primary"}], timezone="America/Montreal")
    settings = Settings(database_path=tmp_path / "reminders.sqlite3", lock_path=tmp_path / "reminders.lock")
    database = MagicMock()
    deliver = MagicMock()

    monkeypatch.setattr(pipeline, "load_reminders", lambda _: config)
    monkeypatch.setattr(pipeline, "list_events_between", lambda *args: [])
    monkeypatch.setattr(pipeline, "Database", database)
    monkeypatch.setattr(pipeline, "deliver", deliver)

    assert pipeline.next_day_agenda(settings, dry_run=True, force=False)["status"] == "ok"
    database.assert_not_called()
    deliver.assert_not_called()


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
    monkeypatch.setattr(pipeline, "deliver", lambda *args: ["discord-id"])

    assert pipeline.retry_agenda(settings, day) == {"status": "ok", "day": "2026-07-09", "delivered": 1}
