import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from two_busy_one_miss.google_calendar import CalendarEvent
from two_busy_one_miss.rules import ReminderCandidate
from two_busy_one_miss.storage import Database


def candidate() -> ReminderCandidate:
    timezone = ZoneInfo("America/Montreal")
    start = datetime(2026, 7, 8, 10, 0, tzinfo=timezone)
    event = CalendarEvent(
        calendar_id="primary",
        calendar_name="Main",
        event_id="event-1",
        instance_id="event-1",
        title="French class",
        location="Room 1",
        start=start,
        end=start + timedelta(hours=1),
        all_day=False,
    )
    return ReminderCandidate(event=event, rule_id="default-5m", before="5m", reminder_time=start - timedelta(minutes=5))


def test_attempt_idempotency_and_delivery_state(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    item = candidate()

    attempt_id = database.create_attempt(item, "message")
    assert attempt_id is not None
    assert database.create_attempt(item, "message") is None
    assert database.counts() == {"events": 1, "reminder_attempts": 1}

    database.finish_delivery(attempt_id, ["123"])
    assert database.attempt_state(attempt_id) == "delivered"
    database.close()


def test_failed_attempt_is_retryable(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    attempt_id = database.create_attempt(candidate(), "message")
    assert attempt_id is not None

    database.fail_delivery(attempt_id)

    assert [int(row["id"]) for row in database.pending_attempts()] == [attempt_id]
    database.close()


def test_attempt_progress_is_preserved_and_unmatched_jobs_are_cancelled(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    item = candidate()
    attempt_id = database.create_attempt(item, "message")
    assert attempt_id is not None

    database.record_delivery_progress(attempt_id, ["chunk-1"])
    database.fail_delivery(attempt_id)
    row = database.pending_attempts()[0]
    assert row["discord_message_ids_json"] == '["chunk-1"]'
    assert database.cancel_unmatched_attempts([], item.reminder_time, item.event.start) == 1
    assert database.attempt_state(attempt_id) == "cancelled"
    database.close()


def test_resync_updates_pending_attempt_content(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    item = candidate()
    attempt_id = database.create_attempt(item, "old content")
    assert attempt_id is not None
    database.record_delivery_progress(attempt_id, ["old-chunk"])

    assert database.create_attempt(item, "new content") is None
    row = database.pending_attempts()[0]
    assert row["content"] == "new content"
    assert row["discord_message_ids_json"] is None
    database.close()


def test_migrates_legacy_reminder_attempt_states(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
CREATE TABLE events(id INTEGER PRIMARY KEY);
INSERT INTO events(id) VALUES(1);
CREATE TABLE reminder_attempts(
  id INTEGER PRIMARY KEY, event_row_id INTEGER NOT NULL, calendar_id TEXT NOT NULL, event_id TEXT NOT NULL,
  instance_id TEXT NOT NULL, rule_id TEXT NOT NULL, reminder_at TEXT NOT NULL, content TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('pending','delivered','failed')), attempt_count INTEGER NOT NULL DEFAULT 0,
  discord_message_ids_json TEXT, delivered_at TEXT, last_error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
INSERT INTO reminder_attempts
(event_row_id,calendar_id,event_id,instance_id,rule_id,reminder_at,content,state,created_at,updated_at)
VALUES(1,'primary','event','instance','rule','2026-07-08T09:55:00+00:00','message','failed','now','now');
"""
    )
    connection.close()

    database = Database(path)

    assert database.attempt_state(1) == "failed"
    database.expire_attempt(1)
    assert database.attempt_state(1) == "expired"
    database.close()


def test_agenda_delivery_is_idempotent_and_forceable(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    day = date(2026, 7, 9)
    delivery_id = database.create_agenda_delivery(day, "America/Montreal", "destination", "agenda")

    assert delivery_id is not None
    assert database.create_agenda_delivery(day, "America/Montreal", "destination", "agenda") is None

    database.fail_agenda_delivery(delivery_id)
    assert [int(row["id"]) for row in database.pending_agenda_deliveries(day, "America/Montreal", "destination")] == [delivery_id]

    database.finish_agenda_delivery(delivery_id, ["123"])
    assert database.agenda_delivery_state(delivery_id) == "delivered"
    assert database.create_agenda_delivery(day, "America/Montreal", "destination", "agenda", force=True) == delivery_id
    assert database.agenda_delivery_state(delivery_id) == "pending"
    database.close()
