from datetime import UTC, datetime

import pytest

from two_much_two_read.digest import canonical_url, dedupe, render_digest
from two_much_two_read.schemas import DigestItem
from two_read_runtime.discord import chunk_text


def item(title: str, url: str | None, confidence: float = 0.8) -> DigestItem:
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


def test_items_without_urls_dedupe_by_title() -> None:
    items = dedupe([item("First story", None), item("Second story", None)])

    assert [entry.title for entry in items] == ["First story", "Second story"]


def test_renderer_and_chunks_disable_mentions() -> None:
    text = render_digest(
        [item("@everyone update", "https://example.com/a")],
        datetime.now(UTC),
        "AI",
        "AlphaSignal",
    )
    assert "@\u200beveryone" in text
    chunks = chunk_text(text * 100)
    assert all(len(chunk) <= 2000 for chunk in chunks)


@pytest.mark.parametrize("topic", ["Cloud & Data", "Cybersecurity"])
def test_renderer_uses_actual_topic_and_sources(topic: str) -> None:
    text = render_digest([item("Update", None)], datetime(2026, 6, 22, tzinfo=UTC), topic, "Source One, Source Two")

    assert text.startswith(f"📰 {topic} 2much2read — 2026-06-22")
    assert f"主題：{topic}" in text
    assert "來源：Source One, Source Two · 1 則有效項目" in text
