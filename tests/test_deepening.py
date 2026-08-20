from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from two_much_two_read import pipeline
from two_much_two_read.article_fetcher import ArticleFetchError, FetchedArticle
from two_much_two_read.config import Settings
from two_much_two_read.digest import DigestEntry
from two_much_two_read.ollama import (
    DEEPEN_RESERVED_OUTPUT_TOKENS,
    OllamaSchemaError,
    create_ollama_client,
    fitted_deepening_content,
)
from two_much_two_read.schemas import DigestItem, ItemDeepening

ARTICLE = (
    "<html><body><article>"
    + "GPT-5.6 Sol Ultrafast runs at 750 tokens per second on Cerebras hardware. " * 40
    + "</article></body></html>"
)


def entry(title: str, url: str | None = None, review_score: int | None = 90) -> DigestEntry:
    return DigestEntry(
        DigestItem(
            title=title,
            category="AI_MODEL",
            summary_zh_tw="很短的摘要。",
            why_it_matters_zh_tw="很短的原因。",
            source_url=url,
            importance=8,
            confidence=0.8,
        ),
        source_name="AlphaSignal",
        article_url=url,
        review_score=review_score,
    )


class FakeOllama:
    def __init__(self, error: Exception | None = None, covers: bool = True) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.error = error
        self.covers = covers

    def deepen_item(self, title: str, category: str, sources: str, basis: str, content: str) -> ItemDeepening:
        self.calls.append((sources, basis, content))
        if self.error is not None:
            raise self.error
        return ItemDeepening(
            covers_the_item=self.covers, summary_zh_tw="重寫後長很多的摘要內容。", why_it_matters_zh_tw="重寫後的實務影響。"
        )


def fake_fetcher(monkeypatch: pytest.MonkeyPatch, body: bytes | None) -> None:
    class FakeFetcher:
        def fetch(self, url: str) -> FetchedArticle:
            if body is None:
                raise ArticleFetchError("ARTICLE_FETCH_FAILED")
            return FetchedArticle(url, url, "text/html", body)

    monkeypatch.setattr(pipeline, "ArticleFetcher", FakeFetcher)


def test_a_headline_is_rewritten_from_the_article_body(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_fetcher(monkeypatch, ARTICLE.encode())
    ollama = FakeOllama()

    deepened = pipeline._deepened_entries(Settings(), ollama, [entry("Headline", "https://example.com/a")], lambda _: None)

    assert deepened[0].item.summary_zh_tw == "重寫後長很多的摘要內容。"
    assert deepened[0].item.why_it_matters_zh_tw == "重寫後的實務影響。"
    sources, basis, content = ollama.calls[0]
    assert basis == "article"
    assert "Cerebras hardware" in content


def test_a_headline_without_a_usable_link_falls_back_to_the_merged_newsletters(monkeypatch: pytest.MonkeyPatch) -> None:
    """Email bodies are never persisted, so the merged coverage is the only text left."""
    fake_fetcher(monkeypatch, None)
    ollama = FakeOllama()
    merged = replace(
        entry("Headline", "https://example.com/a"),
        also_from=("TLDR Dev",),
        merged_summaries=("第一家的摘要。", "第二家的摘要。"),
    )

    pipeline._deepened_entries(Settings(), ollama, [merged], lambda _: None)

    sources, basis, content = ollama.calls[0]
    assert basis == "newsletters"
    assert sources == "AlphaSignal, TLDR Dev"
    assert "第一家的摘要。" in content and "第二家的摘要。" in content


def test_a_failed_rewrite_keeps_the_original_summary_and_reports_it(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_fetcher(monkeypatch, ARTICLE.encode())
    messages: list[str] = []

    deepened = pipeline._deepened_entries(
        Settings(), FakeOllama(OllamaSchemaError("bad")), [entry("Headline", "https://example.com/a")], messages.append
    )

    assert deepened[0].item.summary_zh_tw == "很短的摘要。"
    assert any("kept the original summary" in message for message in messages)


def test_a_transport_failure_also_keeps_the_original_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_fetcher(monkeypatch, ARTICLE.encode())

    deepened = pipeline._deepened_entries(
        Settings(),
        FakeOllama(httpx.ConnectError("ollama down")),
        [entry("Headline", "https://example.com/a")],
        lambda _: None,
    )

    assert deepened[0].item.summary_zh_tw == "很短的摘要。"


def test_secondary_mentions_are_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the headline items are worth an article fetch and a second model pass."""
    fake_fetcher(monkeypatch, ARTICLE.encode())
    ollama = FakeOllama()
    entries = [entry("Headline", "https://example.com/a"), entry("Mention", "https://example.com/b", review_score=None)]

    deepened = pipeline._deepened_entries(Settings(), ollama, entries, lambda _: None)

    assert len(ollama.calls) == 1
    assert deepened[1].item.summary_zh_tw == "很短的摘要。"


def test_the_rewrite_can_be_turned_off(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_fetcher(monkeypatch, ARTICLE.encode())
    ollama = FakeOllama()

    pipeline._deepened_entries(
        Settings(digest_deepen_headlines=False), ollama, [entry("Headline", "https://example.com/a")], lambda _: None
    )

    assert ollama.calls == []


def test_source_text_is_bounded_against_num_ctx() -> None:
    """Ollama truncates an oversized prompt from the head, dropping the system prompt silently."""
    content = "word " * 40_000

    bounded, truncated = fitted_deepening_content(content, 500, 16384)

    assert truncated and len(bounded) < len(content)
    assert fitted_deepening_content(bounded, 500, 16384) == (bounded, False)


def test_source_text_that_already_fits_is_untouched() -> None:
    assert fitted_deepening_content("short article text", 500, 16384) == ("short article text", False)


def test_no_room_at_all_yields_no_content() -> None:
    assert fitted_deepening_content("anything", 0, DEEPEN_RESERVED_OUTPUT_TOKENS) == ("", True)


def test_a_source_about_a_different_story_is_discarded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run rewrote a GPT-5.6 Sol headline from the Grok 4.6 article a bad merge had handed it."""
    fake_fetcher(monkeypatch, ARTICLE.encode())
    messages: list[str] = []

    deepened = pipeline._deepened_entries(
        Settings(), FakeOllama(covers=False), [entry("Headline", "https://example.com/a")], messages.append
    )

    assert deepened[0].item.summary_zh_tw == "很短的摘要。"
    assert any("did not cover" in message for message in messages)


def test_a_rewrite_in_the_wrong_language_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """It replaces prose the extractor already language-checked, so it is held to the same guard."""
    import respx

    settings = Settings(digest_language="zh-TW", ollama_review_model="qwen3:8b")
    english = {
        "covers_the_item": True,
        "summary_zh_tw": "OpenAI previewed a mode generating seven hundred and fifty tokens per second.",
        "why_it_matters_zh_tw": "It suits real time applications that need very low latency responses.",
    }
    with respx.mock(base_url=settings.ollama_base_url) as mock:
        mock.post("/api/chat").respond(json={"message": {"content": json.dumps(english)}})
        client = create_ollama_client(settings)
        with pytest.raises(OllamaSchemaError, match="OLLAMA_DEEPEN_INVALID"):
            client.deepen_item("標題", "AI_MODEL", "TLDR AI", "article", "some article text")
        client.close()


def test_an_unexpected_parser_failure_falls_back_instead_of_ending_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """A TypeError out of the HTML parser ended a run that had already produced a full digest."""
    fake_fetcher(monkeypatch, ARTICLE.encode())
    monkeypatch.setattr(pipeline, "extract_article", lambda *_: (_ for _ in ()).throw(TypeError("parser blew up")))
    ollama = FakeOllama()
    messages: list[str] = []

    deepened = pipeline._deepened_entries(Settings(), ollama, [entry("Headline", "https://example.com/a")], messages.append)

    assert ollama.calls[0][1] == "newsletters"
    assert deepened[0].item.summary_zh_tw == "重寫後長很多的摘要內容。"
    # Named, not swallowed: a silent fallback would hide the rewrite failing for a class of pages.
    assert [message for message in messages if message.startswith("Warning")] == [
        "Warning: could not read https://example.com/a (TypeError); using the newsletter summaries"
    ]


def test_an_unreachable_page_falls_back_without_a_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """A page that cannot be fetched is ordinary; only a defect in our own parsing is reported."""
    fake_fetcher(monkeypatch, None)
    messages: list[str] = []

    pipeline._deepened_entries(Settings(), FakeOllama(), [entry("Headline", "https://example.com/a")], messages.append)

    assert [message for message in messages if message.startswith("Warning")] == []
