from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock

from two_busy_one_miss import pipeline
from two_busy_one_miss.config import EventMatch, RemindersConfig, ReminderSpec, RuleConfig, Settings
from two_busy_one_miss.pipeline import event_query_lookahead


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
