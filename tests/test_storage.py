import os
import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from two_much_two_read.schemas import EmailExtraction, NewsletterItemAnalysis, ResolvedContent, SourceDocument
from two_much_two_read.storage import Database, DatabaseSchemaResetRequiredError
from two_read_runtime.discord import configured_destinations


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
            NewsletterItemAnalysis(
                title=title,
                source_title=title,
                category="OTHER",
                summary_zh_tw="摘要",
                why_it_matters_zh_tw="原因",
                importance=5,
                confidence=0.8,
                tags=[],
            )
        ],
    )


def stored_item_id(database: Database, gmail_id: str, title: str = "Title") -> tuple[int, int]:
    document_id = discover(database, gmail_id)
    assert document_id is not None
    database.store_extraction(document_id, extraction(title))
    item_id = int(str(database.items_for_documents([document_id], 10)[0]["id"]))
    return document_id, item_id


def test_reranker_scores_are_append_only_and_survive_item_replacement(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    document_id, item_id = stored_item_id(database, "gmail-1")

    assert database.save_reranker_scores([(item_id, 0.42)], "Qwen/test", "abc123") == 1
    database.store_extraction(document_id, extraction("Replaced"), replace=True)

    rows = database.connection.execute(
        "SELECT item_id,document_id,normalized_title,score,model,prompt_version FROM reranker_scores"
    ).fetchall()
    assert [tuple(row) for row in rows] == [(item_id, document_id, "title", 0.42, "Qwen/test", "abc123")]
    # SQLite reuses the deleted rowid, so item_id alone can point at a different item after a
    # reprocess. The denormalized title is what keeps the audit row attributable to what was scored.
    replaced = database.items_for_documents([document_id], 10)[0]
    assert replaced["id"] == item_id
    assert replaced["normalized_title"] == "replaced"
    database.close()


def test_reranker_scores_accumulate_across_runs(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    _, item_id = stored_item_id(database, "gmail-1")

    database.save_reranker_scores([(item_id, 0.1)], "Qwen/test", "v1")
    database.save_reranker_scores([(item_id, 0.9)], "Qwen/test", "v2")

    rows = database.connection.execute("SELECT score,prompt_version FROM reranker_scores ORDER BY id").fetchall()
    assert [tuple(row) for row in rows] == [(0.1, "v1"), (0.9, "v2")]
    database.close()


def test_reranker_scores_report_unwritten_rows_and_skip_non_finite(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    _, item_id = stored_item_id(database, "gmail-1")

    assert database.save_reranker_scores([(item_id, 0.5), (999_999, 0.6)], "Qwen/test", "v1") == 1
    assert database.save_reranker_scores([(item_id, float("nan")), (item_id, float("inf"))], "Qwen/test", "v1") == 0
    assert database.save_reranker_scores([], "Qwen/test", "v1") == 0

    scores = database.connection.execute("SELECT score FROM reranker_scores").fetchall()
    assert [row["score"] for row in scores] == [0.5]
    database.close()


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


def test_database_files_are_private_with_permissive_umask(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "test.sqlite3"
    original_umask = os.umask(0)
    try:
        database = Database(path)
    finally:
        os.umask(original_umask)
    database.close()

    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_existing_permissive_database_is_repaired(tmp_path: Path) -> None:
    path = tmp_path / "test.sqlite3"
    connection = sqlite3.connect(path)
    connection.close()
    os.chmod(path, 0o644)

    database = Database(path)
    database.close()

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_database_rejects_symlink_without_changing_target(tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite3"
    target.write_bytes(b"not a database")
    link = tmp_path / "test.sqlite3"
    link.symlink_to(target)
    original = target.read_bytes()

    with pytest.raises(ValueError, match="RUNTIME_PERMISSION_UNSAFE"):
        Database(link)

    assert target.read_bytes() == original


def test_digest_destinations_retry_independently(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    destinations = configured_destinations(
        "both", "https://discord.com/api/webhooks/123456789012345678/test-webhook-token", "token", "123"
    )
    digest_id = database.save_digest("daily:1", "start", "end", "UTC", "digest", destinations=destinations)
    assert digest_id is not None

    deliveries = database.digest_deliveries(digest_id, destinations)
    assert len(deliveries) == 2
    database.finish_digest_delivery(int(deliveries[0]["id"]), ["webhook-message"], destinations)
    database.fail_digest_delivery(int(deliveries[1]["id"]), "DISCORD_BOT_FORBIDDEN", destinations)

    pending = database.digest_deliveries(digest_id, destinations)
    assert [row["destination_key"] for row in pending] == [deliveries[1]["destination_key"]]
    database.finish_digest_delivery(int(deliveries[1]["id"]), ["bot-message"], destinations)
    assert database.pending_digest(digest_id) is None
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


def test_url_resolution_cache_stores_hash_and_expires(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    raw_url = "https://tracking.example/e3t/token?mc_eid=private"

    database.cache_url_resolution(
        raw_url,
        "resolved",
        resolved_url="https://publisher.example/article",
        canonical_url="https://publisher.example/canonical",
    )

    cached = database.cached_url_resolution(raw_url)
    columns = {row["name"] for row in database.connection.execute("PRAGMA table_info(url_resolution_cache)")}
    database.close()
    assert cached is not None
    assert cached["resolved_url"] == "https://publisher.example/article"
    assert "raw_url" not in columns


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

    assert upgraded.connection.execute("SELECT version FROM schema_version ORDER BY version DESC").fetchone()[0] == 9
    assert upgraded.connection.execute("SELECT gmail_message_id FROM gmail_document_state").fetchone()[0] == "gmail-1"
    assert upgraded.connection.execute("SELECT 1 FROM sqlite_master WHERE name='hackernews_document_state'").fetchone()[0] == 1
    upgraded.close()


def test_v3_hackernews_state_upgrades_without_losing_metadata(tmp_path: Path) -> None:
    path = tmp_path / "v3.sqlite3"
    database = Database(path)
    document_id = database.discover_document(
        SourceDocument(
            source_type="hackernews",
            source_id="hn-best",
            external_id="123",
            title="Article",
            published_at=datetime(2026, 7, 23, tzinfo=UTC),
        ),
        ResolvedContent(
            document=SourceDocument(
                source_type="hackernews",
                source_id="hn-best",
                external_id="123",
                title="Article",
                published_at=datetime(2026, 7, 23, tzinfo=UTC),
            ),
            text="",
            basis="metadata",
            truncated=False,
        ),
    )
    assert document_id is not None
    database.connection.execute("DROP TABLE hackernews_document_state")
    database.connection.executescript(
        """CREATE TABLE hackernews_document_state(
        document_id INTEGER PRIMARY KEY REFERENCES documents(id), hn_item_id INTEGER NOT NULL,
        feed TEXT NOT NULL, feed_rank INTEGER NOT NULL, score INTEGER NOT NULL, descendants INTEGER NOT NULL,
        requested_url TEXT, final_url TEXT,
        fetch_status TEXT NOT NULL CHECK(fetch_status IN ('not_requested')), fetched_at TEXT, updated_at TEXT NOT NULL
        );
        INSERT INTO hackernews_document_state VALUES(1,123,'beststories',1,10,2,'https://example.com',NULL,'not_requested',NULL,'now');
        DELETE FROM schema_version; INSERT INTO schema_version VALUES(3,'now');"""
    )
    database.close()

    upgraded = Database(path)

    row = upgraded.connection.execute("SELECT hn_item_id,requested_url,fetch_status FROM hackernews_document_state").fetchone()
    assert tuple(row) == (123, "https://example.com", "not_requested")
    assert upgraded.connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 9
    upgraded.close()


def test_v6_schema_adds_parent_checkpoint_destination(tmp_path: Path) -> None:
    path = tmp_path / "v6.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """CREATE TABLE schema_version(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        INSERT INTO schema_version VALUES(6,'now');
        CREATE TABLE digests(id INTEGER PRIMARY KEY, discord_message_ids_json TEXT);
        INSERT INTO digests VALUES(1,'[\"message\"]');"""
    )
    connection.close()

    upgraded = Database(path)

    row = upgraded.connection.execute(
        "SELECT discord_message_ids_json,discord_destination_key FROM digests WHERE id=1"
    ).fetchone()
    assert tuple(row) == ('["message"]', "webhook")
    assert "retired_at" in {column["name"] for column in upgraded.connection.execute("PRAGMA table_info(digest_deliveries)")}
    assert upgraded.connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 9
    upgraded.close()


def test_digest_checkpoint_is_reset_for_a_new_destination(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    digest_id = database.save_digest("daily:1", "start", "end", "UTC", "digest")
    assert digest_id is not None

    database.record_delivery_progress(digest_id, ["webhook-message"], "webhook:old")

    assert database.delivery_checkpoint(digest_id, "bot:123") is None
    row = database.pending_digest(digest_id)
    assert row is not None
    assert (row["discord_message_ids_json"], row["discord_destination_key"]) == (None, "bot:123")
    database.close()


def test_digest_checkpoint_with_legacy_webhook_marker_is_reset(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    digest_id = database.save_digest("daily:1", "start", "end", "UTC", "digest")
    assert digest_id is not None

    database.record_delivery_progress(digest_id, ["webhook-message"], "webhook")

    assert database.delivery_checkpoint(digest_id, "webhook:new") is None
    row = database.pending_digest(digest_id)
    assert row is not None
    assert (row["discord_message_ids_json"], row["discord_destination_key"]) == (None, "webhook:new")
    database.close()


def test_migrating_legacy_digest_does_not_adopt_unknown_webhook_checkpoint(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    digest_id = database.save_digest("daily:1", "start", "end", "UTC", "digest")
    assert digest_id is not None
    database.record_delivery_progress(digest_id, ["webhook-message"], "webhook")
    database.fail_delivery(digest_id)
    destinations = configured_destinations(
        "both", "https://discord.com/api/webhooks/123456789012345678/test-webhook-token", "token", "123"
    )

    database.migrate_legacy_digest_deliveries(digest_id, destinations)

    assert [row["discord_message_ids_json"] for row in database.digest_deliveries(digest_id, destinations)] == [None, None]
    database.close()


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
        "reranker_scores": 0,
        "digest_deliveries": 0,
        "digests": 0,
        "runs": 0,
        "url_resolution_cache": 0,
    }
    assert backup_path.stat().st_mode & 0o777 == 0o600
    backup = Database(backup_path)
    assert backup.counts()["documents"] == 1
    backup.close()
    database.close()
