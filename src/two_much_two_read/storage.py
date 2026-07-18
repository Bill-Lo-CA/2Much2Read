from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from .digest import canonical_url, normalized_title
from .schemas import EmailExtraction

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY, gmail_message_id TEXT NOT NULL UNIQUE, gmail_thread_id TEXT NOT NULL,
  source_id TEXT NOT NULL, received_at TEXT NOT NULL, subject TEXT NOT NULL, sender TEXT NOT NULL,
  body_sha256 TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('discovered','processing','processed','failed')),
  attempt_count INTEGER NOT NULL DEFAULT 0, last_error_code TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL REFERENCES messages(id), normalized_title TEXT NOT NULL,
  title TEXT NOT NULL, category TEXT NOT NULL, summary_zh_tw TEXT NOT NULL,
  why_it_matters_zh_tw TEXT NOT NULL, source_url TEXT, canonical_url TEXT,
  importance INTEGER NOT NULL, confidence REAL NOT NULL, tags_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS digests(
  id INTEGER PRIMARY KEY, digest_key TEXT NOT NULL UNIQUE, period_start TEXT NOT NULL, period_end TEXT NOT NULL,
  timezone TEXT NOT NULL, content_sha256 TEXT NOT NULL, rendered_content TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('pending','delivered','failed')), delivery_attempt_count INTEGER NOT NULL DEFAULT 0,
  discord_message_ids_json TEXT, delivered_at TEXT, last_error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY, run_type TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
  discovered_count INTEGER NOT NULL DEFAULT 0, processed_count INTEGER NOT NULL DEFAULT 0,
  failed_count INTEGER NOT NULL DEFAULT 0, delivered_digest_count INTEGER NOT NULL DEFAULT 0, error_summary TEXT
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

    def discover(
        self,
        gmail_id: str,
        thread_id: str,
        source_id: str,
        received_at: str,
        subject: str,
        sender: str,
        body: str,
        force: bool = False,
    ) -> int | None:
        now = datetime.now(UTC).isoformat()
        body_sha256 = hashlib.sha256(body.encode()).hexdigest()
        existing = self.connection.execute("SELECT id, state FROM messages WHERE gmail_message_id=?", (gmail_id,)).fetchone()
        if existing and (force or existing["state"] == "discovered"):
            return int(existing["id"])
        cursor = self.connection.execute(
            """INSERT OR IGNORE INTO messages
            (gmail_message_id,gmail_thread_id,source_id,received_at,subject,sender,body_sha256,state,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,'discovered',?,?)""",
            (
                gmail_id,
                thread_id,
                source_id,
                received_at,
                subject,
                sender,
                body_sha256,
                now,
                now,
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid) if cursor.rowcount and cursor.lastrowid is not None else None

    def store_extraction(self, message_id: int, extraction: EmailExtraction, replace: bool = False) -> None:
        now = datetime.now(UTC).isoformat()
        with self.transaction() as connection:
            if replace:
                connection.execute("DELETE FROM items WHERE message_id=?", (message_id,))
            for item in extraction.items:
                url = str(item.source_url) if item.source_url else None
                connection.execute(
                    """INSERT INTO items
                    (message_id,normalized_title,title,category,summary_zh_tw,why_it_matters_zh_tw,
                     source_url,canonical_url,importance,confidence,tags_json,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        message_id,
                        normalized_title(item.title),
                        item.title,
                        item.category,
                        item.summary_zh_tw,
                        item.why_it_matters_zh_tw,
                        url,
                        canonical_url(url),
                        item.importance,
                        item.confidence,
                        json.dumps(item.tags),
                        now,
                    ),
                )
            connection.execute("UPDATE messages SET state='processed', updated_at=? WHERE id=?", (now, message_id))

    def fail_message(self, message_id: int, error_code: str) -> None:
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            """UPDATE messages SET state='failed', attempt_count=attempt_count+1,
            last_error_code=?, updated_at=? WHERE id=?""",
            (error_code, now, message_id),
        )
        self.connection.commit()

    def recent_items(self, limit: int = 100) -> list[dict[str, object]]:
        rows = self.connection.execute(
            """SELECT i.*, m.received_at FROM items i JOIN messages m ON m.id=i.message_id
            WHERE m.state='processed' ORDER BY m.received_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def items_for_messages(self, message_ids: list[int], limit: int) -> list[dict[str, object]]:
        if not message_ids:
            return []
        placeholders = ",".join("?" for _ in message_ids)
        rows = self.connection.execute(
            f"""SELECT i.*, m.received_at FROM items i JOIN messages m ON m.id=i.message_id
            WHERE m.state='processed' AND i.message_id IN ({placeholders})
            ORDER BY m.received_at DESC LIMIT ?""",
            (*message_ids, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def save_digest(self, digest_key: str, period_start: str, period_end: str, timezone: str, content: str) -> int | None:
        now = datetime.now(UTC).isoformat()
        cursor = self.connection.execute(
            """INSERT OR IGNORE INTO digests
            (digest_key,period_start,period_end,timezone,content_sha256,rendered_content,state,created_at,updated_at)
            VALUES(?,?,?,?,?,?,'pending',?,?)""",
            (
                digest_key,
                period_start,
                period_end,
                timezone,
                hashlib.sha256(content.encode()).hexdigest(),
                content,
                now,
                now,
            ),
        )
        self.connection.commit()
        return cursor.lastrowid if cursor.rowcount else None

    def pending_digests(self) -> list[sqlite3.Row]:
        return self.connection.execute("SELECT * FROM digests WHERE state IN ('pending','failed') ORDER BY id").fetchall()

    def pending_digest(self, digest_id: int) -> sqlite3.Row | None:
        row = self.connection.execute(
            "SELECT * FROM digests WHERE id=? AND state IN ('pending','failed')",
            (digest_id,),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    def counts(self) -> dict[str, int]:
        return {
            table: int(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("messages", "items", "digests", "runs")
        }

    def backup(self, path: Path) -> None:
        backup = sqlite3.connect(path)
        try:
            self.connection.backup(backup)
        finally:
            backup.close()
        os.chmod(path, 0o600)

    def reset(self) -> dict[str, int]:
        counts = self.counts()
        with self.transaction() as connection:
            for table in ("items", "messages", "digests", "runs"):
                connection.execute(f"DELETE FROM {table}")
        return counts

    def finish_delivery(self, digest_id: int, message_ids: list[str]) -> None:
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            """UPDATE digests SET state='delivered', delivered_at=?, discord_message_ids_json=?,
            delivery_attempt_count=delivery_attempt_count+1, last_error_code=NULL, updated_at=? WHERE id=?""",
            (now, json.dumps(message_ids), now, digest_id),
        )
        self.connection.commit()

    def record_delivery_progress(self, digest_id: int, message_ids: list[str]) -> None:
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            "UPDATE digests SET discord_message_ids_json=?, updated_at=? WHERE id=?",
            (json.dumps(message_ids), now, digest_id),
        )
        self.connection.commit()

    def fail_delivery(self, digest_id: int) -> None:
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            """UPDATE digests SET state='failed', delivery_attempt_count=delivery_attempt_count+1,
            last_error_code='DISCORD_DELIVERY_FAILED', updated_at=? WHERE id=?""",
            (now, digest_id),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()
