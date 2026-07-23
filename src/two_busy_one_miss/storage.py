from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

from .google_calendar import CalendarEvent
from .rules import ReminderCandidate

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
  delivered_at TEXT,
  last_error_code TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(calendar_id, event_id, instance_id, rule_id, reminder_at)
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
    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        if read_only:
            self.connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout=5000")
        if not read_only:
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.executescript(SCHEMA)

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
                event.start.isoformat(),
                event.end.isoformat(),
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

    def _create_attempt(self, connection: sqlite3.Connection, candidate: ReminderCandidate, content: str) -> int | None:
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
                candidate.reminder_time.isoformat(),
                content,
                now,
                now,
            ),
        )
        if cursor.rowcount and cursor.lastrowid is not None:
            return int(cursor.lastrowid)
        connection.execute(
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
                candidate.reminder_time.isoformat(),
                content,
            ),
        )
        return None

    def create_attempt(self, candidate: ReminderCandidate, content: str) -> int | None:
        with self.transaction() as connection:
            return self._create_attempt(connection, candidate, content)

    def create_attempts(self, candidates: list[tuple[ReminderCandidate, str]]) -> int:
        with self.transaction() as connection:
            return sum(self._create_attempt(connection, candidate, content) is not None for candidate, content in candidates)

    def pending_attempts(self) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM reminder_attempts WHERE state IN ('pending','failed') ORDER BY reminder_at, id"
        ).fetchall()

    def due_attempts(self, now: datetime) -> list[sqlite3.Row]:
        return self.connection.execute(
            """SELECT reminder_attempts.*, events.start_at AS event_start_at, events.end_at AS event_end_at
            FROM reminder_attempts JOIN events ON events.id=reminder_attempts.event_row_id
            WHERE reminder_attempts.state IN ('pending','failed') AND reminder_attempts.reminder_at<=?
            ORDER BY reminder_attempts.reminder_at, reminder_attempts.id""",
            (now.isoformat(),),
        ).fetchall()

    def cancel_unmatched_attempts(
        self, candidates: list[ReminderCandidate], events: list[CalendarEvent], window_start: datetime, window_end: datetime
    ) -> int:
        expected = {
            (
                candidate.event.calendar_id,
                candidate.event.event_id,
                candidate.event.instance_id,
                candidate.rule_id,
                candidate.reminder_time.isoformat(),
            )
            for candidate in candidates
        }
        synced_events = tuple({(event.calendar_id, event.event_id, event.instance_id) for event in events})
        query = """SELECT id,calendar_id,event_id,instance_id,rule_id,reminder_at FROM reminder_attempts
            WHERE state IN ('pending','failed') AND reminder_at>=? AND reminder_at<=?"""
        parameters: list[str] = [window_start.isoformat(), window_end.isoformat()]
        if synced_events:
            event_matches = " OR ".join("(calendar_id=? AND event_id=? AND instance_id=?)" for _ in synced_events)
            query = f"""SELECT id,calendar_id,event_id,instance_id,rule_id,reminder_at FROM reminder_attempts
                WHERE state IN ('pending','failed') AND (
                    (reminder_at>=? AND reminder_at<=?) OR (reminder_at<? AND ({event_matches}))
                )"""
            parameters.extend([window_start.isoformat(), *(value for event in synced_events for value in event)])
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

    def record_delivery_progress(self, attempt_id: int, message_ids: list[str]) -> None:
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            "UPDATE reminder_attempts SET discord_message_ids_json=?, updated_at=? WHERE id=?",
            (json.dumps(message_ids), now, attempt_id),
        )
        self.connection.commit()

    def finish_delivery(self, attempt_id: int, message_ids: list[str]) -> None:
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            """UPDATE reminder_attempts SET state='delivered', delivered_at=?, discord_message_ids_json=?,
            attempt_count=attempt_count+1, last_error_code=NULL, updated_at=? WHERE id=?""",
            (now, json.dumps(message_ids), now, attempt_id),
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
            for table in ("events", "reminder_attempts")
        }

    def attempt_state(self, attempt_id: int) -> str:
        row = self.connection.execute("SELECT state FROM reminder_attempts WHERE id=?", (attempt_id,)).fetchone()
        return cast(str, row["state"])

    def agenda_delivery_state(self, delivery_id: int) -> str:
        row = self.connection.execute("SELECT state FROM agenda_deliveries WHERE id=?", (delivery_id,)).fetchone()
        return cast(str, row["state"])

    def close(self) -> None:
        self.connection.close()
