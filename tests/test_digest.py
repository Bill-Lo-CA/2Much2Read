from datetime import UTC, datetime

from newsletter_digest.digest import canonical_url, chunk_text, dedupe, render_digest
from newsletter_digest.schemas import DigestItem


def item(title: str, url: str, confidence: float = 0.8) -> DigestItem:
    return DigestItem(
        title=title,
        category="AI_MODEL",
        summary_zh_tw="摘要",
        why_it_matters_zh_tw="重要原因",
        source_url=url,
        importance=8,
        confidence=confidence,
        tags=["AI Model"],
    )


def test_canonical_url_and_dedupe() -> None:
    assert canonical_url("HTTPS://Example.COM/a?utm_source=x&id=1#top") == "https://example.com/a?id=1"
    assert dedupe([item("A", "https://example.com/a", 0.5), item("B", "https://example.com/a", 0.9)])[0].title == "B"


def test_renderer_and_chunks_disable_mentions() -> None:
    text = render_digest([item("@everyone update", "https://example.com/a")], datetime.now(UTC))
    assert "@\u200beveryone" in text
    chunks = chunk_text(text * 100)
    assert all(len(chunk) <= 2000 for chunk in chunks)
