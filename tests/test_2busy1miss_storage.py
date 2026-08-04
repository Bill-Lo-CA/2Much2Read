import os
import stat
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from two_busy_one_miss.google_calendar import CalendarEvent
from two_busy_one_miss.rules import ReminderCandidate
from two_busy_one_miss.storage import Database
from two_read_runtime.discord import configured_destinations


def candidate(event_id: str = "event-1") -> ReminderCandidate:
    timezone = ZoneInfo("America/Montreal")
    start = datetime(2026, 7, 8, 10, 0, tzinfo=timezone)
    event = CalendarEvent(
        calendar_id="primary",
        calendar_name="Main",
        event_id=event_id,
        instance_id=event_id,
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
    assert database.counts() == {"events": 1, "reminder_attempts": 1, "reminder_deliveries": 0}

    database.finish_delivery(attempt_id, ["123"])
    assert database.attempt_state(attempt_id) == "delivered"
    database.close()


def test_existing_permissive_database_is_repaired(tmp_path: Path) -> None:
    path = tmp_path / "test.sqlite3"
    database = Database(path)
    database.close()
    os.chmod(path, 0o644)

    database = Database(path)
    database.close()

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_read_only_database_does_not_prepare_or_repair_path(tmp_path: Path) -> None:
    path = tmp_path / "test.sqlite3"
    writable = Database(path)
    writable.close()
    os.chmod(path, 0o644)

    read_only = Database(path, read_only=True)
    read_only.close()

    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_create_attempts_batches_distinct_candidates(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.sqlite3")

    assert database.create_attempts([(candidate("one"), "one"), (candidate("two"), "two")]) == 2
    assert database.counts() == {"events": 2, "reminder_attempts": 2, "reminder_deliveries": 0}
    database.close()


def test_reminder_destinations_retry_independently(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    item = candidate()
    destinations = configured_destinations(
        "both", "https://discord.com/api/webhooks/123456789012345678/test-webhook-token", "token", "123"
    )
    attempt_id = database.create_attempt(item, "message", destinations)
    assert attempt_id is not None

    deliveries = database.due_reminder_deliveries(item.reminder_time + timedelta(minutes=1), destinations)
    assert len(deliveries) == 2
    database.finish_reminder_delivery(int(deliveries[0]["id"]), ["webhook-message"], destinations)
    database.fail_reminder_delivery(int(deliveries[1]["id"]), "DISCORD_BOT_FORBIDDEN", destinations)
    assert database.attempt_state(attempt_id) == "failed"

    pending = database.due_reminder_deliveries(item.reminder_time + timedelta(minutes=1), destinations)
    assert [row["id"] for row in pending] == [deliveries[1]["id"]]
    database.finish_reminder_delivery(int(deliveries[1]["id"]), ["bot-message"], destinations)
    assert database.attempt_state(attempt_id) == "delivered"
    database.close()


def test_failed_attempt_is_retryable(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    attempt_id = database.create_attempt(candidate(), "message")
    assert attempt_id is not None

    database.fail_delivery(attempt_id)

    assert [int(row["id"]) for row in database.pending_attempts()] == [attempt_id]
    database.close()


def test_corrupt_attempt_checkpoint_can_be_reset_explicitly(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    attempt_id = database.create_attempt(candidate(), "message")
    assert attempt_id is not None
    database.record_delivery_progress(attempt_id, ["partial"])
    database.fail_delivery(attempt_id, "DISCORD_MESSAGE_IDS_CORRUPT")

    assert database.reset_corrupt_delivery(attempt_id)
    row = database.pending_attempts()[0]
    assert (row["state"], row["discord_message_ids_json"], row["last_error_code"]) == ("pending", None, None)
    assert not database.reset_corrupt_delivery(attempt_id)
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
    assert database.cancel_unmatched_attempts([], [item.event], item.reminder_time, item.event.start) == 1
    assert database.attempt_state(attempt_id) == "cancelled"
    database.close()


def test_reminder_checkpoint_is_reset_for_a_new_destination(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    attempt_id = database.create_attempt(candidate(), "message")
    assert attempt_id is not None

    database.record_delivery_progress(attempt_id, ["webhook-message"], "webhook:old")

    assert database.delivery_checkpoint(attempt_id, "bot:123") is None
    row = database.pending_attempts()[0]
    assert (row["discord_message_ids_json"], row["discord_destination_key"]) == (None, "bot:123")
    database.close()


def test_reminder_checkpoint_with_legacy_webhook_marker_is_reset(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    attempt_id = database.create_attempt(candidate(), "message")
    assert attempt_id is not None

    database.record_delivery_progress(attempt_id, ["webhook-message"], "webhook")

    assert database.delivery_checkpoint(attempt_id, "webhook:new") is None
    row = database.pending_attempts()[0]
    assert (row["discord_message_ids_json"], row["discord_destination_key"]) == (None, "webhook:new")
    database.close()


def test_migrating_legacy_reminder_does_not_adopt_unknown_webhook_checkpoint(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    attempt_id = database.create_attempt(candidate(), "message")
    assert attempt_id is not None
    database.record_delivery_progress(attempt_id, ["webhook-message"], "webhook")
    database.fail_delivery(attempt_id)
    destinations = configured_destinations(
        "both", "https://discord.com/api/webhooks/123456789012345678/test-webhook-token", "token", "123"
    )

    database.migrate_legacy_reminder_deliveries(attempt_id, destinations)

    rows = database.connection.execute(
        "SELECT discord_message_ids_json FROM reminder_deliveries WHERE reminder_attempt_id=? ORDER BY id", (attempt_id,)
    ).fetchall()
    assert [row["discord_message_ids_json"] for row in rows] == [None, None]
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


def test_corrupt_agenda_checkpoint_can_be_reset_explicitly(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    delivery_id = database.create_agenda_delivery(date(2026, 7, 9), "America/Montreal", "destination", "agenda")
    assert delivery_id is not None
    database.record_agenda_delivery_progress(delivery_id, ["partial"])
    database.fail_agenda_delivery(delivery_id, "DISCORD_MESSAGE_IDS_CORRUPT")

    assert database.reset_corrupt_agenda_delivery(delivery_id)
    row = database.pending_agenda_deliveries(date(2026, 7, 9), "America/Montreal", "destination")[0]
    assert (row["state"], row["discord_message_ids_json"], row["last_error_code"]) == ("pending", None, None)
    assert not database.reset_corrupt_agenda_delivery(delivery_id)
    database.close()
