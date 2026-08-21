from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from two_much_two_read.digest import (
    DigestEntry,
    canonical_url,
    dedupe,
    dedupe_entries,
    merge_related_entries,
    render_digest,
    share_a_candidate_token,
    story_tokens,
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


def judge(answer: bool) -> tuple[Callable[[DigestEntry, DigestEntry], bool], list[tuple[str, str]]]:
    """A stand-in for the model, recording which pairs the shortlist actually put in front of it."""
    asked: list[tuple[str, str]] = []

    def decide(left: DigestEntry, right: DigestEntry) -> bool:
        asked.append((left.item.title, right.item.title))
        return answer

    return decide, asked


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

    headlines, mentions = merge_related_entries([headline], [mention], judge(True)[0])

    assert mentions == []
    assert headlines[0].also_from == ("TLDR Dev",)
    # The headline's own newsletter carried no link; the merged one did.
    assert headlines[0].article_url == "https://cerebras.ai/blog/gpt-5-6-sol"
    assert len(headlines[0].merged_summaries) == 2


def test_a_section_marker_never_puts_a_whole_newsletter_in_front_of_the_model() -> None:
    """Section markers are shared by every item in a section, so they would shortlist all of them."""
    left = merged_entry("CORMA (PRODUCT LAUNCH)", "Corma 是網路安全防禦的 AI 基礎模型。", "TLDR InfoSec", review_score=90)
    right = merged_entry("MINDGARD (PRODUCT LAUNCH)", "Mindgard 提供 AI 紅隊測試平台。", "TLDR InfoSec")

    # "product" and "launch" are gone; only the topic word survives, and that is the model's to judge.
    assert story_tokens(left) & story_tokens(right) == {"ai"}

    decide, asked = judge(False)
    headlines, mentions = merge_related_entries([left], [right], decide)

    assert len(asked) == 1
    assert headlines[0].also_from == ()
    assert len(mentions) == 1


def test_the_same_vendor_covering_two_stories_is_not_merged() -> None:
    left = merged_entry("DeepSeek 釋出 V4-Pro 智慧代理", "DeepSeek V4-Pro 推出，支援離峰價格。", "AlphaSignal", review_score=90)
    right = merged_entry("DeepSeek Harness 開發者預覽", "DeepSeek Harness 提供模組化插件系統。", "TLDR Dev")

    decide, asked = judge(False)
    headlines, mentions = merge_related_entries([left], [right], decide)

    assert asked == [("DeepSeek Harness 開發者預覽", "DeepSeek 釋出 V4-Pro 智慧代理")]
    assert headlines[0].also_from == ()
    assert len(mentions) == 1


def test_mentions_are_deduped_against_each_other_too() -> None:
    first = merged_entry("Gemini 3.7 Flash：Google 高速模型", "Google 推出 Gemini 3.7 Flash。", "ThursdAI")
    second = merged_entry("Google 推出 Gemini 3.7 Flash 模型", "Gemini 3.7 Flash 價格減半。", "TLDR AI")

    headlines, mentions = merge_related_entries([], [first, second], judge(True)[0])

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

    decide, asked = judge(False)
    headlines, mentions = merge_related_entries([headline], [other], decide)

    # Two shared identity tokens are enough to ask about, and only the model can tell that one item
    # names the other to compare against it rather than reporting the same event.
    assert len(asked) == 1
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

    decide, asked = judge(False)
    headlines, mentions = merge_related_entries([left], [right], decide)

    # They share only the topic word "AI", which is exactly the kind of overlap six rounds of
    # filtering could not classify. It is put to the model instead of being decided here.
    assert story_tokens(left) & story_tokens(right) == {"ai"}
    assert len(asked) == 1
    assert headlines[0].also_from == ()
    assert len(mentions) == 1


def test_an_untranslated_title_still_merges_into_a_chinese_one() -> None:
    """The most valuable case: one newsletter translated the headline and another did not.

    21% of live items keep an English title under DIGEST_LANGUAGE=zh-TW, and mixed pairs were the
    largest group of real repeat coverage, so one Chinese title has to be enough.
    """
    chinese = merged_entry(
        "OpenAI 推出 Linux 版 ChatGPT 桌面應用", "OpenAI 讓 ChatGPT 桌面應用登上 Linux。", "TLDR AI", review_score=90
    )
    english = merged_entry("OpenAI's ChatGPT desktop app is now on Linux", "ChatGPT 桌面應用開始支援 Linux。", "AlphaSignal")

    headlines, mentions = merge_related_entries([chinese], [english], judge(True)[0])

    assert headlines[0].also_from == ("AlphaSignal",)
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

    headlines, _ = merge_related_entries([headline], [hacker_news], judge(True)[0])

    assert headlines[0].discussion_url == "https://news.ycombinator.com/item?id=456"
    assert (headlines[0].hn_item_id, headlines[0].hn_score, headlines[0].hn_comments) == ("456", 210, 64)


def test_two_newsletters_on_one_article_keep_both_attributions() -> None:
    """The least ambiguous repeat coverage there is, and the case that used to lose the most.

    Exact-URL dedupe ran before merging and simply dropped the loser, so the surviving entry
    recorded neither the other newsletter nor its wording.
    """
    url = "https://article.example/gpt-5-6"
    stronger = replace(merged_entry("GPT-5.6 Sol 發表", "OpenAI 推出 GPT-5.6 Sol。", "AlphaSignal", url), review_score=90)
    weaker = merged_entry("OpenAI 發表新模型", "GPT-5.6 Sol 以 Cerebras 硬體達到 750 tokens/second。", "TLDR AI", url)

    kept = dedupe_entries([stronger, weaker])

    assert len(kept) == 1
    assert kept[0].source_name == "AlphaSignal"
    assert kept[0].also_from == ("TLDR AI",)
    assert kept[0].merged_summaries == (
        "OpenAI 推出 GPT-5.6 Sol。",
        "GPT-5.6 Sol 以 Cerebras 硬體達到 750 tokens/second。",
    )


def test_exact_url_attribution_does_not_depend_on_the_digest_language() -> None:
    """Related-story merging is gated to Chinese; an identical URL needs no token heuristic at all."""
    url = "https://article.example/story"
    entries = [
        merged_entry("OpenAI Launches New AI Model", "OpenAI launches a new model.", "TLDR AI", url),
        merged_entry("New AI Model From OpenAI", "The model is available today.", "AlphaSignal", url),
    ]

    assert dedupe_entries(entries)[0].also_from == ("AlphaSignal",)


def test_a_tracking_parameter_does_not_hide_repeat_coverage() -> None:
    first = merged_entry("GPT-5.6 Sol 發表", "OpenAI 推出 GPT-5.6 Sol。", "AlphaSignal", "https://article.example/a")
    second = merged_entry("OpenAI 新模型", "GPT-5.6 Sol 開放使用。", "TLDR AI", "https://article.example/a?utm_source=tldr")

    assert dedupe_entries([first, second])[0].also_from == ("TLDR AI",)


def test_a_newsletter_and_hacker_news_on_one_article_show_as_two_sources() -> None:
    """Two independent sources carrying one article is the signal the source line exists to show."""
    article_url = "https://article.example/story"
    text = render_digest(
        [
            DigestEntry(item("Newsletter title", article_url, importance=9), source_name="TLDR AI"),
            DigestEntry(
                item("HN title", f"{article_url}?utm_source=hn"),
                source_name="Hacker News",
                hn_score=10,
                hn_comments=2,
                hn_item_id="456",
                discussion_url="https://news.ycombinator.com/item?id=456",
            ),
        ],
        datetime(2026, 7, 24, tzinfo=UTC),
        "AI",
        "Newsletter, HN",
    )

    assert "來源：TLDR AI, Hacker News" in text
    assert "討論：<https://news.ycombinator.com/item?id=456>" in text
    assert "HN title" not in text


def test_the_shortlist_asks_only_about_pairs_sharing_an_identity_token() -> None:
    """It decides who gets asked, not who merges — precision moved to the model.

    Loose on purpose: one shared token is enough. Over real runs that put 0 to 15 pairs in front of
    the model per digest; dropping the shape requirement as well raised the worst case to 108.
    """
    left = merged_entry("GPT-5.6 Sol 發表", "OpenAI 推出 GPT-5.6 Sol。", "TLDR AI")
    same = merged_entry("OpenAI 開放 GPT-5.6 Sol", "GPT-5.6 Sol 開放使用。", "AlphaSignal")
    unrelated = merged_entry("Rust 1.94 釋出", "Rust 團隊釋出 1.94 版。", "TLDR Dev")

    assert share_a_candidate_token(left, same)
    assert not share_a_candidate_token(left, unrelated)

    # The pair that took six review rounds to classify. Title Case vocabulary still passes the
    # shape filter, so they are shortlisted - and that is now correct, because being shortlisted
    # means being asked rather than being merged. Where the old code had to decide this from
    # "Launches", "New", and "Model", the model is asked whether they report the same event.
    coding = merged_entry("OpenAI Launches New AI Model for Coding", "OpenAI released a coding model.", "TLDR AI")
    search = merged_entry("OpenAI Launches New AI Model for Search", "OpenAI released a search model.", "TLDR AI")

    assert share_a_candidate_token(coding, search)
    assert story_tokens(coding) & story_tokens(search) == {"openai", "launches", "new", "ai", "model"}

    decide, asked = judge(False)
    headlines, mentions = merge_related_entries([replace(coding, review_score=90)], [search], decide)

    assert len(asked) == 1
    assert headlines[0].also_from == ()
    assert len(mentions) == 1
