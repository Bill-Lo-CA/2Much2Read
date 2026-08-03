from datetime import UTC, datetime

import pytest

from two_much_two_read.digest import DigestEntry, canonical_url, dedupe, render_digest
from two_much_two_read.schemas import DigestItem
from two_read_runtime.discord import chunk_text, sanitize_discord_text


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
    assert canonical_url("HTTPS://Example.COM/a?utm_source=x&mc_eid=y&id=1#top") == "https://example.com/a?id=1"
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
    assert "＠everyone" in text
    chunks = chunk_text(text * 100)
    assert all(len(chunk) <= 2000 for chunk in chunks)


def test_renderer_sanitizes_untrusted_fields_and_preserves_application_urls() -> None:
    hostile = "```\n# heading\n||spoiler|| [click](https://evil.example) @everyone"
    hostile_item = DigestItem.model_construct(
        title=hostile,
        category="AI_MODEL",
        summary_zh_tw=hostile,
        why_it_matters_zh_tw=hostile,
        importance=8,
        confidence=0.8,
        tags=[hostile],
        source_url="https://example.com/article",
    )

    text = render_digest(
        [DigestEntry(hostile_item, source_name=hostile)],
        datetime(2026, 6, 22, tzinfo=UTC),
        hostile,
        hostile,
    )

    assert sanitize_discord_text(hostile) in text
    assert "```" not in text
    assert "\n# heading" not in text
    assert "||spoiler||" not in text
    assert "@everyone" not in text
    assert "[click](https://evil.example)" not in text
    assert "文章：<https://example.com/article>" in text


def test_renderer_preserves_multilingual_readability_and_code_punctuation() -> None:
    digest_item = item(
        "C++ v2.0 API() — équipe 日本語",
        None,
    ).model_copy(
        update={
            "summary_zh_tw": "Résumé：這是日本語與繁體中文的摘要，呼叫 x()。",
            "why_it_matters_zh_tw": "L’équipe 可用 C++ v2.0 改善流程。",
        }
    )

    text = render_digest([digest_item], datetime(2026, 6, 22, tzinfo=UTC), "AI", "Source")

    assert sanitize_discord_text("C++ v2.0 API() — équipe 日本語") in text
    assert sanitize_discord_text("Résumé：這是日本語與繁體中文的摘要，呼叫 x()。") in text
    assert sanitize_discord_text("L’équipe 可用 C++ v2.0 改善流程。") in text


def test_renderer_shows_item_source_even_when_extraction_confidence_is_low() -> None:
    text = render_digest(
        [DigestEntry(item("Update", "https://example.com/a", confidence=0.1), source_name="AlphaSignal")],
        datetime.now(UTC),
        "AI",
        "AlphaSignal",
    )

    assert "來源：AlphaSignal" in text


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
