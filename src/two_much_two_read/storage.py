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
from .schemas import EmailExtraction, ResolvedContent, SourceDocument

SCHEMA_VERSION = 2
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS documents(
  id INTEGER PRIMARY KEY, source_type TEXT NOT NULL CHECK(source_type IN ('gmail','hackernews')),
  source_id TEXT NOT NULL, external_id TEXT NOT NULL, published_at TEXT NOT NULL, title TEXT NOT NULL,
  author TEXT, source_url TEXT, discussion_url TEXT,
  content_basis TEXT NOT NULL CHECK(content_basis IN ('newsletter','article','hn_self_post','metadata')),
  content_sha256 TEXT NOT NULL, content_characters INTEGER NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('discovered','processing','processed','failed')),
  attempt_count INTEGER NOT NULL DEFAULT 0, last_error_code TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(source_type, source_id, external_id)
);
CREATE TABLE IF NOT EXISTS gmail_document_state(
  document_id INTEGER PRIMARY KEY REFERENCES documents(id), gmail_message_id TEXT NOT NULL UNIQUE,
  gmail_thread_id TEXT NOT NULL, label_sync_state TEXT CHECK(label_sync_state IN ('synced','failed')),
  label_sync_error_code TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES documents(id), normalized_title TEXT NOT NULL,
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
INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES({SCHEMA_VERSION}, datetime('now'));
"""


class DatabaseSchemaResetRequiredError(ValueError):
    pass


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        version = self._schema_version()
        if version != SCHEMA_VERSION and (version is not None or self._has_user_tables()):
            self.connection.close()
            raise DatabaseSchemaResetRequiredError(
                f"DATABASE_SCHEMA_RESET_REQUIRED: back up {path} and remove it before rerunning 2much2read"
            )
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.executescript(SCHEMA)

    def _schema_version(self) -> int | None:
        row = self.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone()
        if row is None:
            return None
        row = self.connection.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1").fetchone()
        return int(row[0]) if row is not None else -1

    def _has_user_tables(self) -> bool:
        return bool(
            self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
            ).fetchone()
        )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection:
            yield self.connection

    def discover_document(self, document: SourceDocument, content: ResolvedContent, force: bool = False) -> int | None:
        now = datetime.now(UTC).isoformat()
        existing = self.connection.execute(
            "SELECT id, state FROM documents WHERE source_type=? AND source_id=? AND external_id=?",
            (document.source_type, document.source_id, document.external_id),
        ).fetchone()
        if existing and (force or existing["state"] == "discovered"):
            return int(existing["id"])
        cursor = self.connection.execute(
            """INSERT OR IGNORE INTO documents
            (source_type,source_id,external_id,published_at,title,author,source_url,discussion_url,
             content_basis,content_sha256,content_characters,state,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,'discovered',?,?)""",
            (
                document.source_type,
                document.source_id,
                document.external_id,
                document.published_at.isoformat(),
                document.title,
                document.author,
                str(document.source_url) if document.source_url else None,
                str(document.discussion_url) if document.discussion_url else None,
                content.basis,
                hashlib.sha256(content.text.encode()).hexdigest(),
                len(content.text),
                now,
                now,
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid) if cursor.rowcount and cursor.lastrowid is not None else None

    def discover_gmail_document(
        self,
        gmail_id: str,
        thread_id: str,
        source_id: str,
        received_at: datetime,
        subject: str,
        sender: str,
        body: str,
        truncated: bool,
        force: bool = False,
    ) -> int | None:
        document = SourceDocument(
            source_type="gmail",
            source_id=source_id,
            external_id=gmail_id,
            title=subject,
            author=sender or None,
            published_at=received_at,
        )
        document_id = self.discover_document(
            document,
            ResolvedContent(document=document, text=body, basis="newsletter", truncated=truncated),
            force,
        )
        if document_id is not None:
            now = datetime.now(UTC).isoformat()
            self.connection.execute(
                """INSERT INTO gmail_document_state(document_id,gmail_message_id,gmail_thread_id,updated_at)
                VALUES(?,?,?,?) ON CONFLICT(document_id) DO UPDATE SET
                gmail_message_id=excluded.gmail_message_id,gmail_thread_id=excluded.gmail_thread_id,updated_at=excluded.updated_at""",
                (document_id, gmail_id, thread_id, now),
            )
            self.connection.commit()
        return document_id

    def gmail_document(self, gmail_id: str) -> sqlite3.Row | None:
        row = self.connection.execute(
            """SELECT d.id, d.state FROM documents d JOIN gmail_document_state g ON g.document_id=d.id
            WHERE g.gmail_message_id=?""",
            (gmail_id,),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    def store_extraction(
        self, document_id: int, extraction: EmailExtraction, replace: bool = False, finalize: bool = True
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self.transaction() as connection:
            if replace:
                connection.execute("DELETE FROM items WHERE document_id=?", (document_id,))
            for item in extraction.items:
                url = str(item.source_url) if item.source_url else None
                connection.execute(
                    """INSERT INTO items
                    (document_id,normalized_title,title,category,summary_zh_tw,why_it_matters_zh_tw,
                     source_url,canonical_url,importance,confidence,tags_json,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        document_id,
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
            if finalize:
                self._finalize_documents(connection, [document_id], now)

    @staticmethod
    def _finalize_documents(connection: sqlite3.Connection, document_ids: list[int], now: str) -> None:
        if not document_ids:
            return
        placeholders = ",".join("?" for _ in document_ids)
        connection.execute(
            f"UPDATE documents SET state='processed', last_error_code=NULL, updated_at=? WHERE id IN ({placeholders})",
            (now, *document_ids),
        )
        connection.execute(
            f"""UPDATE gmail_document_state SET label_sync_state=NULL,label_sync_error_code=NULL,updated_at=?
            WHERE document_id IN ({placeholders})""",
            (now, *document_ids),
        )

    def finalize_documents(self, document_ids: list[int]) -> None:
        with self.transaction() as connection:
            self._finalize_documents(connection, document_ids, datetime.now(UTC).isoformat())

    def fail_document(self, document_id: int, error_code: str) -> None:
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            """UPDATE documents SET state='failed', attempt_count=attempt_count+1,
            last_error_code=?, updated_at=? WHERE id=?""",
            (error_code, now, document_id),
        )
        self.connection.commit()

    def mark_label_synced(self, document_id: int) -> None:
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            """UPDATE gmail_document_state SET label_sync_state='synced',label_sync_error_code=NULL,updated_at=?
            WHERE document_id=?""",
            (now, document_id),
        )
        self.connection.commit()

    def fail_label_sync(self, document_id: int) -> None:
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            """UPDATE gmail_document_state SET label_sync_state='failed',label_sync_error_code='GMAIL_LABEL_SYNC_FAILED',
            updated_at=? WHERE document_id=?""",
            (now, document_id),
        )
        self.connection.commit()

    def gmail_documents_for_label_reconciliation(self) -> list[sqlite3.Row]:
        return self.connection.execute(
            """SELECT d.id, g.gmail_message_id, d.state FROM documents d
            JOIN gmail_document_state g ON g.document_id=d.id
            WHERE d.state IN ('processed','failed') ORDER BY d.id"""
        ).fetchall()

    def items_for_documents(self, document_ids: list[int], limit: int) -> list[dict[str, object]]:
        if not document_ids:
            return []
        placeholders = ",".join("?" for _ in document_ids)
        rows = self.connection.execute(
            f"""SELECT i.*, d.published_at FROM items i JOIN documents d ON d.id=i.document_id
            WHERE i.document_id IN ({placeholders}) ORDER BY d.published_at DESC LIMIT ?""",
            (*document_ids, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def save_digest(
        self,
        digest_key: str,
        period_start: str,
        period_end: str,
        timezone: str,
        content: str,
        document_ids: list[int] | None = None,
    ) -> int | None:
        now = datetime.now(UTC).isoformat()
        with self.transaction() as connection:
            cursor = connection.execute(
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
            if cursor.rowcount and document_ids:
                self._finalize_documents(connection, document_ids, now)
            return int(cursor.lastrowid) if cursor.rowcount and cursor.lastrowid is not None else None

    def digest_exists(self, digest_key: str) -> bool:
        return self.connection.execute("SELECT 1 FROM digests WHERE digest_key=?", (digest_key,)).fetchone() is not None

    def pending_digests(self) -> list[sqlite3.Row]:
        return self.connection.execute("SELECT * FROM digests WHERE state IN ('pending','failed') ORDER BY id").fetchall()

    def pending_digest(self, digest_id: int) -> sqlite3.Row | None:
        row = self.connection.execute(
            "SELECT * FROM digests WHERE id=? AND state IN ('pending','failed')",
            (digest_id,),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    def start_run(self, run_type: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO runs(run_type,started_at,status) VALUES(?,?,?)",
            (run_type, datetime.now(UTC).isoformat(), "running"),
        )
        self.connection.commit()
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)

    def finish_run(
        self, run_id: int, status: str, discovered: int, processed: int, failed: int, delivered: int, error_summary: str | None
    ) -> None:
        self.connection.execute(
            """UPDATE runs SET finished_at=?,status=?,discovered_count=?,processed_count=?,failed_count=?,
            delivered_digest_count=?,error_summary=? WHERE id=?""",
            (datetime.now(UTC).isoformat(), status, discovered, processed, failed, delivered, error_summary, run_id),
        )
        self.connection.commit()

    def counts(self) -> dict[str, int]:
        return {
            table: int(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("documents", "gmail_document_state", "items", "digests", "runs")
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
            for table in ("items", "gmail_document_state", "documents", "digests", "runs"):
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

    def fail_delivery(self, digest_id: int, error_code: str = "DISCORD_DELIVERY_FAILED") -> None:
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            """UPDATE digests SET state='failed', delivery_attempt_count=delivery_attempt_count+1,
            last_error_code=?, updated_at=? WHERE id=?""",
            (error_code, now, digest_id),
        )
        self.connection.commit()

    def reset_corrupt_delivery(self, digest_id: int) -> bool:
        cursor = self.connection.execute(
            """UPDATE digests SET state='pending', discord_message_ids_json=NULL, last_error_code=NULL, updated_at=?
            WHERE id=? AND state='failed' AND last_error_code='DISCORD_MESSAGE_IDS_CORRUPT'""",
            (datetime.now(UTC).isoformat(), digest_id),
        )
        self.connection.commit()
        return bool(cursor.rowcount)

    def close(self) -> None:
        self.connection.close()
