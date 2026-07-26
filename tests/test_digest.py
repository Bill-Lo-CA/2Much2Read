from datetime import UTC, datetime

import pytest

from two_much_two_read.digest import DigestEntry, canonical_url, dedupe, render_digest
from two_much_two_read.schemas import DigestItem
from two_read_runtime.discord import chunk_text


def item(title: str, url: str | None, confidence: float = 0.8, importance: int = 8) -> DigestItem:
    return DigestItem(
        title=title,
        category="AI_MODEL",
        summary_zh_tw="摘要",
        why_it_matters_zh_tw="重要原因",
        source_url=url,
        importance=importance,
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


@pytest.mark.parametrize(
    ("language", "labels"),
    [
        ("en", ("🔥 Top stories", "Summary：", "Why it matters：", "📊 Processed")),
        ("fr", ("🔥 À la une", "Résumé：", "Pourquoi c’est important：", "📊 Traitement")),
        ("ja", ("🔥 今日の注目", "要約：", "重要な理由：", "📊 処理結果")),
        ("zh-CN", ("🔥 今日重点", "摘要：", "为什么重要：", "📊 本次处理")),
    ],
)
def test_renderer_localizes_labels(language: str, labels: tuple[str, str, str, str]) -> None:
    text = render_digest([item("Update", None)], datetime(2026, 6, 22, tzinfo=UTC), "AI", "Source", language=language)

    assert all(label in text for label in labels)


def test_renderer_uses_neutral_labels_for_unmapped_language() -> None:
    text = render_digest([item("Update", None)], datetime(2026, 6, 22, tzinfo=UTC), "AI", "Source", language="de")

    assert "摘要：" not in text
    assert "   •：摘要" in text
    assert "📊\nAI\nSource · 1 " in text


def test_hackernews_renderer_shows_article_and_discussion_without_self_post_duplicate() -> None:
    discussion = "https://news.ycombinator.com/item?id=123"
    self_post_discussion = "https://news.ycombinator.com/item?id=124"
    external = DigestEntry(
        item("External", "https://article.example/final"),
        datetime(2026, 7, 24, tzinfo=UTC),
        article_url="https://article.example/final",
        discussion_url=discussion,
        hn_score=342,
        hn_comments=98,
        hn_item_id="123",
        content_basis="article",
    )
    self_post = DigestEntry(
        item("Self post", self_post_discussion),
        datetime(2026, 7, 23, tzinfo=UTC),
        article_url=self_post_discussion,
        discussion_url=self_post_discussion,
        hn_score=3,
        hn_comments=1,
        hn_item_id="124",
        content_basis="hn_self_post",
    )

    text = render_digest([external, self_post], datetime(2026, 7, 24, tzinfo=UTC), "AI", "HN")

    assert "HN：342 points · 98 comments" in text
    assert "文章：<https://article.example/final>" in text
    assert text.count(f"討論：<{discussion}>") == 1
    assert "Self post" in text
    assert f"文章：<{self_post_discussion}>" not in text


def test_cross_source_dedupe_preserves_hackernews_attribution() -> None:
    article_url = "https://article.example/story"
    result = render_digest(
        [
            DigestEntry(item("Newsletter title", article_url, importance=9)),
            DigestEntry(
                item("HN title", f"{article_url}?utm_source=hn"),
                hn_score=10,
                hn_comments=2,
                hn_item_id="456",
                discussion_url="https://news.ycombinator.com/item?id=456",
                content_basis="metadata",
            ),
        ],
        datetime(2026, 7, 24, tzinfo=UTC),
        "AI",
        "Newsletter, HN",
    )

    assert "Newsletter title" in result
    assert "HN title" not in result
    assert "討論：<https://news.ycombinator.com/item?id=456>" in result
    assert "內容：僅 metadata" not in result


def test_hackernews_metadata_fallback_is_visible() -> None:
    text = render_digest(
        [
            DigestEntry(
                item("Unavailable article", "https://article.example/requested"),
                article_url="https://article.example/requested",
                discussion_url="https://news.ycombinator.com/item?id=789",
                hn_score=1,
                hn_comments=0,
                hn_item_id="789",
                content_basis="metadata",
            )
        ],
        datetime(2026, 7, 24, tzinfo=UTC),
        "AI",
        "HN",
    )

    assert "內容：僅 metadata" in text
