from pathlib import Path

from two_much_two_read.schemas import DigestItem, EmailExtraction
from two_much_two_read.storage import Database


def test_message_and_digest_idempotency(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    message_id = database.discover("gmail-1", "thread-1", "source", "now", "subject", "sender", "body")
    assert message_id is not None
    assert database.discover("gmail-1", "thread-1", "source", "now", "subject", "sender", "body") == message_id
    database.store_extraction(
        message_id,
        EmailExtraction(
            source_id="source",
            newsletter_title="News",
            newsletter_date=None,
            overview_zh_tw="摘要",
            items=[
                DigestItem(
                    title="Title",
                    category="OTHER",
                    summary_zh_tw="摘要",
                    why_it_matters_zh_tw="原因",
                    source_url=None,
                    importance=5,
                    confidence=0.8,
                    tags=[],
                )
            ],
        ),
    )
    assert len(database.items_for_messages([message_id], 10)) == 1
    assert database.discover("gmail-1", "thread-1", "source", "now", "subject", "sender", "body") is None
    failed_id = database.discover("gmail-2", "thread-2", "source", "now", "subject", "sender", "body")
    assert failed_id is not None
    database.fail_message(failed_id, "OLLAMA_SCHEMA_INVALID")
    assert database.discover("gmail-2", "thread-2", "source", "now", "subject", "sender", "body") is None
    digest_id = database.save_digest("daily:1", "start", "end", "UTC", "digest")
    assert digest_id is not None
    assert database.pending_digest(digest_id)["rendered_content"] == "digest"
    assert database.save_digest("daily:1", "start", "end", "UTC", "digest") is None
    database.close()


def test_force_replaces_existing_extraction(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    message_id = database.discover("gmail-1", "thread-1", "source", "now", "subject", "sender", "body")
    assert message_id is not None
    extraction = EmailExtraction(
        source_id="source",
        newsletter_title="News",
        newsletter_date=None,
        overview_zh_tw="摘要",
        items=[
            DigestItem(
                title="Old title",
                category="OTHER",
                summary_zh_tw="摘要",
                why_it_matters_zh_tw="原因",
                importance=5,
                confidence=0.8,
            )
        ],
    )
    database.store_extraction(message_id, extraction)

    forced_id = database.discover("gmail-1", "thread-1", "source", "now", "subject", "sender", "new body", force=True)
    assert forced_id == message_id
    assert database.items_for_messages([message_id], 10)[0]["title"] == "Old title"
    extraction.items[0].title = "New title"
    database.store_extraction(forced_id, extraction, replace=True)
    assert database.items_for_messages([message_id], 10)[0]["title"] == "New title"
    database.close()


def test_items_for_messages_excludes_prior_runs(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    first_id = database.discover("gmail-1", "thread-1", "source", "2026-07-01", "subject", "sender", "body")
    second_id = database.discover("gmail-2", "thread-2", "source", "2026-07-02", "subject", "sender", "body")
    assert first_id is not None and second_id is not None
    extraction = EmailExtraction(
        source_id="source",
        newsletter_title="News",
        newsletter_date=None,
        overview_zh_tw="摘要",
        items=[
            DigestItem(
                title="Item",
                category="OTHER",
                summary_zh_tw="摘要",
                why_it_matters_zh_tw="原因",
                importance=5,
                confidence=0.8,
            )
        ],
    )
    database.store_extraction(first_id, extraction)
    database.store_extraction(second_id, extraction)

    assert [row["message_id"] for row in database.items_for_messages([second_id], 10)] == [second_id]
    database.close()


def test_save_digest_finalizes_staged_extractions_atomically(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    message_id = database.discover("gmail-1", "thread-1", "source", "now", "subject", "sender", "body")
    assert message_id is not None
    database.store_extraction(
        message_id,
        EmailExtraction(source_id="source", newsletter_title="News", newsletter_date=None, overview_zh_tw="摘要", items=[]),
        finalize=False,
    )

    assert database.message("gmail-1")["state"] == "discovered"
    assert database.save_digest("daily:1", "start", "end", "UTC", "digest", [message_id]) is not None
    assert database.message("gmail-1")["state"] == "processed"
    database.close()


def test_backup_and_reset(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    assert database.discover("gmail-1", "thread-1", "source", "now", "subject", "sender", "body") is not None
    backup_path = tmp_path / "backup.sqlite3"

    database.backup(backup_path)
    counts = database.reset()

    assert counts["messages"] == 1
    assert database.counts() == {"messages": 0, "items": 0, "digests": 0, "runs": 0}
    assert backup_path.stat().st_mode & 0o777 == 0o600
    backup = Database(backup_path)
    assert backup.counts()["messages"] == 1
    backup.close()
    database.close()
