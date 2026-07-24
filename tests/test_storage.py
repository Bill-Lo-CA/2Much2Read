import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from two_much_two_read.schemas import DigestItem, EmailExtraction, ResolvedContent, SourceDocument
from two_much_two_read.storage import Database, DatabaseSchemaResetRequiredError


def discover(database: Database, gmail_id: str, body: str = "body", *, force: bool = False) -> int | None:
    return database.discover_gmail_document(
        gmail_id,
        f"thread-{gmail_id}",
        "source",
        datetime(2026, 7, 23, tzinfo=UTC),
        "subject",
        "sender",
        body,
        False,
        force,
    )


def extraction(title: str = "Title") -> EmailExtraction:
    return EmailExtraction(
        source_id="source",
        newsletter_title="News",
        newsletter_date=None,
        overview_zh_tw="摘要",
        items=[
            DigestItem(
                title=title,
                category="OTHER",
                summary_zh_tw="摘要",
                why_it_matters_zh_tw="原因",
                source_url=None,
                importance=5,
                confidence=0.8,
                tags=[],
            )
        ],
    )


def test_document_and_digest_idempotency(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    document_id = discover(database, "gmail-1")
    assert document_id is not None
    assert discover(database, "gmail-1") == document_id
    database.store_extraction(document_id, extraction())
    assert len(database.items_for_documents([document_id], 10)) == 1
    assert discover(database, "gmail-1") is None
    failed_id = discover(database, "gmail-2")
    assert failed_id is not None
    database.fail_document(failed_id, "OLLAMA_SCHEMA_INVALID")
    assert discover(database, "gmail-2") is None
    digest_id = database.save_digest("daily:1", "start", "end", "UTC", "digest")
    assert digest_id is not None
    assert database.pending_digest(digest_id)["rendered_content"] == "digest"
    assert database.save_digest("daily:1", "start", "end", "UTC", "digest") is None
    database.close()


def test_generic_document_identity_is_source_scoped(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    document = SourceDocument(
        source_type="hackernews",
        source_id="hn-best",
        external_id="123",
        title="Article",
        published_at=datetime(2026, 7, 23, tzinfo=UTC),
    )
    content = ResolvedContent(document=document, text="article text", basis="article", truncated=False)

    document_id = database.discover_document(document, content)

    assert document_id is not None
    row = database.connection.execute("SELECT source_type,source_id,external_id,content_basis FROM documents").fetchone()
    assert tuple(row) == ("hackernews", "hn-best", "123", "article")
    database.close()


def test_force_replaces_existing_extraction(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    document_id = discover(database, "gmail-1")
    assert document_id is not None
    database.store_extraction(document_id, extraction("Old title"))

    forced_id = discover(database, "gmail-1", "new body", force=True)
    assert forced_id == document_id
    assert database.items_for_documents([document_id], 10)[0]["title"] == "Old title"
    database.store_extraction(forced_id, extraction("New title"), replace=True)
    assert database.items_for_documents([document_id], 10)[0]["title"] == "New title"
    database.close()


def test_items_for_documents_excludes_prior_runs(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    first_id = discover(database, "gmail-1")
    second_id = discover(database, "gmail-2")
    assert first_id is not None and second_id is not None
    database.store_extraction(first_id, extraction("Item"))
    database.store_extraction(second_id, extraction("Item"))

    assert [row["document_id"] for row in database.items_for_documents([second_id], 10)] == [second_id]
    database.close()


def test_save_digest_finalizes_staged_documents_atomically(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    document_id = discover(database, "gmail-1")
    assert document_id is not None
    database.store_extraction(
        document_id,
        EmailExtraction(source_id="source", newsletter_title="News", newsletter_date=None, overview_zh_tw="摘要", items=[]),
        finalize=False,
    )

    assert database.gmail_document("gmail-1")["state"] == "discovered"
    assert database.save_digest("daily:1", "start", "end", "UTC", "digest", [document_id]) is not None
    assert database.gmail_document("gmail-1")["state"] == "processed"
    database.close()


def test_legacy_schema_requires_an_explicit_reset(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE schema_version(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);"
        "INSERT INTO schema_version VALUES(1, 'now');"
    )
    connection.close()

    with pytest.raises(DatabaseSchemaResetRequiredError, match="DATABASE_SCHEMA_RESET_REQUIRED"):
        Database(path)


def test_v2_schema_upgrades_without_losing_documents(tmp_path: Path) -> None:
    path = tmp_path / "v2.sqlite3"
    database = Database(path)
    assert discover(database, "gmail-1") is not None
    database.connection.execute("DROP TABLE hackernews_document_state")
    database.connection.execute("DELETE FROM schema_version")
    database.connection.execute("INSERT INTO schema_version VALUES(2, 'now')")
    database.connection.commit()
    database.close()

    upgraded = Database(path)

    assert upgraded.connection.execute("SELECT version FROM schema_version ORDER BY version DESC").fetchone()[0] == 3
    assert upgraded.connection.execute("SELECT gmail_message_id FROM gmail_document_state").fetchone()[0] == "gmail-1"
    assert upgraded.connection.execute("SELECT 1 FROM sqlite_master WHERE name='hackernews_document_state'").fetchone()[0] == 1
    upgraded.close()


def test_backup_and_reset(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    assert discover(database, "gmail-1") is not None
    backup_path = tmp_path / "backup.sqlite3"

    database.backup(backup_path)
    counts = database.reset()

    assert counts["documents"] == 1
    assert database.counts() == {
        "documents": 0,
        "gmail_document_state": 0,
        "hackernews_document_state": 0,
        "items": 0,
        "digests": 0,
        "runs": 0,
    }
    assert backup_path.stat().st_mode & 0o777 == 0o600
    backup = Database(backup_path)
    assert backup.counts()["documents"] == 1
    backup.close()
    database.close()
