from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal, Self

from two_read_runtime.permissions import prepare_private_file, repair_sqlite_files
from two_read_runtime.sqlite_snapshot import reading_connection

from .usage import SECONDS_PER_MINUTE, Counted, PhoneEvent

SCHEMA = """
CREATE TABLE IF NOT EXISTS phone_events(
  id INTEGER PRIMARY KEY,
  app TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('open','close')),
  at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS phone_events_by_time ON phone_events(at, id);
CREATE TABLE IF NOT EXISTS computer_batches(
  id TEXT PRIMARY KEY,
  received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS computer_usage(
  minute TEXT NOT NULL,
  app TEXT NOT NULL,
  focused_seconds INTEGER NOT NULL,
  visible_seconds INTEGER NOT NULL,
  PRIMARY KEY(minute, app)
);
CREATE TABLE IF NOT EXISTS browser_usage(
  minute TEXT NOT NULL,
  domain TEXT NOT NULL,
  focused_seconds INTEGER NOT NULL,
  visible_seconds INTEGER NOT NULL,
  PRIMARY KEY(minute, domain)
);
CREATE TABLE IF NOT EXISTS summaries(
  day TEXT NOT NULL,
  destination_key TEXT NOT NULL,
  discord_message_ids_json TEXT NOT NULL,
  delivered_at TEXT NOT NULL,
  PRIMARY KEY(day, destination_key)
);
"""

READ_TABLES = ("phone_events", "computer_usage", "browser_usage")
# Long enough that no resend from the computer can outlive it: pending batches are kept for 14 days.
BATCH_RETENTION = timedelta(days=30)


def _add_usage(
    connection: sqlite3.Connection, table: Literal["computer_usage", "browser_usage"], rows: Iterable[Counted]
) -> None:
    name_column = "app" if table == "computer_usage" else "domain"
    # Capped at the minute's length: a clock corrected mid-minute must not make a minute of an app
    # worth more than a minute.
    connection.executemany(
        f"""INSERT INTO {table}(minute,{name_column},focused_seconds,visible_seconds)
        VALUES(?,?,MIN(?,{SECONDS_PER_MINUTE}),MIN(?,{SECONDS_PER_MINUTE}))
        ON CONFLICT(minute,{name_column}) DO UPDATE SET
          focused_seconds=MIN(focused_seconds+excluded.focused_seconds,{SECONDS_PER_MINUTE}),
          visible_seconds=MIN(visible_seconds+excluded.visible_seconds,{SECONDS_PER_MINUTE})""",
        [(row.minute, row.name, row.focused_seconds, row.visible_seconds) for row in rows],
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
            self.connection.executescript(SCHEMA)
            repair_sqlite_files(path)
        except Exception:
            self.connection.close()
            raise

    @classmethod
    def reading(cls, connection: sqlite3.Connection) -> Self:
        """Wrap a connection opened elsewhere for reporting, which must create nothing."""
        database = cls.__new__(cls)
        database.connection = connection
        return database

    def close(self) -> None:
        self.connection.close()

    def add_phone_event(self, app: str, kind: Literal["open", "close"], at: datetime) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO phone_events(app,kind,at) VALUES(?,?,?)", (app, kind, at.astimezone(UTC).isoformat())
            )

    def add_computer_batch(self, batch_id: str, rows: Iterable[Counted], now: datetime) -> bool:
        """False, adding nothing, for a batch already taken: one whose reply was lost is sent again."""
        with self.connection:
            taken = self.connection.execute(
                "INSERT OR IGNORE INTO computer_batches(id,received_at) VALUES(?,?)", (batch_id, now.astimezone(UTC).isoformat())
            )
            if taken.rowcount == 0:
                return False
            _add_usage(self.connection, "computer_usage", rows)
            self.connection.execute(
                "DELETE FROM computer_batches WHERE received_at<?", ((now - BATCH_RETENTION).astimezone(UTC).isoformat(),)
            )
        return True

    def add_browser_usage(self, rows: Iterable[Counted]) -> None:
        with self.connection:
            _add_usage(self.connection, "browser_usage", rows)

    def phone_events(self, start: datetime, end: datetime) -> list[PhoneEvent]:
        rows = self.connection.execute(
            "SELECT app,kind,at FROM phone_events WHERE at>=? AND at<? ORDER BY at,id",
            (start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat()),
        )
        return [PhoneEvent(str(row["app"]), row["kind"], datetime.fromisoformat(str(row["at"]))) for row in rows]

    def usage(self, table: Literal["computer_usage", "browser_usage"], start: datetime, end: datetime) -> list[Counted]:
        name_column = "app" if table == "computer_usage" else "domain"
        rows = self.connection.execute(
            f"SELECT minute,{name_column} AS name,focused_seconds,visible_seconds FROM {table} WHERE minute>=? AND minute<?",
            (start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M"), end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M")),
        )
        return [
            Counted(str(row["minute"]), str(row["name"]), int(row["focused_seconds"]), int(row["visible_seconds"]))
            for row in rows
        ]

    def summary_sent(self, day: date, destination_key: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM summaries WHERE day=? AND destination_key=?", (day.isoformat(), destination_key)
        ).fetchone()
        return row is not None

    def record_summary(self, day: date, destination_key: str, message_ids: list[str], now: datetime) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO summaries(day,destination_key,discord_message_ids_json,delivered_at) VALUES(?,?,?,?)
                ON CONFLICT(day,destination_key) DO UPDATE SET
                  discord_message_ids_json=excluded.discord_message_ids_json,
                  delivered_at=excluded.delivered_at""",
                (day.isoformat(), destination_key, json.dumps(message_ids), now.astimezone(UTC).isoformat()),
            )


@contextmanager
def snapshot(path: Path) -> Iterator[Database | None]:
    """Read without changing anything on disk, or None when there is nothing recorded yet."""
    with reading_connection(path, READ_TABLES) as connection:
        yield None if connection is None else Database.reading(connection)
