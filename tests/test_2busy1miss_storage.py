import os
import stat
from datetime import UTC, date, datetime, timedelta
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


MONTREAL = ZoneInfo("America/Montreal")


def _candidate_at(reminder_time: datetime, event_id: str = "event-1", rule_id: str = "default-5m") -> ReminderCandidate:
    start = reminder_time + timedelta(minutes=5)
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
    return ReminderCandidate(event=event, rule_id=rule_id, before="5m", reminder_time=reminder_time)


def test_a_reminder_from_before_the_dst_change_is_still_due_after_it(tmp_path: Path) -> None:
    """reminder_at is compared as text, so both sides have to be in one zone to be comparable.

    On 2026-11-01 Montreal repeats 01:00-02:00, once at -04:00 and once at -05:00. A reminder due
    at 01:45 EDT is 05:45Z; at 01:30 EST, which is 06:30Z, it is three quarters of an hour overdue.
    Stored with its own offset the text comparison read `01:45:00-04:00 <= 01:30:00-05:00` and
    answered no, holding the reminder back until the wall clock caught up.
    """
    database = Database(tmp_path / "test.sqlite3")
    due = datetime(2026, 11, 1, 1, 45, tzinfo=MONTREAL, fold=0)
    assert due.utcoffset() == timedelta(hours=-4)
    database.create_attempt(_candidate_at(due), "message")

    now = datetime(2026, 11, 1, 1, 30, tzinfo=MONTREAL, fold=1)
    assert now.utcoffset() == timedelta(hours=-5)
    # Compared as instants. Two aware datetimes in the same zone compare by wall clock and ignore
    # fold (PEP 495), which is the same confusion in the language that the columns had on disk.
    assert now.astimezone(UTC) > due.astimezone(UTC)

    assert [str(row["reminder_at"]) for row in database.due_attempts(now)] == ["2026-11-01T05:45:00+00:00"]
    database.close()


def test_reminders_in_two_zones_are_ordered_by_instant(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    later = datetime(2026, 11, 1, 1, 30, tzinfo=MONTREAL, fold=1)  # 06:30Z
    earlier = datetime(2026, 11, 1, 1, 45, tzinfo=MONTREAL, fold=0)  # 05:45Z
    database.create_attempt(_candidate_at(later, event_id="event-later"), "later")
    database.create_attempt(_candidate_at(earlier, event_id="event-earlier"), "earlier")

    ordered = [str(row["content"]) for row in database.pending_attempts()]

    assert ordered == ["earlier", "later"]
    database.close()


def test_opening_an_older_database_normalises_its_timestamps(tmp_path: Path) -> None:
    path = tmp_path / "test.sqlite3"
    database = Database(path)
    database.create_attempt(_candidate_at(datetime(2026, 7, 8, 9, 55, tzinfo=MONTREAL)), "message")
    # Put the rows back the way the previous version wrote them.
    database.connection.execute("UPDATE reminder_attempts SET reminder_at='2026-07-08T09:55:00-04:00'")
    database.connection.execute("UPDATE events SET start_at='2026-07-08T10:00:00-04:00', end_at='2026-07-08T11:00:00-04:00'")
    database.connection.commit()
    database.close()

    reopened = Database(path)

    assert str(reopened.connection.execute("SELECT reminder_at FROM reminder_attempts").fetchone()[0]) == (
        "2026-07-08T13:55:00+00:00"
    )
    assert tuple(reopened.connection.execute("SELECT start_at,end_at FROM events").fetchone()) == (
        "2026-07-08T14:00:00+00:00",
        "2026-07-08T15:00:00+00:00",
    )
    reopened.close()


def test_normalising_collapses_two_offsets_naming_one_instant_keeping_the_delivered_copy(tmp_path: Path) -> None:
    """A re-sync across a DST boundary can record one reminder under two offsets.

    Normalised they collide on UNIQUE(calendar_id,event_id,instance_id,rule_id,reminder_at). They
    are the same reminder, so one has to go, and it must not be the one already sent - otherwise
    the survivor is pending and the reminder goes out twice.
    """
    path = tmp_path / "test.sqlite3"
    database = Database(path)
    delivered_id = database.create_attempt(_candidate_at(datetime(2026, 7, 8, 9, 55, tzinfo=MONTREAL)), "delivered copy")
    assert delivered_id is not None
    database.finish_delivery(delivered_id, ["123"])
    database.connection.execute("UPDATE reminder_attempts SET reminder_at='2026-07-08T13:55:00+00:00'")
    database.connection.execute(
        """INSERT INTO reminder_attempts
        (event_row_id,calendar_id,event_id,instance_id,rule_id,reminder_at,content,state,created_at,updated_at)
        SELECT event_row_id,calendar_id,event_id,instance_id,rule_id,'2026-07-08T09:55:00-04:00','pending copy',
               'pending',created_at,updated_at FROM reminder_attempts WHERE id=?""",
        (delivered_id,),
    )
    database.connection.commit()
    database.close()

    reopened = Database(path)

    rows = reopened.connection.execute("SELECT content,state,reminder_at FROM reminder_attempts").fetchall()
    assert [tuple(row) for row in rows] == [("delivered copy", "delivered", "2026-07-08T13:55:00+00:00")]
    reopened.close()
