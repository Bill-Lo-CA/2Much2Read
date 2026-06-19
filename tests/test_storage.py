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
    database.close()
