import json
import os
import sqlite3
import stat
import threading
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from two_busy_one_miss.google_calendar import CalendarEvent
from two_busy_one_miss.rules import ReminderCandidate
from two_busy_one_miss.storage import Database
from two_read_runtime.discord import DiscordDestination, configured_destinations


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


def _twin(database: Database, delivered_at_offset: str, pending_at_offset: str) -> int:
    """One reminder written twice, once per offset, with the delivered copy carrying a checkpoint.

    Returns the id of the delivered row so a test can check it, and its deliveries, are still there.
    """
    delivered_id = database.create_attempt(
        _candidate_at(datetime(2026, 7, 8, 9, 55, tzinfo=MONTREAL)),
        "delivered copy",
        configured_destinations("webhook", "https://discord.com/api/webhooks/123456789012345678/test-webhook-token", "", ""),
    )
    assert delivered_id is not None
    database.finish_delivery(delivered_id, ["123"])
    database.connection.execute("UPDATE reminder_attempts SET reminder_at=? WHERE id=?", (delivered_at_offset, delivered_id))
    database.connection.execute(
        """INSERT INTO reminder_attempts
        (event_row_id,calendar_id,event_id,instance_id,rule_id,reminder_at,content,state,created_at,updated_at)
        SELECT event_row_id,calendar_id,event_id,instance_id,rule_id,?,'pending copy','pending',created_at,updated_at
        FROM reminder_attempts WHERE id=?""",
        (pending_at_offset, delivered_id),
    )
    database.connection.commit()
    return delivered_id


def test_normalising_keeps_the_delivered_copy_when_a_pending_row_already_holds_the_key(tmp_path: Path) -> None:
    """The occupant can be a row already in UTC, which therefore never enters the conversion loop.

    Ordering the rows cannot help there: the occupant reached the key without being converted. A
    handler that dropped whichever row it was updating would delete the delivered copy, take its
    delivery checkpoints with it through ON DELETE CASCADE, and leave a pending row behind to send
    the reminder a second time.
    """
    path = tmp_path / "test.sqlite3"
    database = Database(path)
    delivered_id = _twin(database, "2026-07-08T09:55:00-04:00", "2026-07-08T13:55:00+00:00")
    database.close()

    reopened = Database(path)

    rows = reopened.connection.execute("SELECT id,content,state,reminder_at FROM reminder_attempts").fetchall()
    assert [(row["content"], row["state"], row["reminder_at"]) for row in rows] == [
        ("delivered copy", "delivered", "2026-07-08T13:55:00+00:00")
    ]
    assert int(rows[0]["id"]) == delivered_id
    assert (
        reopened.connection.execute(
            "SELECT COUNT(*) FROM reminder_deliveries WHERE reminder_attempt_id=?", (delivered_id,)
        ).fetchone()[0]
        == 1
    )
    reopened.close()


def test_normalising_drops_the_pending_copy_when_the_delivered_one_is_already_canonical(tmp_path: Path) -> None:
    """The mirror case: the row needing conversion is the one that has got least far."""
    path = tmp_path / "test.sqlite3"
    database = Database(path)
    delivered_id = _twin(database, "2026-07-08T13:55:00+00:00", "2026-07-08T09:55:00-04:00")
    database.close()

    reopened = Database(path)

    rows = reopened.connection.execute("SELECT id,content,state,reminder_at FROM reminder_attempts").fetchall()
    assert [(row["content"], row["state"], row["reminder_at"]) for row in rows] == [
        ("delivered copy", "delivered", "2026-07-08T13:55:00+00:00")
    ]
    assert int(rows[0]["id"]) == delivered_id
    reopened.close()


WEBHOOK = "https://discord.com/api/webhooks/123456789012345678/test-webhook-token"


def test_a_dry_run_on_an_unmigrated_database_reads_the_same_instants(tmp_path: Path) -> None:
    """Database.reading skips the migration on purpose, so due_attempts must not need it.

    A legacy `09:55:00-04:00` row sorts before `13:50:00+00:00` as text, so a dry run five minutes
    short of the reminder reported it as due.
    """
    path = tmp_path / "test.sqlite3"
    database = Database(path)
    database.create_attempt(_candidate_at(datetime(2026, 7, 8, 9, 55, tzinfo=MONTREAL)), "message")
    database.connection.execute("UPDATE reminder_attempts SET reminder_at='2026-07-08T09:55:00-04:00'")
    database.connection.commit()
    database.close()

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        reading = Database.reading(connection)
        five_minutes_early = datetime(2026, 7, 8, 13, 50, tzinfo=UTC)
        on_time = datetime(2026, 7, 8, 13, 55, tzinfo=UTC)

        assert reading.due_attempts(five_minutes_early) == []
        assert [str(row["content"]) for row in reading.due_attempts(on_time)] == ["message"]
        # Untouched: a read must not migrate.
        assert str(connection.execute("SELECT reminder_at FROM reminder_attempts").fetchone()[0]) == ("2026-07-08T09:55:00-04:00")
    finally:
        connection.close()


def test_normalising_two_failed_copies_keeps_each_destination_that_already_has_the_message(tmp_path: Path) -> None:
    """An attempt is one word for several destinations that can be at different points.

    Both copies read `failed`, so nothing in the attempt state separates them - but one has already
    delivered to the webhook and the other to the bot. Dropping either outright takes its
    reminder_deliveries rows with it through ON DELETE CASCADE, and due_reminder_deliveries selects
    on `rd.state IN ('pending','failed')`, so the survivor would send again to a destination that
    already has the message.
    """
    path = tmp_path / "test.sqlite3"
    database = Database(path)
    destinations = configured_destinations("both", WEBHOOK, "token", "123")
    webhook_key, bot_key = destinations[0].key, destinations[1].key
    legacy = database.create_attempt(_candidate_at(datetime(2026, 7, 8, 9, 55, tzinfo=MONTREAL)), "copy", destinations)
    assert legacy is not None
    database.connection.execute("UPDATE reminder_attempts SET state='failed' WHERE id=?", (legacy,))
    database.connection.execute(
        "UPDATE reminder_deliveries SET state='delivered' WHERE reminder_attempt_id=? AND destination_key=?",
        (legacy, webhook_key),
    )
    database.connection.execute("UPDATE reminder_attempts SET reminder_at='2026-07-08T09:55:00-04:00' WHERE id=?", (legacy,))
    database.connection.execute(
        """INSERT INTO reminder_attempts
        (event_row_id,calendar_id,event_id,instance_id,rule_id,reminder_at,content,state,created_at,updated_at)
        SELECT event_row_id,calendar_id,event_id,instance_id,rule_id,'2026-07-08T13:55:00+00:00','copy','failed',
               created_at,updated_at FROM reminder_attempts WHERE id=?""",
        (legacy,),
    )
    canonical = int(database.connection.execute("SELECT id FROM reminder_attempts WHERE id<>?", (legacy,)).fetchone()[0])
    database.ensure_reminder_deliveries(canonical, destinations)
    database.connection.execute(
        "UPDATE reminder_deliveries SET state='delivered' WHERE reminder_attempt_id=? AND destination_key=?",
        (canonical, bot_key),
    )
    database.connection.commit()
    # The two copies hold different halves of the evidence.
    assert _delivery_states(database, legacy) == {webhook_key: "delivered", bot_key: "pending"}
    assert _delivery_states(database, canonical) == {webhook_key: "pending", bot_key: "delivered"}
    database.close()

    reopened = Database(path)

    survivors = [int(row["id"]) for row in reopened.connection.execute("SELECT id FROM reminder_attempts")]
    assert survivors == [canonical]
    assert _delivery_states(reopened, canonical) == {webhook_key: "delivered", bot_key: "delivered"}
    reopened.close()


def _failed_twins(database: Database) -> tuple[int, int, list[DiscordDestination]]:
    """One reminder written twice, both copies reading `failed`, deliveries left to the caller."""
    destinations = configured_destinations("both", WEBHOOK, "token", "123")
    legacy = database.create_attempt(_candidate_at(datetime(2026, 7, 8, 9, 55, tzinfo=MONTREAL)), "copy", destinations)
    assert legacy is not None
    database.connection.execute(
        "UPDATE reminder_attempts SET state='failed',reminder_at='2026-07-08T09:55:00-04:00' WHERE id=?", (legacy,)
    )
    database.connection.execute(
        """INSERT INTO reminder_attempts
        (event_row_id,calendar_id,event_id,instance_id,rule_id,reminder_at,content,state,created_at,updated_at)
        SELECT event_row_id,calendar_id,event_id,instance_id,rule_id,'2026-07-08T13:55:00+00:00','copy','failed',
               created_at,updated_at FROM reminder_attempts WHERE id=?""",
        (legacy,),
    )
    canonical = int(database.connection.execute("SELECT id FROM reminder_attempts WHERE id<>?", (legacy,)).fetchone()[0])
    database.ensure_reminder_deliveries(canonical, destinations)
    return legacy, canonical, destinations


def test_normalising_keeps_the_chunks_a_destination_has_already_had(tmp_path: Path) -> None:
    """Two records of one destination can tie on state and still be a chunk apart.

    `deliver_resumable` sends `chunks[len(message_ids):]`, so the stored ids are a resume cursor,
    not a detail. Both copies read `failed` for the webhook, one having got two chunks out and the
    other one; discarding the further-along record by state alone sent the second chunk again.
    """
    path = tmp_path / "test.sqlite3"
    database = Database(path)
    legacy, canonical, destinations = _failed_twins(database)
    webhook_key = destinations[0].key
    for attempt, ids in ((legacy, '["c1", "c2"]'), (canonical, '["c1"]')):
        database.connection.execute(
            """UPDATE reminder_deliveries SET state='failed',attempt_count=2,discord_message_ids_json=?
            WHERE reminder_attempt_id=? AND destination_key=?""",
            (ids, attempt, webhook_key),
        )
    database.connection.commit()
    database.close()

    reopened = Database(path)

    survivor = reopened.connection.execute(
        """SELECT discord_message_ids_json,attempt_count FROM reminder_deliveries
        WHERE reminder_attempt_id=? AND destination_key=?""",
        (canonical, webhook_key),
    ).fetchone()
    assert json.loads(str(survivor["discord_message_ids_json"])) == ["c1", "c2"]
    assert int(survivor["attempt_count"]) == 2
    reopened.close()


def test_normalising_leaves_the_surviving_attempt_saying_what_its_destinations_say(tmp_path: Path) -> None:
    """Merging the destinations can change what the attempt's own one word should be.

    Two copies read `failed` because each was missing a different destination; merged, every
    destination on the survivor is delivered. Left at `failed` the attempt stays in due_attempts
    for good, and the dispatcher expires it the moment the event starts.
    """
    path = tmp_path / "test.sqlite3"
    database = Database(path)
    legacy, canonical, destinations = _failed_twins(database)
    webhook_key, bot_key = destinations[0].key, destinations[1].key
    for attempt, delivered in ((legacy, webhook_key), (canonical, bot_key)):
        database.connection.execute(
            "UPDATE reminder_deliveries SET state='delivered' WHERE reminder_attempt_id=? AND destination_key=?",
            (attempt, delivered),
        )
    database.connection.commit()
    database.close()

    reopened = Database(path)

    assert _delivery_states(reopened, canonical) == {webhook_key: "delivered", bot_key: "delivered"}
    assert str(reopened.connection.execute("SELECT state FROM reminder_attempts").fetchone()[0]) == "delivered"
    assert reopened.due_attempts(datetime(2026, 7, 8, 14, 0, tzinfo=UTC)) == []
    reopened.close()


@pytest.mark.parametrize("terminal", ["cancelled", "expired"])
def test_normalising_does_not_reopen_a_reminder_that_was_called_off(tmp_path: Path, terminal: str) -> None:
    """`cancelled` and `expired` are decisions about the reminder, not summaries of its destinations.

    The surviving attempt outranks a pending copy, so its pending deliveries move across - and
    recomputing its state from those would put a called-off reminder back in the send queue.
    """
    path = tmp_path / "test.sqlite3"
    database = Database(path)
    legacy, canonical, _ = _failed_twins(database)
    database.connection.execute("UPDATE reminder_attempts SET state=? WHERE id=?", (terminal, canonical))
    database.connection.execute("UPDATE reminder_attempts SET state='pending' WHERE id=?", (legacy,))
    database.connection.commit()
    database.close()

    reopened = Database(path)

    assert str(reopened.connection.execute("SELECT state FROM reminder_attempts").fetchone()[0]) == terminal
    reopened.close()


def _delivery_states(database: Database, attempt_id: int) -> dict[str, str]:
    return {
        str(row["destination_key"]): str(row["state"])
        for row in database.connection.execute(
            "SELECT destination_key,state FROM reminder_deliveries WHERE reminder_attempt_id=?", (attempt_id,)
        )
    }


def _legacy_twins(
    database: Database, offset_cursor: str | None, utc_cursor: str | None, *, rows_on: str | None = None
) -> tuple[int, int, str]:
    """One reminder twice, both `failed`, with its resume cursors where the old code kept them.

    Before reminder_deliveries existed the cursor lived on the attempt row, and
    migrate_legacy_reminder_deliveries moves it onto a delivery row only on the first dispatch that
    finds the attempt with no rows at all. `rows_on` gives one copy delivery rows, as an attempt
    written after that change has. Returns (offset copy, UTC copy, destination key).
    """
    destinations = configured_destinations("webhook", WEBHOOK, "", "")
    key = destinations[0].key
    offset = database.create_attempt(_candidate_at(datetime(2026, 7, 8, 9, 55, tzinfo=MONTREAL)), "copy")
    assert offset is not None
    database.connection.execute(
        """UPDATE reminder_attempts SET state='failed',reminder_at='2026-07-08T09:55:00-04:00',
        discord_message_ids_json=?,discord_destination_key=? WHERE id=?""",
        (offset_cursor, key, offset),
    )
    database.connection.execute(
        """INSERT INTO reminder_attempts
        (event_row_id,calendar_id,event_id,instance_id,rule_id,reminder_at,content,state,
         discord_message_ids_json,discord_destination_key,created_at,updated_at)
        SELECT event_row_id,calendar_id,event_id,instance_id,rule_id,'2026-07-08T13:55:00+00:00','copy','failed',
               ?,?,created_at,updated_at FROM reminder_attempts WHERE id=?""",
        (utc_cursor, key, offset),
    )
    utc = int(database.connection.execute("SELECT id FROM reminder_attempts WHERE id<>?", (offset,)).fetchone()[0])
    if rows_on is not None:
        database.ensure_reminder_deliveries(offset if rows_on == "offset" else utc, destinations)
    database.connection.commit()
    return offset, utc, key


def _cursor_the_pipeline_reads(database: Database) -> list[str]:
    """Where dispatch will resume: the delivery row if there is one, else the attempt row."""
    [attempt] = database.connection.execute("SELECT id,discord_message_ids_json FROM reminder_attempts").fetchall()
    row = database.connection.execute(
        "SELECT discord_message_ids_json FROM reminder_deliveries WHERE reminder_attempt_id=?", (attempt["id"],)
    ).fetchone()
    stored = row["discord_message_ids_json"] if row is not None else attempt["discord_message_ids_json"]
    return [] if stored is None else list(json.loads(str(stored)))


@pytest.mark.parametrize(
    ("offset_cursor", "utc_cursor", "rows_on"),
    [
        # Both legacy: the copy that is deleted had sent more.
        ('["c1", "c2"]', '["c1"]', None),
        # The deleted copy is legacy and the survivor already has rows, so the pipeline would never
        # look at an attempt-level cursor again.
        ('["c1", "c2"]', None, "utc"),
        # The mirror: the survivor is legacy and receives the loser's rows, which strands its own
        # attempt-level cursor for the same reason.
        (None, '["c1", "c2"]', "offset"),
    ],
)
def test_normalising_keeps_a_legacy_resume_cursor_where_dispatch_will_read_it(
    tmp_path: Path, offset_cursor: str | None, utc_cursor: str | None, rows_on: str | None
) -> None:
    path = tmp_path / "test.sqlite3"
    database = Database(path)
    _legacy_twins(database, offset_cursor, utc_cursor, rows_on=rows_on)
    database.close()

    reopened = Database(path)

    assert _cursor_the_pipeline_reads(reopened) == ["c1", "c2"]
    reopened.close()


def test_normalising_leaves_the_cursor_of_a_reminder_already_sent(tmp_path: Path) -> None:
    """Nothing is dispatched for a delivered attempt, so its cursor is not rewritten either."""
    path = tmp_path / "test.sqlite3"
    database = Database(path)
    offset, utc, _ = _legacy_twins(database, '["c1", "c2"]', '["done"]')
    database.connection.execute("UPDATE reminder_attempts SET state='delivered' WHERE id=?", (utc,))
    database.connection.commit()
    database.close()

    reopened = Database(path)

    assert _cursor_the_pipeline_reads(reopened) == ["done"]
    reopened.close()


def test_two_processes_opening_an_unconverted_database_together_lose_nothing(tmp_path: Path) -> None:
    """The first open after an upgrade can happen in two processes at once.

    run() opens the database before taking ProcessLock, and the per-minute and agenda timers fire
    in the same second. The second opener used to read its snapshot before the first had converted
    anything, then find each row at its own canonical key and merge it into itself - deleting
    every reminder the first had converted, with its delivery history.

    Here one connection holds the write lock while it converts, the way the other process's
    migration would, and a Database() is opened meanwhile.
    """
    path = tmp_path / "test.sqlite3"
    setup = Database(path)
    for hour in (9, 10, 11):
        attempt = setup.create_attempt(_candidate_at(datetime(2026, 7, 8, hour, 55, tzinfo=MONTREAL), event_id=f"e{hour}"), "m")
        setup.connection.execute(
            "UPDATE reminder_attempts SET state='delivered', reminder_at=? WHERE id=?",
            (f"2026-07-08T{hour:02}:55:00-04:00", attempt),
        )
    setup.connection.commit()
    setup.close()

    other = sqlite3.connect(path)
    other.execute("BEGIN IMMEDIATE")
    second = threading.Thread(target=lambda: Database(path).close())
    second.start()
    # Long enough for the second opener to have read whatever it is going to read before this
    # commits; far shorter than its 5 s busy timeout.
    time.sleep(0.5)
    for hour, utc in ((9, 13), (10, 14), (11, 15)):
        other.execute(
            "UPDATE reminder_attempts SET reminder_at=? WHERE reminder_at=?",
            (f"2026-07-08T{utc}:55:00+00:00", f"2026-07-08T{hour:02}:55:00-04:00"),
        )
    other.commit()
    other.close()
    second.join()

    rows = sqlite3.connect(path).execute("SELECT state,reminder_at FROM reminder_attempts ORDER BY reminder_at").fetchall()
    assert rows == [
        ("delivered", "2026-07-08T13:55:00+00:00"),
        ("delivered", "2026-07-08T14:55:00+00:00"),
        ("delivered", "2026-07-08T15:55:00+00:00"),
    ]
