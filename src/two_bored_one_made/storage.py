from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, time
from pathlib import Path

from two_read_runtime.permissions import prepare_private_file, repair_sqlite_files

SCHEMA = """
CREATE TABLE IF NOT EXISTS nudge_sends(
  id INTEGER PRIMARY KEY,
  nudge_id TEXT NOT NULL,
  slot_date TEXT NOT NULL,
  slot_time TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('delivered','failed')),
  attempt_count INTEGER NOT NULL DEFAULT 0,
  message TEXT NOT NULL,
  destination_key TEXT NOT NULL,
  discord_message_ids_json TEXT,
  last_error_code TEXT,
  delivered_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(nudge_id, slot_date, slot_time)
);
CREATE INDEX IF NOT EXISTS nudge_sends_by_nudge ON nudge_sends(nudge_id, state);
"""


class Database:
    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        if read_only:
            self.connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        else:
            prepare_private_file(path)
            repair_sqlite_files(path)
            self.connection = sqlite3.connect(path)
        try:
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA busy_timeout=5000")
            if not read_only:
                self.connection.execute("PRAGMA journal_mode=WAL")
                self.connection.execute("PRAGMA foreign_keys=ON")
                self.connection.executescript(SCHEMA)
                repair_sqlite_files(path)
        except Exception:
            self.connection.close()
            raise

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection:
            yield self.connection

    def close(self) -> None:
        self.connection.close()

    def delivered_counts(self) -> dict[str, int]:
        """How many times each nudge has actually reached Discord.

        The count is of deliveries, not of elapsed slots, so an outage costs a nudge nothing: the
        run that could not send does not consume one of the sends the configuration asked for.
        """
        return {
            str(row["nudge_id"]): int(row["sends"])
            for row in self.connection.execute(
                "SELECT nudge_id, COUNT(*) AS sends FROM nudge_sends WHERE state='delivered' GROUP BY nudge_id"
            )
        }

    def slot_states(self, slot_date: date) -> dict[tuple[str, str], tuple[str, int]]:
        return {
            (str(row["nudge_id"]), str(row["slot_time"])): (str(row["state"]), int(row["attempt_count"]))
            for row in self.connection.execute(
                "SELECT nudge_id, slot_time, state, attempt_count FROM nudge_sends WHERE slot_date=?",
                (slot_date.isoformat(),),
            )
        }

    def record_send(
        self,
        nudge_id: str,
        slot_date: date,
        slot_time: time,
        *,
        message: str,
        destination_key: str,
        delivered: bool,
        message_ids: list[str] | None = None,
        error_code: str | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        state = "delivered" if delivered else "failed"
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO nudge_sends
                (nudge_id,slot_date,slot_time,state,attempt_count,message,destination_key,
                 discord_message_ids_json,last_error_code,delivered_at,created_at,updated_at)
                VALUES(?,?,?,?,1,?,?,?,?,?,?,?)
                ON CONFLICT(nudge_id, slot_date, slot_time) DO UPDATE SET
                  state=excluded.state,
                  attempt_count=nudge_sends.attempt_count + 1,
                  message=excluded.message,
                  destination_key=excluded.destination_key,
                  discord_message_ids_json=excluded.discord_message_ids_json,
                  last_error_code=excluded.last_error_code,
                  delivered_at=excluded.delivered_at,
                  updated_at=excluded.updated_at""",
                (
                    nudge_id,
                    slot_date.isoformat(),
                    slot_time.isoformat("minutes"),
                    state,
                    message,
                    destination_key,
                    json.dumps(message_ids) if message_ids else None,
                    error_code,
                    now if delivered else None,
                    now,
                    now,
                ),
            )

    def reset_nudge(self, nudge_id: str) -> int:
        with self.transaction() as connection:
            cursor = connection.execute("DELETE FROM nudge_sends WHERE nudge_id=?", (nudge_id,))
            return int(cursor.rowcount)

    def last_delivery(self, nudge_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT delivered_at FROM nudge_sends WHERE nudge_id=? AND state='delivered' ORDER BY delivered_at DESC LIMIT 1",
            (nudge_id,),
        ).fetchone()
        return None if row is None or row["delivered_at"] is None else str(row["delivered_at"])
