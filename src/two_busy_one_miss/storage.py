from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Self, cast

from two_read_runtime.discord import CorruptMessageIdsError, DiscordDestination, parse_message_ids
from two_read_runtime.permissions import prepare_private_file, repair_sqlite_files

from .google_calendar import CalendarEvent
from .rules import ReminderCandidate


def instant(value: datetime) -> str:
    """A timestamp normalised to UTC, for the columns that are compared as text.

    SQLite orders these lexically, and two ISO strings carrying different offsets are not in
    absolute-time order: `01:45:00-04:00` sorts after `01:30:00-05:00` although it is 45 minutes
    earlier. So `reminder_at<=?` and `ORDER BY reminder_at` give the wrong answer the moment one
    database holds more than one offset - which happens on every DST boundary, and on any event
    Google returns in another zone. Normalising here makes text order and absolute order the same
    thing again. Converting back for display is the renderer's job, off the comparison path.
    """
    return value.astimezone(UTC).isoformat()


# Which of two records of one reminder to keep, first choice first. `delivered` outranks everything
# because discarding it in favour of another row would send the reminder a second time. A reminder
# still owed comes next and a called-off one last, because only that way round can a wrong choice
# be put right: a sync cancels an owed reminder whose event has gone, and dispatch expires one whose
# event has started, but nothing reopens a cancelled row - _create_attempt updates only pending and
# failed ones. And the pair is a real one: the text comparison this change replaces cancelled the
# old row and wrote a pending one beside it whenever the same instant came back in another offset.
_STATE_RANK = {"delivered": 0, "failed": 1, "pending": 2, "cancelled": 3, "expired": 4}


def _state_rank(state: str) -> int:
    return _STATE_RANK.get(state, len(_STATE_RANK))


# The same idea one level down, per destination. `delivered` must win for the same reason; between
# the other two, `failed` carries an attempt count and an error code that `pending` does not. That
# is worth less than a sent chunk, so it only separates two records that got equally far.
_DELIVERY_RANK = {"delivered": 0, "failed": 1, "pending": 2}


def _delivery_rank(state: str) -> int:
    return _DELIVERY_RANK.get(state, len(_DELIVERY_RANK))


# The attempt states that summarise the destinations below them, and may therefore be recomputed
# from those. `cancelled` and `expired` are decisions about the reminder itself - nothing under it
# can undo them - so they are never derived.
_DERIVED_ATTEMPT_STATES = frozenset({"pending", "delivered", "failed"})


def _sent_chunk_count(value: object) -> int:
    """How many chunks a destination already has, which is where a retry resumes.

    `deliver_resumable` sends `chunks[len(message_ids):]`, so this number is the resume cursor and
    not a detail: two copies of one destination can tie on state and still be one chunk apart.
    Progress that will not parse counts as none, which is what the delivery path does with it too
    (CORRUPT_MESSAGE_IDS); guessing higher would skip a chunk that was never sent.
    """
    try:
        return len(parse_message_ids(value))
    except CorruptMessageIdsError:
        return 0


def _delivery_progress(row: sqlite3.Row) -> tuple[int, int, int]:
    """How far one destination got, furthest first, for choosing between two records of it.

    `delivered` comes first because nothing is sent for it. Below that the sent chunks decide, and
    state only breaks a tie: due_reminder_deliveries retries `pending` and `failed` alike, and a
    `pending` record can hold chunks - record_reminder_delivery_progress commits each one before
    the final state is written, so a crash in between leaves exactly that.
    """
    state = str(row["state"])
    return (state != "delivered", -_sent_chunk_count(row["discord_message_ids_json"]), _delivery_rank(state))


REMINDER_ATTEMPTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS reminder_attempts(
  id INTEGER PRIMARY KEY,
  event_row_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  calendar_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  instance_id TEXT NOT NULL,
  rule_id TEXT NOT NULL,
  reminder_at TEXT NOT NULL,
  content TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('pending','delivered','failed','expired','cancelled')),
  attempt_count INTEGER NOT NULL DEFAULT 0,
  discord_message_ids_json TEXT,
  discord_destination_key TEXT,
  delivered_at TEXT,
  last_error_code TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(calendar_id, event_id, instance_id, rule_id, reminder_at)
);
CREATE TABLE IF NOT EXISTS reminder_deliveries(
  id INTEGER PRIMARY KEY, reminder_attempt_id INTEGER NOT NULL REFERENCES reminder_attempts(id) ON DELETE CASCADE,
  destination_key TEXT NOT NULL, transport TEXT NOT NULL CHECK(transport IN ('webhook','bot')),
  state TEXT NOT NULL CHECK(state IN ('pending','delivered','failed')),
  attempt_count INTEGER NOT NULL DEFAULT 0, discord_message_ids_json TEXT, delivered_at TEXT, last_error_code TEXT,
  retired_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(reminder_attempt_id,destination_key)
);
"""

SCHEMA = (
    """
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY,
  calendar_id TEXT NOT NULL,
  calendar_name TEXT,
  event_id TEXT NOT NULL,
  instance_id TEXT NOT NULL,
  title TEXT NOT NULL,
  location TEXT NOT NULL,
  start_at TEXT NOT NULL,
  end_at TEXT NOT NULL,
  all_day INTEGER NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(calendar_id, instance_id)
);
"""
    + REMINDER_ATTEMPTS_SCHEMA
    + """
CREATE TABLE IF NOT EXISTS agenda_deliveries(
  id INTEGER PRIMARY KEY,
  agenda_day TEXT NOT NULL,
  timezone TEXT NOT NULL,
  destination_hash TEXT NOT NULL,
  content TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('pending','delivered','failed')),
  attempt_count INTEGER NOT NULL DEFAULT 0,
  discord_message_ids_json TEXT,
  delivered_at TEXT,
  last_error_code TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(agenda_day, timezone, destination_hash)
);
"""
)


class Database:
    def __init__(self, path: Path) -> None:
        prepare_private_file(path)
        repair_sqlite_files(path)
        self.connection = sqlite3.connect(path)
        try:
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA busy_timeout=5000")
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.executescript(SCHEMA)
            columns = {str(row["name"]) for row in self.connection.execute("PRAGMA table_info(reminder_attempts)")}
            if "discord_destination_key" not in columns:
                self.connection.execute("ALTER TABLE reminder_attempts ADD COLUMN discord_destination_key TEXT")
                self.connection.execute(
                    "UPDATE reminder_attempts SET discord_destination_key='webhook' WHERE discord_message_ids_json IS NOT NULL"
                )
                self.connection.commit()
            delivery_columns = {str(row["name"]) for row in self.connection.execute("PRAGMA table_info(reminder_deliveries)")}
            if "retired_at" not in delivery_columns:
                self.connection.execute("ALTER TABLE reminder_deliveries ADD COLUMN retired_at TEXT")
                self.connection.commit()
            self._normalise_stored_instants()
            repair_sqlite_files(path)
        except Exception:
            self.connection.close()
            raise

    def _merge_reminder_deliveries(self, winner: int, loser: int) -> None:
        """Carry the loser's per-destination progress across before its rows cascade away.

        The attempt's own state is one word for what may be several destinations at different
        points: an attempt reads `failed` while one of its destinations is already `delivered`.
        Two copies can therefore tie on attempt state and still hold different evidence, and
        `rd.state IN ('pending','failed')` in due_reminder_deliveries is the only thing stopping a
        resend to a destination that already has the message. Deleting the loser outright took
        that evidence with it through ON DELETE CASCADE.

        Two records of the same destination are separated by state first and by how many chunks
        they have already sent second, because state alone does not separate two `failed` copies
        that stopped at different chunks. attempt_count is the larger of the two either way: it is
        only ever reported, and understating how often a destination has been tried is the less
        useful of the two errors.
        """
        for row in self.connection.execute("SELECT * FROM reminder_deliveries WHERE reminder_attempt_id=?", (loser,)).fetchall():
            existing = self.connection.execute(
                "SELECT * FROM reminder_deliveries WHERE reminder_attempt_id=? AND destination_key=?",
                (winner, row["destination_key"]),
            ).fetchone()
            if existing is None:
                self.connection.execute(
                    "UPDATE reminder_deliveries SET reminder_attempt_id=? WHERE id=?", (winner, int(row["id"]))
                )
                continue
            attempts = max(int(row["attempt_count"]), int(existing["attempt_count"]))
            if _delivery_progress(row) < _delivery_progress(existing):
                self.connection.execute(
                    """UPDATE reminder_deliveries SET state=?,attempt_count=?,discord_message_ids_json=?,
                    delivered_at=?,last_error_code=?,retired_at=?,updated_at=? WHERE id=?""",
                    (
                        row["state"],
                        attempts,
                        row["discord_message_ids_json"],
                        row["delivered_at"],
                        row["last_error_code"],
                        row["retired_at"],
                        datetime.now(UTC).isoformat(),
                        int(existing["id"]),
                    ),
                )
            elif attempts != int(existing["attempt_count"]):
                self.connection.execute(
                    "UPDATE reminder_deliveries SET attempt_count=?,updated_at=? WHERE id=?",
                    (attempts, datetime.now(UTC).isoformat(), int(existing["id"])),
                )

    def _merge_attempt_checkpoints(self, winner: int, loser: int) -> None:
        """Carry the attempt-level resume cursors across as well as the per-destination ones.

        Attempts written before reminder_deliveries existed keep their cursor on the attempt row,
        and migrate_legacy_reminder_deliveries moves it onto a delivery row only on the first
        dispatch that finds the attempt with no delivery rows at all. So a merge can lose one in
        two ways: the loser's goes with it, and once the winner has delivery rows - its own, or
        ones just moved over from the loser - its own attempt-level cursor is never migrated.

        Where the winner has rows, each cursor is folded onto the row for the same destination if
        that row is behind; a key with no row is not adopted, which is also what the migration
        does with a key that matches no configured destination. Where it has none, the winner keeps
        the longer of the two cursors for the migration to find. Only a reminder that is still owed
        is touched - nothing is sent for the others, whatever their cursor says.
        """
        rows = {
            int(row["id"]): row
            for row in self.connection.execute(
                "SELECT id,state,discord_message_ids_json,discord_destination_key FROM reminder_attempts WHERE id IN (?,?)",
                (winner, loser),
            )
        }
        if str(rows[winner]["state"]) not in ("pending", "failed"):
            return
        cursors = [
            (rows[attempt]["discord_message_ids_json"], str(rows[attempt]["discord_destination_key"]))
            for attempt in (winner, loser)
            if rows[attempt]["discord_message_ids_json"] is not None and rows[attempt]["discord_destination_key"] is not None
        ]
        if not cursors:
            return
        if self.connection.execute("SELECT 1 FROM reminder_deliveries WHERE reminder_attempt_id=?", (winner,)).fetchone():
            for message_ids, key in cursors:
                delivery = self.connection.execute(
                    """SELECT id,discord_message_ids_json FROM reminder_deliveries
                    WHERE reminder_attempt_id=? AND destination_key=? AND state<>'delivered'""",
                    (winner, key),
                ).fetchone()
                if delivery is not None and _sent_chunk_count(message_ids) > _sent_chunk_count(
                    delivery["discord_message_ids_json"]
                ):
                    self.connection.execute(
                        "UPDATE reminder_deliveries SET discord_message_ids_json=?,updated_at=? WHERE id=?",
                        (message_ids, datetime.now(UTC).isoformat(), int(delivery["id"])),
                    )
            return
        # max() keeps the first of equals, and the winner's cursor is listed first.
        message_ids, key = max(cursors, key=lambda cursor: _sent_chunk_count(cursor[0]))
        self.connection.execute(
            "UPDATE reminder_attempts SET discord_message_ids_json=?,discord_destination_key=? WHERE id=?",
            (message_ids, key, winner),
        )

    def _advance_merged_attempt(self, attempt_id: int, state: str) -> None:
        """Move a merged attempt's own word forward to what its destinations now say.

        Merging can change it: two copies that both read `failed` because each was missing a
        different destination leave one row where every destination is delivered. Left at `failed`
        that reminder stays in due_attempts for good, and the dispatcher expires it the moment the
        event starts.

        Forwards only, and only from a state that summarises the destinations below it. An attempt
        delivered through the per-attempt path still has `pending` delivery rows underneath it -
        finish_delivery writes the one and not the other - so deriving freely would demote it and
        send the reminder again. `cancelled` and `expired` are decisions about the reminder itself
        that nothing below it may undo.
        """
        if state not in _DERIVED_ATTEMPT_STATES:
            return
        derived = self._derived_attempt_state(self.connection, attempt_id)
        if derived is None or _state_rank(derived) >= _state_rank(state):
            return
        self.connection.execute(
            "UPDATE reminder_attempts SET state=?,updated_at=? WHERE id=?",
            (derived, datetime.now(UTC).isoformat(), attempt_id),
        )

    def _normalise_stored_instants(self) -> None:
        """Rewrite timestamps written before these columns held UTC.

        Detect-and-fix, like the two column additions above: a row is converted only when its text
        differs from its own UTC form, so every later open is a no-op over a few hundred rows.

        A naive value is left alone. None should exist - everything written here comes from an
        aware datetime - and converting one would mean guessing a zone from the machine's locale,
        which is how the offsets got mixed in the first place.

        The whole pass holds the write lock from its first read. The constructor runs outside
        ProcessLock - run() opens the database before taking it, and the per-minute and agenda
        timers fire in the same second - so two processes can make the first open after an
        upgrade together. Without the lock the second read its snapshot before the first had
        converted anything, then looked each row's canonical key up afterwards and found the row
        itself there: it merged every row into itself and deleted it, delivery history and all,
        and the next sync recreated and resent whatever was still in the window. With it, the
        second waits for the first to commit and then finds nothing left to do.
        """
        if self.connection.in_transaction:
            self.connection.commit()
        self.connection.execute("BEGIN IMMEDIATE")
        events = self.connection.execute("SELECT id,start_at,end_at FROM events").fetchall()
        for row in events:
            start, end = str(row["start_at"]), str(row["end_at"])
            parsed_start, parsed_end = datetime.fromisoformat(start), datetime.fromisoformat(end)
            if parsed_start.tzinfo is None or parsed_end.tzinfo is None:
                continue
            if (instant(parsed_start), instant(parsed_end)) != (start, end):
                self.connection.execute(
                    "UPDATE events SET start_at=?,end_at=? WHERE id=?",
                    (instant(parsed_start), instant(parsed_end), int(row["id"])),
                )
        # reminder_at is part of UNIQUE(calendar_id,event_id,instance_id,rule_id,reminder_at), so two
        # offsets naming one instant become one key here - the same reminder recorded twice, as a
        # re-sync across a DST boundary produces. The target key can already be held by a row that
        # was written in UTC and so never enters this loop, which is why the occupant is looked up
        # rather than inferred from the order rows are visited in: ordering decides what gets
        # converted first, not what got there first.
        attempts = self.connection.execute(
            "SELECT id,state,reminder_at,calendar_id,event_id,instance_id,rule_id FROM reminder_attempts"
        ).fetchall()
        removed: set[int] = set()
        for row in sorted(attempts, key=lambda item: (_state_rank(str(item["state"])), int(item["id"]))):
            row_id = int(row["id"])
            if row_id in removed:
                continue
            reminder_at = str(row["reminder_at"])
            parsed = datetime.fromisoformat(reminder_at)
            if parsed.tzinfo is None or instant(parsed) == reminder_at:
                continue
            canonical = instant(parsed)
            occupant = self.connection.execute(
                """SELECT id,state FROM reminder_attempts WHERE calendar_id=? AND event_id=? AND instance_id=?
                AND rule_id=? AND reminder_at=?""",
                (row["calendar_id"], row["event_id"], row["instance_id"], row["rule_id"], canonical),
            ).fetchone()
            if occupant is not None and int(occupant["id"]) == row_id:
                # Already converted by someone else since the snapshot. The lock above rules this
                # out; it is checked anyway, because the other branch would delete the row.
                continue
            if occupant is not None:
                # One reminder, two rows. The copy _STATE_RANK puts second is dropped, so a
                # delivered one is never discarded in favour of a pending one that would send it
                # again, nor an owed one for a cancelled one that nothing reopens - and its
                # reminder_deliveries rows, which ON DELETE CASCADE would take with it, stay with it.
                keep_occupant = _state_rank(str(occupant["state"])) <= _state_rank(str(row["state"]))
                winner = int(occupant["id"]) if keep_occupant else row_id
                loser = row_id if keep_occupant else int(occupant["id"])
                self._merge_reminder_deliveries(winner, loser)
                self._merge_attempt_checkpoints(winner, loser)
                self.connection.execute("DELETE FROM reminder_attempts WHERE id=?", (loser,))
                removed.add(loser)
                self._advance_merged_attempt(winner, str(occupant["state"] if keep_occupant else row["state"]))
                if keep_occupant:
                    continue
            self.connection.execute("UPDATE reminder_attempts SET reminder_at=? WHERE id=?", (canonical, row_id))
        self.connection.commit()

    @classmethod
    def reading(cls, connection: sqlite3.Connection) -> Self:
        """Wrap a connection opened elsewhere for reporting only.

        The reporting commands must not create or migrate anything, so they skip the constructor
        and reuse the read methods below against a connection they were handed.
        """
        database = cls.__new__(cls)
        database.connection = connection
        return database

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection:
            yield self.connection

    def _upsert_event(self, connection: sqlite3.Connection, event: CalendarEvent) -> int:
        now = datetime.now(UTC).isoformat()
        connection.execute(
            """INSERT INTO events
            (calendar_id,calendar_name,event_id,instance_id,title,location,start_at,end_at,all_day,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(calendar_id, instance_id) DO UPDATE SET
              calendar_name=excluded.calendar_name,
              event_id=excluded.event_id,
              title=excluded.title,
              location=excluded.location,
              start_at=excluded.start_at,
              end_at=excluded.end_at,
              all_day=excluded.all_day,
              updated_at=excluded.updated_at""",
            (
                event.calendar_id,
                event.calendar_name,
                event.event_id,
                event.instance_id,
                event.title,
                event.location,
                instant(event.start),
                instant(event.end),
                int(event.all_day),
                now,
            ),
        )
        row = connection.execute(
            "SELECT id FROM events WHERE calendar_id=? AND instance_id=?",
            (event.calendar_id, event.instance_id),
        ).fetchone()
        return int(row["id"])

    def upsert_event(self, event: CalendarEvent) -> int:
        with self.transaction() as connection:
            return self._upsert_event(connection, event)

    def _create_reminder_deliveries(
        self, connection: sqlite3.Connection, reminder_attempt_id: int, destinations: list[DiscordDestination], now: str
    ) -> None:
        connection.executemany(
            """INSERT OR IGNORE INTO reminder_deliveries
            (reminder_attempt_id,destination_key,transport,state,created_at,updated_at) VALUES(?,?,?,'pending',?,?)""",
            [(reminder_attempt_id, destination.key, destination.transport, now, now) for destination in destinations],
        )

    def _create_attempt(
        self, connection: sqlite3.Connection, candidate: ReminderCandidate, content: str, destinations: list[DiscordDestination]
    ) -> int | None:
        event_row_id = self._upsert_event(connection, candidate.event)
        now = datetime.now(UTC).isoformat()
        cursor = connection.execute(
            """INSERT OR IGNORE INTO reminder_attempts
            (event_row_id,calendar_id,event_id,instance_id,rule_id,reminder_at,content,state,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,'pending',?,?)""",
            (
                event_row_id,
                candidate.event.calendar_id,
                candidate.event.event_id,
                candidate.event.instance_id,
                candidate.rule_id,
                instant(candidate.reminder_time),
                content,
                now,
                now,
            ),
        )
        created = int(cursor.lastrowid) if cursor.rowcount and cursor.lastrowid is not None else None
        if created is None:
            updated = connection.execute(
                """UPDATE reminder_attempts SET event_row_id=?, content=?, state='pending', discord_message_ids_json=NULL,
                last_error_code=NULL, updated_at=?
                WHERE calendar_id=? AND event_id=? AND instance_id=? AND rule_id=? AND reminder_at=?
                  AND state IN ('pending','failed') AND content<>?""",
                (
                    event_row_id,
                    content,
                    now,
                    candidate.event.calendar_id,
                    candidate.event.event_id,
                    candidate.event.instance_id,
                    candidate.rule_id,
                    instant(candidate.reminder_time),
                    content,
                ),
            )
            if updated.rowcount:
                row = connection.execute(
                    """SELECT id FROM reminder_attempts WHERE calendar_id=? AND event_id=? AND instance_id=?
                    AND rule_id=? AND reminder_at=?""",
                    (
                        candidate.event.calendar_id,
                        candidate.event.event_id,
                        candidate.event.instance_id,
                        candidate.rule_id,
                        instant(candidate.reminder_time),
                    ),
                ).fetchone()
                assert row is not None
                connection.execute("DELETE FROM reminder_deliveries WHERE reminder_attempt_id=?", (int(row["id"]),))
        if destinations:
            row = connection.execute(
                """SELECT id FROM reminder_attempts WHERE calendar_id=? AND event_id=? AND instance_id=?
                AND rule_id=? AND reminder_at=?""",
                (
                    candidate.event.calendar_id,
                    candidate.event.event_id,
                    candidate.event.instance_id,
                    candidate.rule_id,
                    instant(candidate.reminder_time),
                ),
            ).fetchone()
            assert row is not None
            self._create_reminder_deliveries(connection, int(row["id"]), destinations, now)
        return created

    def create_attempt(
        self, candidate: ReminderCandidate, content: str, destinations: list[DiscordDestination] | None = None
    ) -> int | None:
        with self.transaction() as connection:
            return self._create_attempt(connection, candidate, content, destinations or [])

    def create_attempts(
        self, candidates: list[tuple[ReminderCandidate, str]], destinations: list[DiscordDestination] | None = None
    ) -> int:
        with self.transaction() as connection:
            return sum(
                self._create_attempt(connection, candidate, content, destinations or []) is not None
                for candidate, content in candidates
            )

    def pending_attempts(self) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM reminder_attempts WHERE state IN ('pending','failed') ORDER BY reminder_at, id"
        ).fetchall()

    def due_attempts(self, now: datetime) -> list[sqlite3.Row]:
        """Selected by state in SQL, then compared and ordered as instants in Python.

        `Database.reading` deliberately does not migrate, because a reporting command must not
        write; so the read-only path - `run --dry-run` - can still meet rows carrying a local
        offset, and comparing those as text against a UTC parameter is the defect this change
        exists to remove. An old `09:55:00-04:00` row sorts before `13:50:00+00:00` and a dry run
        would report it due five minutes early.

        Parsing each candidate costs nothing at these sizes - a few hundred rows - and gives the
        migrated and unmigrated paths one answer instead of two.
        """
        rows = self.connection.execute(
            """SELECT reminder_attempts.*, events.start_at AS event_start_at, events.end_at AS event_end_at
            FROM reminder_attempts JOIN events ON events.id=reminder_attempts.event_row_id
            WHERE reminder_attempts.state IN ('pending','failed')"""
        ).fetchall()
        moment = now.astimezone(UTC)
        due = [(datetime.fromisoformat(str(row["reminder_at"])).astimezone(UTC), int(row["id"]), row) for row in rows]
        return [row for reminder_at, _, row in sorted(due, key=lambda item: item[:2]) if reminder_at <= moment]

    def ensure_reminder_deliveries(self, reminder_attempt_id: int, destinations: list[DiscordDestination]) -> None:
        with self.transaction() as connection:
            self._create_reminder_deliveries(connection, reminder_attempt_id, destinations, datetime.now(UTC).isoformat())

    def reconcile_reminder_deliveries(self, reminder_attempt_id: int, destinations: list[DiscordDestination]) -> None:
        with self.transaction() as connection:
            now = datetime.now(UTC).isoformat()
            self._create_reminder_deliveries(connection, reminder_attempt_id, destinations, now)
            keys = [destination.key for destination in destinations]
            placeholders = ",".join("?" for _ in keys)
            connection.execute(
                f"""UPDATE reminder_deliveries SET retired_at=?,updated_at=? WHERE reminder_attempt_id=?
                AND destination_key NOT IN ({placeholders}) AND state IN ('pending','failed') AND retired_at IS NULL""",
                (now, now, reminder_attempt_id, *keys),
            )
            connection.execute(
                f"""UPDATE reminder_deliveries SET retired_at=NULL,updated_at=? WHERE reminder_attempt_id=?
                AND destination_key IN ({placeholders})""",
                (now, reminder_attempt_id, *keys),
            )
            self._refresh_reminder_state(connection, reminder_attempt_id)

    def migrate_legacy_reminder_deliveries(self, reminder_attempt_id: int, destinations: list[DiscordDestination]) -> None:
        with self.transaction() as connection:
            attempt = connection.execute(
                """SELECT state,discord_message_ids_json,discord_destination_key,last_error_code
                FROM reminder_attempts WHERE id=?""",
                (reminder_attempt_id,),
            ).fetchone()
            if attempt is None:
                raise ValueError(f"reminder attempt {reminder_attempt_id} not found")
            now = datetime.now(UTC).isoformat()
            self._create_reminder_deliveries(connection, reminder_attempt_id, destinations, now)
            source_key = attempt["discord_destination_key"]
            destination = next((item for item in destinations if item.key == source_key), None)
            if destination is not None:
                connection.execute(
                    """UPDATE reminder_deliveries SET state=?,discord_message_ids_json=?,last_error_code=?,updated_at=?
                    WHERE reminder_attempt_id=? AND destination_key=?""",
                    (
                        attempt["state"],
                        attempt["discord_message_ids_json"],
                        attempt["last_error_code"],
                        now,
                        reminder_attempt_id,
                        destination.key,
                    ),
                )

    def has_reminder_deliveries(self, reminder_attempt_id: int) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM reminder_deliveries WHERE reminder_attempt_id=?", (reminder_attempt_id,)
            ).fetchone()
            is not None
        )

    def has_reminder_delivery(self, delivery_id: int) -> bool:
        return self.connection.execute("SELECT 1 FROM reminder_deliveries WHERE id=?", (delivery_id,)).fetchone() is not None

    def due_reminder_deliveries(self, now: datetime, destinations: list[DiscordDestination]) -> list[sqlite3.Row]:
        if not destinations:
            return []
        # Which attempts are due is decided once, by due_attempts, rather than repeated here as a
        # second text comparison that the read-only path would get wrong in its own way.
        due_ids = [int(attempt["id"]) for attempt in self.due_attempts(now)]
        if not due_ids:
            return []
        for attempt_id in due_ids:
            self.reconcile_reminder_deliveries(attempt_id, destinations)
        keys = [destination.key for destination in destinations]
        placeholders = ",".join("?" for _ in keys)
        attempt_placeholders = ",".join("?" for _ in due_ids)
        return self.connection.execute(
            f"""SELECT rd.*,ra.content,e.start_at AS event_start_at FROM reminder_deliveries rd
            JOIN reminder_attempts ra ON ra.id=rd.reminder_attempt_id JOIN events e ON e.id=ra.event_row_id
            WHERE rd.destination_key IN ({placeholders}) AND rd.state IN ('pending','failed')
            AND rd.retired_at IS NULL AND ra.id IN ({attempt_placeholders})
            ORDER BY ra.reminder_at,rd.id""",
            (*keys, *due_ids),
        ).fetchall()

    def _derived_attempt_state(self, connection: sqlite3.Connection, reminder_attempt_id: int) -> str | None:
        """What an attempt's destinations add up to, or None when it has none to speak for it."""
        states = [
            str(row["state"])
            for row in connection.execute(
                "SELECT state FROM reminder_deliveries WHERE reminder_attempt_id=? AND retired_at IS NULL",
                (reminder_attempt_id,),
            )
        ]
        if not states:
            return None
        return "delivered" if all(value == "delivered" for value in states) else "failed" if "failed" in states else "pending"

    def _refresh_reminder_state(self, connection: sqlite3.Connection, reminder_attempt_id: int) -> None:
        state = self._derived_attempt_state(connection, reminder_attempt_id)
        if state is None:
            return
        connection.execute(
            "UPDATE reminder_attempts SET state=?,updated_at=? WHERE id=?",
            (state, datetime.now(UTC).isoformat(), reminder_attempt_id),
        )

    def record_reminder_delivery_progress(self, delivery_id: int, message_ids: list[str]) -> None:
        self.connection.execute(
            "UPDATE reminder_deliveries SET discord_message_ids_json=?,updated_at=? WHERE id=?",
            (json.dumps(message_ids), datetime.now(UTC).isoformat(), delivery_id),
        )
        self.connection.commit()

    def finish_reminder_delivery(self, delivery_id: int, message_ids: list[str], destinations: list[DiscordDestination]) -> None:
        now = datetime.now(UTC).isoformat()
        with self.transaction() as connection:
            row = connection.execute("SELECT reminder_attempt_id FROM reminder_deliveries WHERE id=?", (delivery_id,)).fetchone()
            if row is None:
                raise ValueError(f"reminder delivery {delivery_id} not found")
            connection.execute(
                """UPDATE reminder_deliveries SET state='delivered',delivered_at=?,discord_message_ids_json=?,
                attempt_count=attempt_count+1,last_error_code=NULL,updated_at=? WHERE id=?""",
                (now, json.dumps(message_ids), now, delivery_id),
            )
            self._refresh_reminder_state(connection, int(row["reminder_attempt_id"]))

    def fail_reminder_delivery(self, delivery_id: int, error_code: str, destinations: list[DiscordDestination]) -> None:
        now = datetime.now(UTC).isoformat()
        with self.transaction() as connection:
            row = connection.execute("SELECT reminder_attempt_id FROM reminder_deliveries WHERE id=?", (delivery_id,)).fetchone()
            if row is None:
                raise ValueError(f"reminder delivery {delivery_id} not found")
            connection.execute(
                """UPDATE reminder_deliveries SET state='failed',attempt_count=attempt_count+1,last_error_code=?,updated_at=?
                WHERE id=?""",
                (error_code, now, delivery_id),
            )
            self._refresh_reminder_state(connection, int(row["reminder_attempt_id"]))

    def reset_corrupt_reminder_delivery(self, delivery_id: int, destinations: list[DiscordDestination]) -> bool:
        with self.transaction() as connection:
            row = connection.execute("SELECT reminder_attempt_id FROM reminder_deliveries WHERE id=?", (delivery_id,)).fetchone()
            if row is None:
                return False
            cursor = connection.execute(
                """UPDATE reminder_deliveries SET state='pending',discord_message_ids_json=NULL,last_error_code=NULL,updated_at=?
                WHERE id=? AND state='failed' AND last_error_code='DISCORD_MESSAGE_IDS_CORRUPT'""",
                (datetime.now(UTC).isoformat(), delivery_id),
            )
            self._refresh_reminder_state(connection, int(row["reminder_attempt_id"]))
            return bool(cursor.rowcount)

    def cancel_unmatched_attempts(
        self, candidates: list[ReminderCandidate], events: list[CalendarEvent], window_start: datetime, window_end: datetime
    ) -> int:
        expected = {
            (
                candidate.event.calendar_id,
                candidate.event.event_id,
                candidate.event.instance_id,
                candidate.rule_id,
                instant(candidate.reminder_time),
            )
            for candidate in candidates
        }
        synced_events = tuple({(event.calendar_id, event.event_id, event.instance_id) for event in events})
        query = """SELECT id,calendar_id,event_id,instance_id,rule_id,reminder_at FROM reminder_attempts
            WHERE state IN ('pending','failed') AND reminder_at>=? AND reminder_at<=?"""
        parameters: list[str] = [instant(window_start), instant(window_end)]
        if synced_events:
            event_matches = " OR ".join("(calendar_id=? AND event_id=? AND instance_id=?)" for _ in synced_events)
            query = f"""SELECT id,calendar_id,event_id,instance_id,rule_id,reminder_at FROM reminder_attempts
                WHERE state IN ('pending','failed') AND (
                    (reminder_at>=? AND reminder_at<=?) OR (reminder_at<? AND ({event_matches}))
                )"""
            parameters.extend([instant(window_start), *(value for event in synced_events for value in event)])
        rows = self.connection.execute(query, parameters).fetchall()
        cancelled = [
            int(row["id"])
            for row in rows
            if (row["calendar_id"], row["event_id"], row["instance_id"], row["rule_id"], row["reminder_at"]) not in expected
        ]
        if cancelled:
            now = datetime.now(UTC).isoformat()
            self.connection.executemany(
                """UPDATE reminder_attempts SET state='cancelled', last_error_code='CALENDAR_EVENT_CHANGED',
                updated_at=? WHERE id=?""",
                [(now, attempt_id) for attempt_id in cancelled],
            )
            self.connection.commit()
        return len(cancelled)

    def expire_attempt(self, attempt_id: int) -> None:
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            """UPDATE reminder_attempts SET state='expired', last_error_code='REMINDER_EVENT_STARTED',
            updated_at=? WHERE id=?""",
            (now, attempt_id),
        )
        self.connection.commit()

    def delivery_checkpoint(self, attempt_id: int, destination_key: str) -> object:
        row = self.connection.execute(
            "SELECT discord_message_ids_json,discord_destination_key FROM reminder_attempts WHERE id=?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"reminder attempt {attempt_id} not found")
        previous_key = row["discord_destination_key"]
        if previous_key != destination_key:
            self.connection.execute(
                "UPDATE reminder_attempts SET discord_message_ids_json=NULL,discord_destination_key=?,updated_at=? WHERE id=?",
                (destination_key, datetime.now(UTC).isoformat(), attempt_id),
            )
            self.connection.commit()
            return None
        return row["discord_message_ids_json"]

    def record_delivery_progress(self, attempt_id: int, message_ids: list[str], destination_key: str | None = None) -> None:
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            "UPDATE reminder_attempts SET discord_message_ids_json=?,discord_destination_key=?,updated_at=? WHERE id=?",
            (json.dumps(message_ids), destination_key, now, attempt_id),
        )
        self.connection.commit()

    def finish_delivery(self, attempt_id: int, message_ids: list[str], destination_key: str | None = None) -> None:
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            """UPDATE reminder_attempts SET state='delivered', delivered_at=?,
            discord_message_ids_json=?,discord_destination_key=?, attempt_count=attempt_count+1,
            last_error_code=NULL, updated_at=? WHERE id=?""",
            (now, json.dumps(message_ids), destination_key, now, attempt_id),
        )
        self.connection.commit()

    def fail_delivery(self, attempt_id: int, error_code: str = "DISCORD_DELIVERY_FAILED") -> None:
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            """UPDATE reminder_attempts SET state='failed', attempt_count=attempt_count+1,
            last_error_code=?, updated_at=? WHERE id=?""",
            (error_code, now, attempt_id),
        )
        self.connection.commit()

    def reset_corrupt_delivery(self, attempt_id: int) -> bool:
        cursor = self.connection.execute(
            """UPDATE reminder_attempts SET state='pending', discord_message_ids_json=NULL, last_error_code=NULL, updated_at=?
            WHERE id=? AND state='failed' AND last_error_code='DISCORD_MESSAGE_IDS_CORRUPT'""",
            (datetime.now(UTC).isoformat(), attempt_id),
        )
        self.connection.commit()
        return bool(cursor.rowcount)

    def create_agenda_delivery(
        self, day: date, timezone: str, destination_hash: str, content: str, *, force: bool = False
    ) -> int | None:
        now = datetime.now(UTC).isoformat()
        cursor = self.connection.execute(
            """INSERT OR IGNORE INTO agenda_deliveries
            (agenda_day,timezone,destination_hash,content,state,created_at,updated_at)
            VALUES(?,?,?,?,'pending',?,?)""",
            (day.isoformat(), timezone, destination_hash, content, now, now),
        )
        if cursor.rowcount and cursor.lastrowid is not None:
            self.connection.commit()
            return int(cursor.lastrowid)
        if not force:
            self.connection.commit()
            return None
        row = self.connection.execute(
            "SELECT id FROM agenda_deliveries WHERE agenda_day=? AND timezone=? AND destination_hash=?",
            (day.isoformat(), timezone, destination_hash),
        ).fetchone()
        if row is None:
            raise RuntimeError("agenda delivery was not created")
        self.connection.execute(
            """UPDATE agenda_deliveries SET content=?, state='pending', discord_message_ids_json=NULL,
            delivered_at=NULL, last_error_code=NULL, updated_at=? WHERE id=?""",
            (content, now, int(row["id"])),
        )
        self.connection.commit()
        return int(row["id"])

    def pending_agenda_deliveries(self, day: date, timezone: str, destination_hash: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            """SELECT * FROM agenda_deliveries
            WHERE agenda_day=? AND timezone=? AND destination_hash=? AND state IN ('pending','failed') ORDER BY id""",
            (day.isoformat(), timezone, destination_hash),
        ).fetchall()

    def finish_agenda_delivery(self, delivery_id: int, message_ids: list[str]) -> None:
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            """UPDATE agenda_deliveries SET state='delivered', delivered_at=?, discord_message_ids_json=?,
            attempt_count=attempt_count+1, last_error_code=NULL, updated_at=? WHERE id=?""",
            (now, json.dumps(message_ids), now, delivery_id),
        )
        self.connection.commit()

    def record_agenda_delivery_progress(self, delivery_id: int, message_ids: list[str]) -> None:
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            "UPDATE agenda_deliveries SET discord_message_ids_json=?, updated_at=? WHERE id=?",
            (json.dumps(message_ids), now, delivery_id),
        )
        self.connection.commit()

    def fail_agenda_delivery(self, delivery_id: int, error_code: str = "DISCORD_DELIVERY_FAILED") -> None:
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            """UPDATE agenda_deliveries SET state='failed', attempt_count=attempt_count+1,
            last_error_code=?, updated_at=? WHERE id=?""",
            (error_code, now, delivery_id),
        )
        self.connection.commit()

    def reset_corrupt_agenda_delivery(self, delivery_id: int) -> bool:
        cursor = self.connection.execute(
            """UPDATE agenda_deliveries SET state='pending', discord_message_ids_json=NULL, last_error_code=NULL, updated_at=?
            WHERE id=? AND state='failed' AND last_error_code='DISCORD_MESSAGE_IDS_CORRUPT'""",
            (datetime.now(UTC).isoformat(), delivery_id),
        )
        self.connection.commit()
        return bool(cursor.rowcount)

    def counts(self) -> dict[str, int]:
        return {
            table: int(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("events", "reminder_attempts", "reminder_deliveries")
        }

    def attempt_state(self, attempt_id: int) -> str:
        row = self.connection.execute("SELECT state FROM reminder_attempts WHERE id=?", (attempt_id,)).fetchone()
        return cast(str, row["state"])

    def agenda_delivery_state(self, delivery_id: int) -> str:
        row = self.connection.execute("SELECT state FROM agenda_deliveries WHERE id=?", (delivery_id,)).fetchone()
        return cast(str, row["state"])

    def close(self) -> None:
        self.connection.close()
