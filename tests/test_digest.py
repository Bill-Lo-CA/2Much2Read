from dataclasses import replace
from datetime import UTC, datetime

import pytest

from two_much_two_read.digest import (
    DigestEntry,
    canonical_url,
    dedupe,
    merge_related_entries,
    render_digest,
)
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


def test_secondary_mentions_are_one_scannable_line_each() -> None:
    text = render_digest(
        [
            DigestEntry(item("Headline", "https://example.com/a"), review_score=90, source_name="TLDR AI"),
            DigestEntry(item("Also worth noting", "https://example.com/b"), reranker_score=0.3, source_name="TLDR Sec"),
        ],
        datetime(2026, 6, 22, tzinfo=UTC),
        "AI",
        "TLDR",
        top_items=1,
    )

    assert "🔥 今日重點\n1. Headline" in text
    assert "🧰 其他值得注意\n• Also worth noting · TLDR Sec · <https://example.com/b>" in text
    # The summary and the reason belong to the headline items; the mentions stay one line.
    assert text.count("摘要：") == 1
    assert text.count("為什麼重要：") == 1


def test_secondary_mentions_follow_the_reranker_order() -> None:
    text = render_digest(
        [
            DigestEntry(item("Weaker", None, importance=10), reranker_score=0.1),
            DigestEntry(item("Stronger", None, importance=1), reranker_score=0.9),
            DigestEntry(item("Headline", None, importance=1), review_score=50),
        ],
        datetime(2026, 6, 22, tzinfo=UTC),
        "AI",
        "TLDR",
        top_items=1,
    )

    assert text.index("Headline") < text.index("Stronger") < text.index("Weaker")


def test_a_mention_without_a_source_or_url_still_renders() -> None:
    text = render_digest(
        [
            DigestEntry(item("Headline", None), review_score=90),
            DigestEntry(item("Bare mention", None)),
        ],
        datetime(2026, 6, 22, tzinfo=UTC),
        "AI",
        "TLDR",
        top_items=1,
    )

    assert "• Bare mention" in text


def test_spare_headline_slots_are_not_filled_with_items_the_reviewer_rejected() -> None:
    """The reviewer selecting fewer than top_items must not promote what it passed over."""
    text = render_digest(
        [
            DigestEntry(item("Selected", None), review_score=90, source_name="TLDR AI"),
            DigestEntry(item("Passed over", None), reranker_score=0.4, source_name="TLDR AI"),
            DigestEntry(item("Also passed over", None), reranker_score=0.3, source_name="TLDR AI"),
        ],
        datetime(2026, 6, 22, tzinfo=UTC),
        "AI",
        "TLDR",
        top_items=5,
    )

    assert "1. Selected" in text
    assert "2. Passed over" not in text
    assert "• Passed over" in text and "• Also passed over" in text


def test_an_unreviewed_item_list_still_fills_the_headline_section() -> None:
    """Rendering plain items has no review scores, and every one of them is a headline."""
    text = render_digest([item("First", None), item("Second", None)], datetime(2026, 6, 22, tzinfo=UTC), "AI", "TLDR")

    assert "1. First" in text and "2. Second" in text
    assert "🧰" not in text


def merged_entry(title: str, summary: str, source: str, url: str | None = None, review_score: int | None = None) -> DigestEntry:
    return (
        DigestEntry(
            item(title, url),
            source_name=source,
            article_url=url,
            review_score=review_score,
            reranker_score=0.5,
        )
        if not summary
        else DigestEntry(
            DigestItem(
                title=title,
                category="AI_MODEL",
                summary_zh_tw=summary,
                why_it_matters_zh_tw="重要原因",
                source_url=url,
                importance=8,
                confidence=0.8,
            ),
            source_name=source,
            article_url=url,
            review_score=review_score,
            reranker_score=0.5,
        )
    )


def test_repeat_coverage_of_a_headline_is_folded_into_it() -> None:
    """The reviewer drops duplicates from its own picks, so they land in the mention list."""
    headline = merged_entry(
        "OpenAI 推出 GPT-5.6 Sol 超快版本", "OpenAI 以 Cerebras 硬體推出 GPT-5.6 Sol。", "AlphaSignal", review_score=90
    )
    mention = merged_entry(
        "使用 Cerebras 加速 GPT-5.6 Sol 超快版本",
        "Cerebras 硬體讓 GPT-5.6 Sol 達到 750 tokens/second。",
        "TLDR Dev",
        "https://cerebras.ai/blog/gpt-5-6-sol",
    )

    headlines, mentions = merge_related_entries([headline], [mention], 0.25)

    assert mentions == []
    assert headlines[0].also_from == ("TLDR Dev",)
    # The headline's own newsletter carried no link; the merged one did.
    assert headlines[0].article_url == "https://cerebras.ai/blog/gpt-5-6-sol"
    assert len(headlines[0].merged_summaries) == 2


def test_two_launches_from_the_same_section_are_not_merged() -> None:
    """Section markers dominate token overlap, which is why they are stripped before comparing."""
    left = merged_entry("CORMA (PRODUCT LAUNCH)", "Corma 是網路安全防禦的 AI 基礎模型。", "TLDR InfoSec", review_score=90)
    right = merged_entry("MINDGARD (PRODUCT LAUNCH)", "Mindgard 提供 AI 紅隊測試平台。", "TLDR InfoSec")

    headlines, mentions = merge_related_entries([left], [right], 0.25)

    assert headlines[0].also_from == ()
    assert len(mentions) == 1


def test_the_same_vendor_covering_two_stories_is_not_merged() -> None:
    left = merged_entry("DeepSeek 釋出 V4-Pro 智慧代理", "DeepSeek V4-Pro 推出，支援離峰價格。", "AlphaSignal", review_score=90)
    right = merged_entry("DeepSeek Harness 開發者預覽", "DeepSeek Harness 提供模組化插件系統。", "TLDR Dev")

    headlines, mentions = merge_related_entries([left], [right], 0.25)

    assert headlines[0].also_from == ()
    assert len(mentions) == 1


def test_mentions_are_deduped_against_each_other_too() -> None:
    first = merged_entry("Gemini 3.7 Flash：Google 高速模型", "Google 推出 Gemini 3.7 Flash。", "ThursdAI")
    second = merged_entry("Google 推出 Gemini 3.7 Flash 模型", "Gemini 3.7 Flash 價格減半。", "TLDR AI")

    headlines, mentions = merge_related_entries([], [first, second], 0.25)

    assert headlines == []
    assert len(mentions) == 1
    assert mentions[0].also_from == ("TLDR AI",)


def test_merged_sources_are_rendered_in_both_sections() -> None:
    headline = replace(
        merged_entry("Headline", "摘要內容", "AlphaSignal", review_score=90),
        also_from=("TLDR Dev", "TLDR AI"),
    )
    mention = replace(merged_entry("Mention", "另一則摘要", "ThursdAI"), also_from=("TLDR AI",))

    text = render_digest([headline, mention], datetime(2026, 6, 22, tzinfo=UTC), "AI", "TLDR", top_items=1)

    assert "來源：AlphaSignal, TLDR Dev, TLDR AI" in text
    assert "• Mention · ThursdAI, TLDR AI" in text


def test_a_story_merely_named_by_another_is_not_merged_into_it() -> None:
    """A run merged these, then rewrote the GPT-5.6 headline from the Grok article it borrowed."""
    headline = merged_entry(
        "OpenAI預覽GPT-5.6 Sol Ultrafast",
        "OpenAI預覽GPT-5.6 Sol模式，可每秒生成750個輸出tokens，運行速度達標準的14倍。",
        "TLDR AI",
        review_score=90,
    )
    other = merged_entry(
        "Grok 4.6 推出：專注長時間任務處理",
        "Grok 4.6 針對長時間任務處理進行優化，匹配 GPT-5.6 Sol 的表現。",
        "TLDR AI",
        "https://x.ai/news/grok-4-6",
    )

    headlines, mentions = merge_related_entries([headline], [other], 0.25)

    assert headlines[0].also_from == ()
    assert headlines[0].article_url is None
    assert len(mentions) == 1


def test_ordinary_vocabulary_never_identifies_a_story() -> None:
    """With DIGEST_LANGUAGE=en the summaries are English, so every item shares common words."""
    left = merged_entry(
        "OpenAI launches new AI model for coding",
        "OpenAI released a new model aimed at software development tasks.",
        "TLDR AI",
        review_score=90,
    )
    right = merged_entry(
        "Google launches new AI model for search",
        "Google released a new model aimed at search ranking tasks.",
        "TLDR AI",
    )

    headlines, mentions = merge_related_entries([left], [right], 0.25)

    assert headlines[0].also_from == ()
    assert len(mentions) == 1


def test_an_english_digest_still_merges_on_product_names() -> None:
    left = merged_entry(
        "OpenAI previews GPT-5.6 Sol Ultrafast",
        "OpenAI previewed a mode generating 750 tokens per second.",
        "TLDR AI",
        review_score=90,
    )
    right = merged_entry(
        "Cerebras accelerates GPT-5.6 Sol Ultrafast",
        "Cerebras hardware drives GPT-5.6 Sol to 750 tokens per second.",
        "TLDR Dev",
        "https://cerebras.ai/blog/sol",
    )

    headlines, mentions = merge_related_entries([left], [right], 0.25)

    assert headlines[0].also_from == ("TLDR Dev",)
    assert mentions == []


def test_absorbing_a_hacker_news_entry_keeps_its_discussion() -> None:
    """The absorbed entry stops being rendered, so its HN attribution would vanish with it."""
    headline = merged_entry(
        "Docker CopyEscape 漏洞", "Docker cp 的競爭條件漏洞 CVE-2026-17106。", "TLDR InfoSec", review_score=90
    )
    hacker_news = replace(
        merged_entry("Docker CopyEscape vulnerability", "Docker cp race condition CVE-2026-17106 overwrites host files.", "HN"),
        discussion_url="https://news.ycombinator.com/item?id=456",
        hn_item_id="456",
        hn_score=210,
        hn_comments=64,
    )

    headlines, _ = merge_related_entries([headline], [hacker_news], 0.25)

    assert headlines[0].discussion_url == "https://news.ycombinator.com/item?id=456"
    assert (headlines[0].hn_item_id, headlines[0].hn_score, headlines[0].hn_comments) == ("456", 210, 64)
