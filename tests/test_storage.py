from pathlib import Path

from newsletter_digest.schemas import DigestItem, EmailExtraction
from newsletter_digest.storage import Database


def test_message_and_digest_idempotency(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    message_id = database.discover("gmail-1", "thread-1", "source", "now", "subject", "sender", "body")
    assert message_id is not None
    assert database.discover("gmail-1", "thread-1", "source", "now", "subject", "sender", "body") is None
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
    assert len(database.recent_items()) == 1
    assert database.save_digest("daily:1", "start", "end", "UTC", "digest")
    assert not database.save_digest("daily:1", "start", "end", "UTC", "digest")
    resend = database.prepare_latest_resend()
    assert resend is not None
    assert resend["rendered_content"] == "digest"
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
    assert database.recent_items()[0]["title"] == "Old title"
    extraction.items[0].title = "New title"
    database.store_extraction(forced_id, extraction, replace=True)
    assert database.recent_items()[0]["title"] == "New title"
    database.close()
