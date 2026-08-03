from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from two_read_runtime.discord import DiscordDestination

from .digest import canonical_url, normalized_title
from .schemas import DigestItem, EmailExtraction, ItemAnalysis, ResolvedContent, SourceDocument

SCHEMA_VERSION = 8
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
CREATE TABLE IF NOT EXISTS hackernews_document_state(
  document_id INTEGER PRIMARY KEY REFERENCES documents(id), hn_item_id INTEGER NOT NULL,
  feed TEXT NOT NULL, feed_rank INTEGER NOT NULL, score INTEGER NOT NULL, descendants INTEGER NOT NULL,
  requested_url TEXT, final_url TEXT,
  fetch_status TEXT NOT NULL, fetched_at TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES documents(id), normalized_title TEXT NOT NULL,
  title TEXT NOT NULL, category TEXT NOT NULL, summary_zh_tw TEXT NOT NULL,
  why_it_matters_zh_tw TEXT NOT NULL, source_url TEXT, canonical_url TEXT,
  raw_url TEXT, resolved_url TEXT,
  url_match_status TEXT NOT NULL DEFAULT 'not_applicable', url_match_method TEXT, url_match_confidence REAL,
  url_resolution_status TEXT NOT NULL DEFAULT 'not_applicable', url_error_code TEXT, url_checked_at TEXT,
  importance INTEGER NOT NULL, confidence REAL NOT NULL, tags_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS url_resolution_cache(
  raw_url_hash TEXT PRIMARY KEY, raw_url_host TEXT NOT NULL, resolved_url TEXT, canonical_url TEXT,
  status TEXT NOT NULL, error_code TEXT, checked_at TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS digests(
  id INTEGER PRIMARY KEY, digest_key TEXT NOT NULL UNIQUE, period_start TEXT NOT NULL, period_end TEXT NOT NULL,
  timezone TEXT NOT NULL, content_sha256 TEXT NOT NULL, rendered_content TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('pending','delivered','failed')), delivery_attempt_count INTEGER NOT NULL DEFAULT 0,
  discord_message_ids_json TEXT, discord_destination_key TEXT, delivered_at TEXT, last_error_code TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS digest_deliveries(
  id INTEGER PRIMARY KEY, digest_id INTEGER NOT NULL REFERENCES digests(id) ON DELETE CASCADE,
  destination_key TEXT NOT NULL, transport TEXT NOT NULL CHECK(transport IN ('webhook','bot')),
  state TEXT NOT NULL CHECK(state IN ('pending','delivered','failed')),
  delivery_attempt_count INTEGER NOT NULL DEFAULT 0, discord_message_ids_json TEXT, delivered_at TEXT,
  last_error_code TEXT, retired_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(digest_id, destination_key)
);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY, run_type TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
  discovered_count INTEGER NOT NULL DEFAULT 0, processed_count INTEGER NOT NULL DEFAULT 0,
  failed_count INTEGER NOT NULL DEFAULT 0, delivered_digest_count INTEGER NOT NULL DEFAULT 0, error_summary TEXT
);
INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES({SCHEMA_VERSION}, datetime('now'));
"""

MIGRATE_V2_TO_V3 = """
CREATE TABLE IF NOT EXISTS hackernews_document_state(
  document_id INTEGER PRIMARY KEY REFERENCES documents(id), hn_item_id INTEGER NOT NULL,
  feed TEXT NOT NULL, feed_rank INTEGER NOT NULL, score INTEGER NOT NULL, descendants INTEGER NOT NULL,
  requested_url TEXT, final_url TEXT,
  fetch_status TEXT NOT NULL CHECK(fetch_status IN ('not_requested')), fetched_at TEXT, updated_at TEXT NOT NULL
);
INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(3, datetime('now'));
"""

MIGRATE_V3_TO_V4 = """
BEGIN;
ALTER TABLE hackernews_document_state RENAME TO hackernews_document_state_v3;
CREATE TABLE hackernews_document_state(
  document_id INTEGER PRIMARY KEY REFERENCES documents(id), hn_item_id INTEGER NOT NULL,
  feed TEXT NOT NULL, feed_rank INTEGER NOT NULL, score INTEGER NOT NULL, descendants INTEGER NOT NULL,
  requested_url TEXT, final_url TEXT,
  fetch_status TEXT NOT NULL, fetched_at TEXT, updated_at TEXT NOT NULL
);
INSERT INTO hackernews_document_state
SELECT document_id,hn_item_id,feed,feed_rank,score,descendants,requested_url,final_url,fetch_status,fetched_at,updated_at
FROM hackernews_document_state_v3;
DROP TABLE hackernews_document_state_v3;
INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(4, datetime('now'));
COMMIT;
"""


class DatabaseSchemaResetRequiredError(ValueError):
    pass


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        version = self._schema_version()
        if version == 2:
            self.connection.executescript(MIGRATE_V2_TO_V3)
            version = 3
        if version == 3:
            self.connection.executescript(MIGRATE_V3_TO_V4)
            version = 4
        if version == 4:
            self._migrate_v4_to_v5()
            version = 5
        if version == 5:
            self._migrate_v5_to_v6()
            version = 6
        if version == 6:
            self._migrate_v6_to_v7()
            version = 7
        if version == 7:
            self._migrate_v7_to_v8()
            version = 8
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

    def _migrate_v4_to_v5(self) -> None:
        columns = {str(row["name"]) for row in self.connection.execute("PRAGMA table_info(items)")}
        additions = {
            "raw_url": "TEXT",
            "resolved_url": "TEXT",
            "url_match_status": "TEXT NOT NULL DEFAULT 'not_applicable'",
            "url_match_method": "TEXT",
            "url_match_confidence": "REAL",
            "url_resolution_status": "TEXT NOT NULL DEFAULT 'not_applicable'",
            "url_error_code": "TEXT",
            "url_checked_at": "TEXT",
        }
        with self.transaction() as connection:
            for name, definition in additions.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE items ADD COLUMN {name} {definition}")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS url_resolution_cache(
                raw_url_hash TEXT PRIMARY KEY, raw_url_host TEXT NOT NULL, resolved_url TEXT, canonical_url TEXT,
                status TEXT NOT NULL, error_code TEXT, checked_at TEXT NOT NULL, expires_at TEXT NOT NULL
                )"""
            )
            connection.execute("INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(5, datetime('now'))")

    def _migrate_v5_to_v6(self) -> None:
        with self.transaction() as connection:
            columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(digests)")}
            if "discord_destination_key" not in columns:
                connection.execute("ALTER TABLE digests ADD COLUMN discord_destination_key TEXT")
                connection.execute(
                    "UPDATE digests SET discord_destination_key='webhook' WHERE discord_message_ids_json IS NOT NULL"
                )
            connection.execute("INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(6, datetime('now'))")

    def _migrate_v6_to_v7(self) -> None:
        with self.transaction() as connection:
            columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(digests)")}
            if "discord_destination_key" not in columns:
                connection.execute("ALTER TABLE digests ADD COLUMN discord_destination_key TEXT")
                connection.execute(
                    "UPDATE digests SET discord_destination_key='webhook' WHERE discord_message_ids_json IS NOT NULL"
                )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS digest_deliveries(
                id INTEGER PRIMARY KEY, digest_id INTEGER NOT NULL REFERENCES digests(id) ON DELETE CASCADE,
                destination_key TEXT NOT NULL, transport TEXT NOT NULL CHECK(transport IN ('webhook','bot')),
                state TEXT NOT NULL CHECK(state IN ('pending','delivered','failed')),
                delivery_attempt_count INTEGER NOT NULL DEFAULT 0, discord_message_ids_json TEXT, delivered_at TEXT,
                last_error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(digest_id, destination_key)
                )"""
            )
            connection.execute("INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(7, datetime('now'))")

    def _migrate_v7_to_v8(self) -> None:
        with self.transaction() as connection:
            columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(digest_deliveries)")}
            if "retired_at" not in columns:
                connection.execute("ALTER TABLE digest_deliveries ADD COLUMN retired_at TEXT")
            connection.execute("INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(8, datetime('now'))")

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

    def store_hackernews_metadata(
        self,
        document: SourceDocument,
        feed: str,
        feed_rank: int,
        score: int,
        descendants: int,
        *,
        force: bool = False,
    ) -> tuple[int, bool]:
        now = datetime.now(UTC).isoformat()
        existing = self.connection.execute(
            "SELECT id FROM documents WHERE source_type=? AND source_id=? AND external_id=?",
            (document.source_type, document.source_id, document.external_id),
        ).fetchone()
        created = existing is None
        if existing is None:
            document_id = self.discover_document(
                document,
                ResolvedContent(document=document, text="", basis="metadata", truncated=False),
            )
            assert document_id is not None
        else:
            document_id = int(existing["id"])
            self.connection.execute(
                """UPDATE documents SET published_at=?,title=?,author=?,source_url=?,discussion_url=?,
                state=CASE WHEN ? THEN 'discovered' ELSE state END,
                last_error_code=CASE WHEN ? THEN NULL ELSE last_error_code END,updated_at=? WHERE id=?""",
                (
                    document.published_at.isoformat(),
                    document.title,
                    document.author,
                    str(document.source_url) if document.source_url else None,
                    str(document.discussion_url) if document.discussion_url else None,
                    force,
                    force,
                    now,
                    document_id,
                ),
            )
        self.connection.execute(
            """INSERT INTO hackernews_document_state
            (document_id,hn_item_id,feed,feed_rank,score,descendants,requested_url,fetch_status,updated_at)
            VALUES(?,?,?,?,?,?,?,'not_requested',?)
            ON CONFLICT(document_id) DO UPDATE SET hn_item_id=excluded.hn_item_id,feed=excluded.feed,
            feed_rank=excluded.feed_rank,score=excluded.score,descendants=excluded.descendants,
            final_url=CASE WHEN hackernews_document_state.requested_url IS NOT excluded.requested_url
                THEN NULL ELSE final_url END,
            fetch_status=CASE WHEN hackernews_document_state.requested_url IS NOT excluded.requested_url
                THEN 'not_requested' ELSE fetch_status END,
            fetched_at=CASE WHEN hackernews_document_state.requested_url IS NOT excluded.requested_url
                THEN NULL ELSE fetched_at END,
            requested_url=excluded.requested_url,updated_at=excluded.updated_at""",
            (
                document_id,
                int(document.external_id),
                feed,
                feed_rank,
                score,
                descendants,
                str(document.source_url) if document.source_url else None,
                now,
            ),
        )
        self.connection.commit()
        return document_id, created

    def hackernews_fetch_status(self, document_id: int) -> str:
        row = self.connection.execute(
            "SELECT fetch_status FROM hackernews_document_state WHERE document_id=?", (document_id,)
        ).fetchone()
        assert row is not None
        return str(row["fetch_status"])

    def hackernews_document(self, source_id: str, external_id: str) -> sqlite3.Row | None:
        row = self.connection.execute(
            """SELECT id,state FROM documents WHERE source_type='hackernews' AND source_id=? AND external_id=?""",
            (source_id, external_id),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    def failed_hackernews_documents(self, source_id: str, limit: int) -> list[sqlite3.Row]:
        return self.connection.execute(
            """SELECT d.id,d.external_id,h.feed_rank FROM documents d
            JOIN hackernews_document_state h ON h.document_id=d.id
            WHERE d.source_type='hackernews' AND d.source_id=? AND d.state='failed'
            ORDER BY d.updated_at,d.id LIMIT ?""",
            (source_id, limit),
        ).fetchall()

    def store_hackernews_resolution(self, document_id: int, content: ResolvedContent) -> None:
        now = datetime.now(UTC).isoformat()
        with self.transaction() as connection:
            connection.execute(
                """UPDATE documents SET content_basis=?,content_sha256=?,content_characters=?,state='discovered',
                last_error_code=NULL,updated_at=? WHERE id=?""",
                (
                    content.basis,
                    hashlib.sha256(content.text.encode()).hexdigest(),
                    len(content.text),
                    now,
                    document_id,
                ),
            )
            connection.execute(
                """UPDATE hackernews_document_state SET final_url=?,fetch_status='fetched',fetched_at=?,updated_at=?
                WHERE document_id=?""",
                (str(content.final_url) if content.final_url else None, now, now, document_id),
            )

    def record_hackernews_fetch_failure(self, document_id: int, error_code: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self.transaction() as connection:
            connection.execute(
                """UPDATE documents SET state='failed',attempt_count=attempt_count+1,last_error_code=?,updated_at=?
                WHERE id=?""",
                (error_code, now, document_id),
            )
            connection.execute(
                """UPDATE hackernews_document_state SET fetch_status=?,fetched_at=?,updated_at=? WHERE document_id=?""",
                (error_code, now, now, document_id),
            )

    def store_items(self, document_id: int, items: list[DigestItem], replace: bool = False, finalize: bool = True) -> None:
        now = datetime.now(UTC).isoformat()
        with self.transaction() as connection:
            if replace:
                connection.execute("DELETE FROM items WHERE document_id=?", (document_id,))
            for item in items:
                url = str(item.source_url) if item.source_url else None
                connection.execute(
                    """INSERT INTO items
                    (document_id,normalized_title,title,category,summary_zh_tw,why_it_matters_zh_tw,
                     source_url,canonical_url,raw_url,resolved_url,url_match_status,url_match_method,url_match_confidence,
                     url_resolution_status,url_error_code,url_checked_at,importance,confidence,tags_json,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        document_id,
                        normalized_title(item.title),
                        item.title,
                        item.category,
                        item.summary_zh_tw,
                        item.why_it_matters_zh_tw,
                        url,
                        str(item.canonical_url) if item.canonical_url else canonical_url(url),
                        str(item.raw_url) if item.raw_url else None,
                        str(item.resolved_url) if item.resolved_url else None,
                        item.url_match_status,
                        item.url_match_method,
                        item.url_match_confidence,
                        item.url_resolution_status,
                        item.url_error_code,
                        item.url_checked_at.isoformat() if item.url_checked_at else None,
                        item.importance,
                        item.confidence,
                        json.dumps(item.tags),
                        now,
                    ),
                )
            if finalize:
                self._finalize_documents(connection, [document_id], now)

    def store_extraction(
        self, document_id: int, extraction: EmailExtraction, replace: bool = False, finalize: bool = True
    ) -> None:
        self.store_items(
            document_id,
            [
                DigestItem(
                    **{name: getattr(item, name) for name in ItemAnalysis.model_fields},
                    url_match_status="unmatched",
                    url_resolution_status="not_requested",
                )
                for item in extraction.items
            ],
            replace,
            finalize,
        )

    def cached_url_resolution(self, raw_url: str) -> dict[str, object] | None:
        raw_url_hash = hashlib.sha256(raw_url.encode()).hexdigest()
        row = self.connection.execute(
            "SELECT * FROM url_resolution_cache WHERE raw_url_hash=? AND expires_at>?",
            (raw_url_hash, datetime.now(UTC).isoformat()),
        ).fetchone()
        return dict(row) if row is not None else None

    def cache_url_resolution(
        self,
        raw_url: str,
        status: str,
        *,
        resolved_url: str | None = None,
        canonical_url: str | None = None,
        error_code: str | None = None,
    ) -> None:
        now = datetime.now(UTC)
        ttl = timedelta(days=30) if status in {"resolved", "blocked"} else timedelta(hours=1)
        self.connection.execute(
            """INSERT INTO url_resolution_cache
            (raw_url_hash,raw_url_host,resolved_url,canonical_url,status,error_code,checked_at,expires_at)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(raw_url_hash) DO UPDATE SET
            raw_url_host=excluded.raw_url_host,resolved_url=excluded.resolved_url,canonical_url=excluded.canonical_url,
            status=excluded.status,error_code=excluded.error_code,checked_at=excluded.checked_at,expires_at=excluded.expires_at""",
            (
                hashlib.sha256(raw_url.encode()).hexdigest(),
                (urlsplit(raw_url).hostname or "").casefold(),
                resolved_url,
                canonical_url,
                status,
                error_code,
                now.isoformat(),
                (now + ttl).isoformat(),
            ),
        )
        self.connection.commit()

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
            f"""SELECT i.*,d.published_at,d.source_id,d.source_type,d.discussion_url,d.content_basis,d.external_id,
            h.score AS hn_score,h.descendants AS hn_comments,h.final_url
            FROM items i JOIN documents d ON d.id=i.document_id
            LEFT JOIN hackernews_document_state h ON h.document_id=d.id
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
        destinations: list[DiscordDestination] | None = None,
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
            if cursor.rowcount and cursor.lastrowid is not None and destinations:
                self._create_digest_deliveries(connection, int(cursor.lastrowid), destinations, now)
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

    def _create_digest_deliveries(
        self, connection: sqlite3.Connection, digest_id: int, destinations: list[DiscordDestination], now: str
    ) -> None:
        connection.executemany(
            """INSERT OR IGNORE INTO digest_deliveries(digest_id,destination_key,transport,state,created_at,updated_at)
            VALUES(?,?,?,'pending',?,?)""",
            [(digest_id, destination.key, destination.transport, now, now) for destination in destinations],
        )

    def ensure_digest_deliveries(self, digest_id: int, destinations: list[DiscordDestination]) -> None:
        with self.transaction() as connection:
            self._create_digest_deliveries(connection, digest_id, destinations, datetime.now(UTC).isoformat())

    def reconcile_digest_deliveries(self, digest_id: int, destinations: list[DiscordDestination]) -> None:
        with self.transaction() as connection:
            now = datetime.now(UTC).isoformat()
            self._create_digest_deliveries(connection, digest_id, destinations, now)
            keys = [destination.key for destination in destinations]
            placeholders = ",".join("?" for _ in keys)
            connection.execute(
                f"""UPDATE digest_deliveries SET retired_at=?,updated_at=? WHERE digest_id=?
                AND destination_key NOT IN ({placeholders}) AND state IN ('pending','failed') AND retired_at IS NULL""",
                (now, now, digest_id, *keys),
            )
            connection.execute(
                f"""UPDATE digest_deliveries SET retired_at=NULL,updated_at=? WHERE digest_id=?
                AND destination_key IN ({placeholders})""",
                (now, digest_id, *keys),
            )
            self._refresh_digest_state(connection, digest_id)

    def migrate_legacy_digest_deliveries(self, digest_id: int, destinations: list[DiscordDestination]) -> None:
        with self.transaction() as connection:
            digest = connection.execute(
                "SELECT state,discord_message_ids_json,discord_destination_key,last_error_code FROM digests WHERE id=?",
                (digest_id,),
            ).fetchone()
            if digest is None:
                raise ValueError(f"digest {digest_id} not found")
            now = datetime.now(UTC).isoformat()
            self._create_digest_deliveries(connection, digest_id, destinations, now)
            source_key = digest["discord_destination_key"]
            destination = next((item for item in destinations if item.key == source_key), None)
            if destination is not None:
                connection.execute(
                    """UPDATE digest_deliveries SET state=?,discord_message_ids_json=?,last_error_code=?,updated_at=?
                    WHERE digest_id=? AND destination_key=?""",
                    (
                        digest["state"],
                        digest["discord_message_ids_json"],
                        digest["last_error_code"],
                        now,
                        digest_id,
                        destination.key,
                    ),
                )

    def has_digest_deliveries(self, digest_id: int) -> bool:
        return self.connection.execute("SELECT 1 FROM digest_deliveries WHERE digest_id=?", (digest_id,)).fetchone() is not None

    def has_digest_delivery(self, delivery_id: int) -> bool:
        return self.connection.execute("SELECT 1 FROM digest_deliveries WHERE id=?", (delivery_id,)).fetchone() is not None

    def digest_deliveries(self, digest_id: int, destinations: list[DiscordDestination]) -> list[sqlite3.Row]:
        if not destinations:
            return []
        keys = [destination.key for destination in destinations]
        placeholders = ",".join("?" for _ in keys)
        return self.connection.execute(
            f"""SELECT dd.*,d.rendered_content FROM digest_deliveries dd JOIN digests d ON d.id=dd.digest_id
            WHERE dd.digest_id=? AND dd.destination_key IN ({placeholders}) AND dd.state IN ('pending','failed')
            AND dd.retired_at IS NULL
            ORDER BY dd.id""",
            (digest_id, *keys),
        ).fetchall()

    def pending_digest_deliveries(self, destinations: list[DiscordDestination]) -> list[sqlite3.Row]:
        if not destinations:
            return []
        keys = [destination.key for destination in destinations]
        placeholders = ",".join("?" for _ in keys)
        return self.connection.execute(
            f"""SELECT dd.*,d.rendered_content FROM digest_deliveries dd JOIN digests d ON d.id=dd.digest_id
            WHERE dd.destination_key IN ({placeholders}) AND dd.state IN ('pending','failed')
            AND dd.retired_at IS NULL ORDER BY dd.id""",
            keys,
        ).fetchall()

    def _refresh_digest_state(self, connection: sqlite3.Connection, digest_id: int) -> None:
        states = [
            str(row["state"])
            for row in connection.execute(
                "SELECT state FROM digest_deliveries WHERE digest_id=? AND retired_at IS NULL",
                (digest_id,),
            )
        ]
        if not states:
            return
        now = datetime.now(UTC).isoformat()
        state = "delivered" if all(value == "delivered" for value in states) else "failed" if "failed" in states else "pending"
        connection.execute(
            "UPDATE digests SET state=?,delivered_at=?,updated_at=? WHERE id=?",
            (state, now if state == "delivered" else None, now, digest_id),
        )

    def finish_digest_delivery(self, delivery_id: int, message_ids: list[str], destinations: list[DiscordDestination]) -> None:
        now = datetime.now(UTC).isoformat()
        with self.transaction() as connection:
            row = connection.execute("SELECT digest_id FROM digest_deliveries WHERE id=?", (delivery_id,)).fetchone()
            if row is None:
                raise ValueError(f"digest delivery {delivery_id} not found")
            connection.execute(
                """UPDATE digest_deliveries SET state='delivered',delivered_at=?,discord_message_ids_json=?,
                delivery_attempt_count=delivery_attempt_count+1,last_error_code=NULL,updated_at=? WHERE id=?""",
                (now, json.dumps(message_ids), now, delivery_id),
            )
            self._refresh_digest_state(connection, int(row["digest_id"]))

    def record_digest_delivery_progress(self, delivery_id: int, message_ids: list[str]) -> None:
        self.connection.execute(
            "UPDATE digest_deliveries SET discord_message_ids_json=?,updated_at=? WHERE id=?",
            (json.dumps(message_ids), datetime.now(UTC).isoformat(), delivery_id),
        )
        self.connection.commit()

    def fail_digest_delivery(self, delivery_id: int, error_code: str, destinations: list[DiscordDestination]) -> None:
        now = datetime.now(UTC).isoformat()
        with self.transaction() as connection:
            row = connection.execute("SELECT digest_id FROM digest_deliveries WHERE id=?", (delivery_id,)).fetchone()
            if row is None:
                raise ValueError(f"digest delivery {delivery_id} not found")
            connection.execute(
                """UPDATE digest_deliveries SET state='failed',delivery_attempt_count=delivery_attempt_count+1,
                last_error_code=?,updated_at=? WHERE id=?""",
                (error_code, now, delivery_id),
            )
            self._refresh_digest_state(connection, int(row["digest_id"]))

    def reset_corrupt_digest_delivery(self, delivery_id: int, destinations: list[DiscordDestination]) -> bool:
        with self.transaction() as connection:
            row = connection.execute("SELECT digest_id FROM digest_deliveries WHERE id=?", (delivery_id,)).fetchone()
            if row is None:
                return False
            cursor = connection.execute(
                """UPDATE digest_deliveries SET state='pending',discord_message_ids_json=NULL,last_error_code=NULL,updated_at=?
                WHERE id=? AND state='failed' AND last_error_code='DISCORD_MESSAGE_IDS_CORRUPT'""",
                (datetime.now(UTC).isoformat(), delivery_id),
            )
            self._refresh_digest_state(connection, int(row["digest_id"]))
            return bool(cursor.rowcount)

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
            for table in (
                "documents",
                "gmail_document_state",
                "hackernews_document_state",
                "items",
                "digest_deliveries",
                "digests",
                "runs",
                "url_resolution_cache",
            )
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
            for table in (
                "url_resolution_cache",
                "items",
                "gmail_document_state",
                "hackernews_document_state",
                "documents",
                "digest_deliveries",
                "digests",
                "runs",
            ):
                connection.execute(f"DELETE FROM {table}")
        return counts

    def delivery_checkpoint(self, digest_id: int, destination_key: str) -> object:
        row = self.connection.execute(
            "SELECT discord_message_ids_json,discord_destination_key FROM digests WHERE id=?", (digest_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"digest {digest_id} not found")
        previous_key = row["discord_destination_key"]
        if previous_key != destination_key:
            self.connection.execute(
                "UPDATE digests SET discord_message_ids_json=NULL,discord_destination_key=?,updated_at=? WHERE id=?",
                (destination_key, datetime.now(UTC).isoformat(), digest_id),
            )
            self.connection.commit()
            return None
        return row["discord_message_ids_json"]

    def finish_delivery(self, digest_id: int, message_ids: list[str], destination_key: str | None = None) -> None:
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            """UPDATE digests SET state='delivered', delivered_at=?, discord_message_ids_json=?,discord_destination_key=?,
            delivery_attempt_count=delivery_attempt_count+1, last_error_code=NULL, updated_at=? WHERE id=?""",
            (now, json.dumps(message_ids), destination_key, now, digest_id),
        )
        self.connection.commit()

    def record_delivery_progress(self, digest_id: int, message_ids: list[str], destination_key: str | None = None) -> None:
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            "UPDATE digests SET discord_message_ids_json=?,discord_destination_key=?,updated_at=? WHERE id=?",
            (json.dumps(message_ids), destination_key, now, digest_id),
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
