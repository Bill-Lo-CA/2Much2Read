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

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
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
CREATE TABLE IF NOT EXISTS reminder_attempts(
  id INTEGER PRIMARY KEY,
  event_row_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  calendar_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  instance_id TEXT NOT NULL,
  rule_id TEXT NOT NULL,
  reminder_at TEXT NOT NULL,
  content TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('pending','delivered','failed')),
  attempt_count INTEGER NOT NULL DEFAULT 0,
  discord_message_ids_json TEXT,
  delivered_at TEXT,
  last_error_code TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(calendar_id, event_id, instance_id, rule_id, reminder_at)
);
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
INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(1, datetime('now'));
"""


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection:
            yield self.connection

    def upsert_event(self, event: CalendarEvent) -> int:
        now = datetime.now(UTC).isoformat()
        with self.transaction() as connection:
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

    def create_attempt(self, candidate: ReminderCandidate, content: str) -> int | None:
        event_row_id = self.upsert_event(candidate.event)
        now = datetime.now(UTC).isoformat()
        cursor = self.connection.execute(
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
        self.connection.commit()
        return int(cursor.lastrowid) if cursor.rowcount and cursor.lastrowid is not None else None

    def pending_attempts(self) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM reminder_attempts WHERE state IN ('pending','failed') ORDER BY reminder_at, id"
        ).fetchall()

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

    def fail_agenda_delivery(self, delivery_id: int, error_code: str = "DISCORD_DELIVERY_FAILED") -> None:
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            """UPDATE agenda_deliveries SET state='failed', attempt_count=attempt_count+1,
            last_error_code=?, updated_at=? WHERE id=?""",
            (error_code, now, delivery_id),
        )
        self.connection.commit()

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
