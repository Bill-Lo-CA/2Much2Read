from __future__ import annotations

import sqlite3
from base64 import urlsafe_b64encode
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from two_much_two_read import mail_operations, pipeline
from two_much_two_read.article_fetcher import ArticleFetchError, ResolvedUrl
from two_much_two_read.command_models import NewsletterRetryResult, NewsletterRunResult
from two_much_two_read.config import HackerNewsSource, Settings
from two_much_two_read.hackernews import HackerNewsCandidate, HackerNewsDiscovery, ResolvedHackerNewsContent
from two_much_two_read.mime import EmailExtractionError
from two_much_two_read.ollama import OllamaSchemaError
from two_much_two_read.pipeline import deliver_digest, run_pipeline
from two_much_two_read.schemas import (
    ArticleAnalysis,
    EmailExtraction,
    ExtractedEmailContent,
    NewsletterItemAnalysis,
    ResolvedContent,
    SourceDocument,
)
from two_much_two_read.storage import Database
from two_read_runtime.discord import DiscordDeliveryError, DiscordDestination


def write_sources(path: Path, *, enabled: bool = True) -> None:
    path.write_text(
        f"sources:\n  - id: alphasignal\n    name: AlphaSignal\n    enabled: {str(enabled).lower()}\n"
        "    gmail_query: 'from:alphasignal.ai'\n",
        encoding="utf-8",
    )


def discover_gmail_document(
    database: Database, gmail_id: str, source_id: str, body: str = "body", *, force: bool = False
) -> int | None:
    return database.discover_gmail_document(
        gmail_id,
        "thread",
        source_id,
        datetime(2026, 7, 23, tzinfo=UTC),
        "subject",
        "sender",
        body,
        False,
        force,
    )


class StubGmailClient:
    def __init__(self, message_ids: list[str], messages: dict[str, dict[str, object]]) -> None:
        self.message_ids = message_ids
        self.messages = messages
        self.applied_labels: list[tuple[str, str]] = []

    def ensure_labels(self) -> None:
        pass

    def list_messages(self, query: str, limit: int | None = None) -> list[str]:
        return self.message_ids if limit is None else self.message_ids[:limit]

    def iter_messages(self, query: str):
        yield from self.message_ids

    def get_message(self, message_id: str) -> dict[str, object]:
        return self.messages[message_id]

    def sync_processing_label(self, message_id: str, state: str) -> None:
        self.applied_labels.append((message_id, state))


class StubOllamaClient:
    def __init__(self, extraction: EmailExtraction | None = None, error: Exception | None = None) -> None:
        self.extraction = extraction
        self.error = error

    def extract(self, source_id: str, content: str, truncated: bool, max_items: int) -> EmailExtraction:
        if self.error is not None:
            raise self.error
        assert self.extraction is not None
        return self.extraction


@pytest.fixture(autouse=True)
def bypass_digest_review_models(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeReranker:
        def __init__(self, _model: str, _device: str) -> None:
            pass

        def rank(self, entries):
            return entries

        def close(self) -> None:
            pass

    monkeypatch.setattr(pipeline, "RelevanceReranker", FakeReranker)
    monkeypatch.setattr(pipeline, "_unload_model", lambda *_: None)
    monkeypatch.setattr(
        pipeline,
        "_reviewed_entries",
        lambda settings, _ollama, entries: [
            replace(entry, review_score=100 - index) for index, entry in enumerate(entries[: settings.digest_max_items])
        ],
    )


def test_gmail_url_enrichment_owns_and_persists_resolved_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources_path = tmp_path / "sources.yaml"
    write_sources(sources_path)
    settings = Settings(
        sources_config_path=sources_path,
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
    )

    def encoded(value: str) -> str:
        return urlsafe_b64encode(value.encode()).decode().rstrip("=")

    gmail = StubGmailClient(
        ["gmail-1"],
        {
            "gmail-1": {
                "threadId": "thread-1",
                "internalDate": "1784786400000",
                "payload": {
                    "headers": [{"name": "Subject", "value": "Newsletter"}, {"name": "From", "value": "news@example.com"}],
                    "parts": [
                        {"mimeType": "text/plain", "body": {"data": encoded("A useful article")}},
                        {
                            "mimeType": "text/html",
                            "body": {
                                "data": encoded('<h2>Useful article</h2><a href="https://short.example/go">Useful article</a>')
                            },
                        },
                    ],
                },
            }
        },
    )
    ollama = StubOllamaClient(
        EmailExtraction(
            source_id="alphasignal",
            newsletter_title="Newsletter",
            newsletter_date=None,
            overview_zh_tw="摘要",
            items=[
                NewsletterItemAnalysis(
                    title="Useful article",
                    category="OTHER",
                    summary_zh_tw="摘要",
                    why_it_matters_zh_tw="原因",
                    importance=7,
                    confidence=0.9,
                )
            ],
        )
    )

    class FakeFetcher:
        def resolve_url(self, raw_url: str) -> ResolvedUrl:
            assert raw_url == "https://short.example/go"
            return ResolvedUrl(raw_url, "https://publisher.example/article", "https://publisher.example/canonical")

    monkeypatch.setattr(pipeline, "credentials", lambda *args: object())
    monkeypatch.setattr(pipeline, "GmailClient", lambda _: gmail)
    monkeypatch.setattr(pipeline, "create_ollama_client", lambda _: ollama)
    monkeypatch.setattr(pipeline, "ArticleFetcher", FakeFetcher)

    result = run_pipeline(settings, no_deliver=True, now=datetime(2026, 7, 24, tzinfo=UTC))

    database = Database(settings.database_path)
    row = database.connection.execute(
        "SELECT source_url,raw_url,resolved_url,canonical_url,url_match_status,url_resolution_status FROM items"
    ).fetchone()
    database.close()
    assert result.status == "ok"
    assert tuple(row) == (
        "https://publisher.example/canonical",
        "https://short.example/go",
        "https://publisher.example/article",
        "https://publisher.example/canonical",
        "matched",
        "resolved",
    )


class FakeDigestDatabase:
    def __init__(self, pending: list[dict[str, object]], failure_error: Exception | None = None) -> None:
        self.pending = pending
        self.failure_error = failure_error
        self.failed: list[tuple[int, str]] = []
        self.finished: list[tuple[int, list[str]]] = []
        self.progress: list[tuple[int, list[str]]] = []
        self.closed = False

    def pending_digests(self) -> list[dict[str, object]]:
        return self.pending

    def delivery_checkpoint(self, digest_id: int, destination_key: str) -> object:
        return next(digest for digest in self.pending if digest["id"] == digest_id)["discord_message_ids_json"]

    def record_delivery_progress(self, digest_id: int, message_ids: list[str], destination_key: str | None = None) -> None:
        self.progress.append((digest_id, message_ids))

    def finish_delivery(self, digest_id: int, message_ids: list[str], destination_key: str | None = None) -> None:
        self.finished.append((digest_id, message_ids))

    def fail_delivery(self, digest_id: int, error_code: str) -> None:
        if self.failure_error is not None:
            raise self.failure_error
        self.failed.append((digest_id, error_code))

    def close(self) -> None:
        self.closed = True


class RecordingLock:
    def __init__(self) -> None:
        self.entered = False

    def __enter__(self) -> RecordingLock:
        self.entered = True
        return self

    def __exit__(self, *args: object) -> None:
        pass


def test_unknown_source_lists_enabled_ids(tmp_path: Path) -> None:
    sources_path = tmp_path / "sources.yaml"
    write_sources(sources_path)

    with pytest.raises(
        ValueError,
        match="unknown or disabled source_id 'ai-newspaper'; enabled source IDs: alphasignal",
    ):
        run_pipeline(Settings(sources_config_path=sources_path), source_id="ai-newspaper", dry_run=True)


@pytest.mark.parametrize(("length", "truncated"), [(45_000, False), (45_001, True)])
def test_pipeline_uses_original_analysis_length_for_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, length: int, truncated: bool
) -> None:
    sources_path = tmp_path / "sources.yaml"
    write_sources(sources_path)
    settings = Settings(
        sources_config_path=sources_path,
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
    )
    calls: list[tuple[str, bool]] = []

    class FakeGmailClient:
        def ensure_labels(self) -> None:
            pass

        def iter_messages(self, query: str):
            yield "gmail-1"

        def get_message(self, message_id: str) -> dict[str, object]:
            return {"internalDate": "0", "threadId": "thread", "payload": {"body": "ignored"}}

        def sync_processing_label(self, message_id: str, state: str) -> None:
            pass

    class FakeOllamaClient:
        def extract(self, source_id: str, content: str, was_truncated: bool, max_items: int) -> EmailExtraction:
            calls.append((content, was_truncated))
            return EmailExtraction(
                source_id=source_id, newsletter_title="Test", newsletter_date=None, overview_zh_tw="摘要", items=[]
            )

    body = "x" * length
    monkeypatch.setattr(pipeline, "credentials", lambda *args: object())
    monkeypatch.setattr(pipeline, "GmailClient", lambda _: FakeGmailClient())
    monkeypatch.setattr(pipeline, "create_ollama_client", lambda _: FakeOllamaClient())
    monkeypatch.setattr(
        pipeline,
        "extract_gmail_payload",
        lambda payload: ExtractedEmailContent(analysis_text=body[:45_000], original_characters=length),
    )

    assert run_pipeline(settings, no_deliver=True).processed == 1
    assert calls == [(body[:45_000], truncated)]
    database = Database(settings.database_path)
    assert database.connection.execute("SELECT content_characters FROM documents").fetchone()[0] == min(length, 45_000)
    database.close()


def test_no_enabled_sources_has_distinct_error(tmp_path: Path) -> None:
    sources_path = tmp_path / "sources.yaml"
    write_sources(sources_path, enabled=False)

    with pytest.raises(ValueError, match="no enabled sources configured"):
        run_pipeline(Settings(sources_config_path=sources_path), dry_run=True)


def test_hacker_news_source_runs_without_gmail_and_skips_processed_items(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text(
        "sources:\n  - type: hackernews\n    id: hn-best\n    name: Hacker News Best\n",
        encoding="utf-8",
    )
    settings = Settings(
        sources_config_path=sources_path,
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
    )
    candidate = HackerNewsCandidate(
        SourceDocument(
            source_type="hackernews",
            source_id="hn-best",
            external_id="123",
            title="HN article",
            author="author",
            published_at=datetime(2026, 7, 24, tzinfo=UTC),
            source_url="https://example.com/requested",
            discussion_url="https://news.ycombinator.com/item?id=123",
            metadata={},
        ),
        "beststories",
        1,
        42,
        7,
        "external",
        None,
    )

    class FakeHackerNewsClient:
        def discover(self, *args: object, **kwargs: object) -> HackerNewsDiscovery:
            return HackerNewsDiscovery([candidate], 0)

        def close(self) -> None:
            pass

    class FakeOllamaClient:
        calls = 0

        def analyze_article(self, *args: object, **kwargs: object) -> ArticleAnalysis:
            self.calls += 1
            return ArticleAnalysis(
                title="ignored model title",
                category="AI_MODEL",
                summary_zh_tw="摘要",
                why_it_matters_zh_tw="原因",
                importance=8,
                confidence=0.9,
                tags=["ai"],
            )

    ollama = FakeOllamaClient()
    monkeypatch.setattr(pipeline, "credentials", lambda *args: pytest.fail("HN-only run must not initialize Gmail"))
    monkeypatch.setattr(pipeline, "HackerNewsClient", FakeHackerNewsClient)
    monkeypatch.setattr(pipeline, "create_ollama_client", lambda _: ollama)
    monkeypatch.setattr(
        pipeline,
        "resolve_hackernews_candidate",
        lambda candidate, fetcher: ResolvedHackerNewsContent(
            ResolvedContent(
                document=candidate.document,
                text="usable article text",
                basis="article",
                final_url="https://example.com/final",
                truncated=False,
            ),
            "article title",
        ),
    )

    result = run_pipeline(settings, source_id="hn-best", no_deliver=True, now=datetime(2026, 7, 24, tzinfo=UTC))
    repeated = run_pipeline(settings, source_id="hn-best", no_deliver=True, now=datetime(2026, 7, 25, tzinfo=UTC))

    assert result.processed == 1
    assert repeated.status == "no_content"
    assert ollama.calls == 1
    database = Database(settings.database_path)
    row = database.connection.execute(
        """SELECT d.state,i.title,h.final_url FROM documents d JOIN items i ON i.document_id=d.id
        JOIN hackernews_document_state h ON h.document_id=d.id"""
    ).fetchone()
    database.close()
    assert tuple(row) == ("processed", "HN article", "https://example.com/final")


def test_hacker_news_force_retries_only_failed_documents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text(
        "sources:\n  - type: hackernews\n    id: hn-best\n    name: Hacker News Best\n",
        encoding="utf-8",
    )
    settings = Settings(
        sources_config_path=sources_path,
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
    )
    candidate = HackerNewsCandidate(
        SourceDocument(
            source_type="hackernews",
            source_id="hn-best",
            external_id="123",
            title="HN article",
            published_at=datetime(2026, 7, 24, tzinfo=UTC),
            source_url="https://example.com/requested",
            discussion_url="https://news.ycombinator.com/item?id=123",
        ),
        "beststories",
        1,
        42,
        7,
        "external",
        None,
    )
    database = Database(settings.database_path)
    document_id, _ = database.store_hackernews_metadata(candidate.document, candidate.feed, 1, 42, 7)
    database.record_hackernews_fetch_failure(document_id, ArticleFetchError("ARTICLE_FETCH_FAILED").code)
    database.close()

    class FakeHackerNewsClient:
        def retry_candidate(self, *args: object, **kwargs: object) -> HackerNewsCandidate:
            return candidate

        def close(self) -> None:
            pass

    class FakeOllamaClient:
        calls = 0

        def analyze_article(self, *args: object, **kwargs: object) -> ArticleAnalysis:
            self.calls += 1
            return ArticleAnalysis(
                title="ignored",
                category="AI_MODEL",
                summary_zh_tw="摘要",
                why_it_matters_zh_tw="原因",
                importance=8,
                confidence=0.9,
            )

    ollama = FakeOllamaClient()
    monkeypatch.setattr(pipeline, "HackerNewsClient", FakeHackerNewsClient)
    monkeypatch.setattr(pipeline, "create_ollama_client", lambda _: ollama)
    monkeypatch.setattr(
        pipeline,
        "resolve_hackernews_candidate",
        lambda candidate, fetcher: ResolvedHackerNewsContent(
            ResolvedContent(document=candidate.document, text="usable", basis="article", truncated=False), None
        ),
    )

    retried = run_pipeline(
        settings,
        source_id="hn-best",
        max_messages=1,
        no_deliver=True,
        force=True,
        now=datetime(2026, 7, 24, tzinfo=UTC),
    )
    repeated = run_pipeline(
        settings,
        source_id="hn-best",
        max_messages=1,
        no_deliver=True,
        force=True,
        now=datetime(2026, 7, 25, tzinfo=UTC),
    )

    assert retried.processed == 1
    assert repeated.processed == 0
    assert ollama.calls == 1


def test_mixed_gmail_and_hackernews_sources_run_together(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text(
        "sources:\n  - id: alphasignal\n    name: AlphaSignal\n    gmail_query: from:alphasignal.ai\n"
        "  - type: hackernews\n    id: hn-best\n    name: Hacker News Best\n",
        encoding="utf-8",
    )
    settings = Settings(
        sources_config_path=sources_path,
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
    )
    seen: list[tuple[str, int]] = []

    class FakeGmailClient:
        def ensure_labels(self) -> None:
            pass

    class FakeHackerNewsClient:
        def close(self) -> None:
            pass

    def process_gmail(*args: object, **kwargs: object) -> tuple[int, int, int, int, list[int], list[tuple[int, str]]]:
        budget = int(args[5])
        seen.append(("gmail", budget))
        return budget if budget == settings.gmail_max_messages_per_run else 1, 1, 1, 0, [], []

    def process_hackernews(*args: object, **kwargs: object) -> tuple[int, int, int, int, list[int]]:
        seen.append(("hackernews", int(args[4])))
        return 1, 1, 1, 0, []

    monkeypatch.setattr(pipeline, "credentials", lambda *args: object())
    monkeypatch.setattr(pipeline, "GmailClient", lambda _: FakeGmailClient())
    monkeypatch.setattr(pipeline, "HackerNewsClient", FakeHackerNewsClient)
    monkeypatch.setattr(pipeline, "create_ollama_client", lambda _: object())
    monkeypatch.setattr(pipeline, "_process_source", process_gmail)
    monkeypatch.setattr(pipeline, "_process_hackernews_source", process_hackernews)

    default_result = run_pipeline(settings, no_deliver=True)
    capped_result = run_pipeline(settings, max_messages=2, no_deliver=True)

    assert seen == [("gmail", 50), ("hackernews", 10), ("gmail", 2), ("hackernews", 1)]
    assert default_result.discovered == capped_result.discovered == 2
    assert default_result.processed == capped_result.processed == 2


def test_hacker_news_force_respects_explicit_command_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text(
        "sources:\n  - type: hackernews\n    id: hn-best\n    name: Hacker News Best\n",
        encoding="utf-8",
    )
    settings = Settings(
        sources_config_path=sources_path,
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
    )

    class FakeHackerNewsClient:
        def close(self) -> None:
            pass

    def process_hackernews(*args: object, **kwargs: object) -> tuple[int, int, int, int, list[int]]:
        assert args[4] == 1
        return 0, 0, 0, 0, []

    monkeypatch.setattr(pipeline, "HackerNewsClient", FakeHackerNewsClient)
    monkeypatch.setattr(pipeline, "create_ollama_client", lambda _: object())
    monkeypatch.setattr(pipeline, "_process_hackernews_source", process_hackernews)

    run_pipeline(settings, source_id="hn-best", max_messages=1, no_deliver=True, force=True)


def test_hackernews_deadline_failure_does_not_abort_later_story(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = HackerNewsSource(id="hn-best", name="Hacker News Best")
    candidate = HackerNewsCandidate(
        SourceDocument(
            source_type="hackernews",
            source_id="hn-best",
            external_id="123",
            title="Unreadable article",
            published_at=datetime(2026, 7, 24, tzinfo=UTC),
            source_url="https://example.com/requested",
            discussion_url="https://news.ycombinator.com/item?id=123",
        ),
        "beststories",
        1,
        42,
        7,
        "external",
        None,
    )
    successful_candidate = HackerNewsCandidate(
        SourceDocument(
            source_type="hackernews",
            source_id="hn-best",
            external_id="124",
            title="Readable article",
            published_at=datetime(2026, 7, 24, tzinfo=UTC),
            source_url="https://example.com/readable",
            discussion_url="https://news.ycombinator.com/item?id=124",
        ),
        "beststories",
        2,
        40,
        5,
        "external",
        None,
    )

    class FakeHackerNewsClient:
        def discover(self, *args: object, **kwargs: object) -> HackerNewsDiscovery:
            return HackerNewsDiscovery([candidate, successful_candidate], 0)

    class FakeOllamaClient:
        def analyze_article(self, *args: object, **kwargs: object) -> ArticleAnalysis:
            assert args[1] == 124
            return ArticleAnalysis(
                title="ignored",
                category="OTHER",
                summary_zh_tw="摘要",
                why_it_matters_zh_tw="原因",
                importance=7,
                confidence=0.8,
            )

    def resolve(current: HackerNewsCandidate, *args: object) -> ResolvedHackerNewsContent:
        if current.document.external_id == "123":
            raise ArticleFetchError("ARTICLE_FETCH_DEADLINE_EXCEEDED")
        return ResolvedHackerNewsContent(
            ResolvedContent(document=current.document, text="usable", basis="article", truncated=False), None
        )

    database = Database(tmp_path / "digest.sqlite3")
    monkeypatch.setattr(pipeline, "resolve_hackernews_candidate", resolve)

    result = pipeline._process_hackernews_source(
        database,
        FakeHackerNewsClient(),
        FakeOllamaClient(),
        source,
        2,
        lambda _: None,
        force=False,
        now=datetime(2026, 7, 24, tzinfo=UTC),
    )

    assert result[:4] == (2, 2, 1, 1)
    assert database.connection.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    database.close()


def test_empty_news_day_records_no_content_run(newsletter_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = newsletter_settings
    write_sources(settings.sources_config_path)
    gmail = StubGmailClient([], {})
    monkeypatch.setattr(pipeline, "credentials", lambda *args: object())
    monkeypatch.setattr(pipeline, "GmailClient", lambda credentials: gmail)

    result = run_pipeline(settings, no_deliver=True)

    assert isinstance(result, NewsletterRunResult)
    assert result.model_dump() == {
        "status": "no_content",
        "discovered": 0,
        "processed": 0,
        "failed": 0,
        "delivered": 0,
        "delivery_succeeded": 0,
        "delivery_failed": 0,
        "delivery_pending": 0,
        "reason": None,
    }

    database = Database(settings.database_path)
    row = database.connection.execute(
        "SELECT run_type,status,discovered_count,processed_count,failed_count,delivered_digest_count FROM runs"
    ).fetchone()
    assert tuple(row) == ("newsletter_digest", "no_content", 0, 0, 0, 0)
    database.close()


def test_run_pipeline_uses_one_captured_time_for_digest_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources_path = tmp_path / "sources.yaml"
    write_sources(sources_path)
    settings = Settings(
        sources_config_path=sources_path,
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
    )

    class FakeGmailClient:
        def ensure_labels(self) -> None:
            pass

    monkeypatch.setattr(pipeline, "credentials", lambda *args: object())
    monkeypatch.setattr(pipeline, "GmailClient", lambda _: FakeGmailClient())
    monkeypatch.setattr(pipeline, "_process_source", lambda *args, **kwargs: (0, 0, 0, 0, [], []))
    monkeypatch.setattr(pipeline, "render_digest", lambda *args: "digest")
    now = datetime(2026, 1, 1, 9, 30, tzinfo=ZoneInfo("America/Montreal"))

    assert run_pipeline(settings, no_deliver=True, force=True, now=now).status == "ok"

    database = Database(settings.database_path)
    row = database.connection.execute("SELECT digest_key,period_start,period_end FROM digests").fetchone()
    assert tuple(row) == (
        "daily:2026-01-01:America/Montreal:all:force:2026-01-01T14:30:00+00:00",
        "2025-12-31T09:30:00-05:00",
        "2026-01-01T09:30:00-05:00",
    )
    database.close()


def test_run_pipeline_loads_models_sequentially(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources_path = tmp_path / "sources.yaml"
    write_sources(sources_path)
    settings = Settings(
        sources_config_path=sources_path,
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
    )
    events: list[str] = []

    class FakeGmailClient:
        def ensure_labels(self) -> None:
            pass

    class FakeReranker:
        def __init__(self, _model: str, device: str) -> None:
            assert device == "cpu"
            events.append("reranker:load")

        def close(self) -> None:
            events.append("reranker:unload")

    monkeypatch.setattr(pipeline, "credentials", lambda *args: object())
    monkeypatch.setattr(pipeline, "GmailClient", lambda _: FakeGmailClient())
    monkeypatch.setattr(pipeline, "create_ollama_client", lambda _: events.append("extractor:load") or object())
    monkeypatch.setattr(
        pipeline,
        "_process_source",
        lambda *args, **kwargs: events.append("extractor:run") or (0, 0, 0, 0, [], []),
    )
    monkeypatch.setattr(pipeline, "_unload_model", lambda *_: events.append("extractor:unload"))
    monkeypatch.setattr(pipeline, "RelevanceReranker", FakeReranker)
    monkeypatch.setattr(
        pipeline,
        "_ranked_entries",
        lambda *args: events.append("reranker:rank") or [],
    )
    monkeypatch.setattr(
        pipeline,
        "_reviewed_entries",
        lambda *args: events.append("reviewer:run") or [],
    )

    run_pipeline(settings, no_deliver=True)

    assert events == [
        "extractor:load",
        "extractor:run",
        "extractor:unload",
        "reranker:load",
        "reranker:rank",
        "reranker:unload",
        "reviewer:run",
    ]


def test_credentials_failure_records_a_failed_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources_path = tmp_path / "sources.yaml"
    write_sources(sources_path)
    settings = Settings(
        sources_config_path=sources_path,
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
    )
    monkeypatch.setattr(
        pipeline,
        "credentials",
        lambda *args: (_ for _ in ()).throw(ValueError("AUTH_REAUTH_REQUIRED: run '2much2read auth gmail'")),
    )

    with pytest.raises(ValueError, match="AUTH_REAUTH_REQUIRED"):
        run_pipeline(settings, no_deliver=True)

    database = Database(settings.database_path)
    row = database.connection.execute(
        "SELECT run_type,status,discovered_count,processed_count,failed_count,delivered_digest_count,error_summary FROM runs"
    ).fetchone()
    assert tuple(row) == ("newsletter_digest", "failed", 0, 0, 0, 0, "ValueError")
    database.close()


def test_deliver_digest_only_sends_selected_digest(
    newsletter_database: Database, newsletter_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = newsletter_database
    first_id = database.save_digest("daily:1", "start", "end", "UTC", "old digest")
    current_id = database.save_digest("daily:2", "start", "end", "UTC", "current digest")
    assert first_id is not None and current_id is not None
    delivered: list[str] = []

    def fake_deliver(webhook_url: str, content: str, username: str, *args: object, **kwargs: object) -> list[str]:
        delivered.append(content)
        return ["discord-1"]

    monkeypatch.setattr(pipeline, "deliver", fake_deliver)

    deliver_digest(newsletter_settings, database, current_id)

    assert delivered == ["current digest"]
    assert database.pending_digest(first_id) is not None
    assert database.pending_digest(current_id) is None


def test_retry_delivery_holds_process_lock(newsletter_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = newsletter_settings
    database = FakeDigestDatabase([{"id": 1, "rendered_content": "content", "discord_message_ids_json": None}])
    lock = RecordingLock()
    monkeypatch.setattr(pipeline, "Database", lambda _: database)
    monkeypatch.setattr(pipeline, "ProcessLock", lambda _: lock)

    def fake_deliver(*args: object) -> list[str]:
        assert lock.entered
        return ["discord-id"]

    monkeypatch.setattr(pipeline, "deliver", fake_deliver)

    result = pipeline.retry_delivery(settings)

    assert isinstance(result, NewsletterRetryResult)
    assert result.model_dump() == {"status": "ok", "delivered": 1, "failed": 0, "failed_by_error_code": {}}
    assert database.finished == [(1, ["discord-id"])]
    assert database.closed


def test_retry_delivery_continues_after_a_failed_digest(newsletter_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = newsletter_settings
    database = FakeDigestDatabase(
        [
            {"id": 1, "rendered_content": "bad", "discord_message_ids_json": None},
            {"id": 2, "rendered_content": "good", "discord_message_ids_json": None},
        ]
    )

    def fake_deliver(*args: object) -> list[str]:
        if args[1] == "bad":
            raise DiscordDeliveryError("delivery failed")
        return ["discord-id"]

    monkeypatch.setattr(pipeline, "deliver", fake_deliver)

    assert pipeline.retry_delivery(settings, database).model_dump() == {
        "status": "partial",
        "delivered": 1,
        "failed": 1,
        "failed_by_error_code": {"DISCORD_DELIVERY_FAILED": 1},
    }
    assert database.failed == [(1, "DISCORD_DELIVERY_FAILED")]
    assert database.finished == [(2, ["discord-id"])]


def test_retry_delivery_stops_when_recording_a_failure_hits_the_database(
    newsletter_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = newsletter_settings
    database = FakeDigestDatabase(
        [
            {"id": 1, "rendered_content": "bad", "discord_message_ids_json": None},
            {"id": 2, "rendered_content": "good", "discord_message_ids_json": None},
        ],
        sqlite3.OperationalError("database unavailable"),
    )
    monkeypatch.setattr(pipeline, "deliver", lambda *args: (_ for _ in ()).throw(DiscordDeliveryError("delivery failed")))

    with pytest.raises(sqlite3.OperationalError, match="database unavailable"):
        pipeline.retry_delivery(settings, database)

    assert database.finished == []


def test_retry_delivery_preserves_corrupt_checkpoint_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
        discord_webhook_url="https://discord.com/api/webhooks/123456789012345678/test-webhook-token",
    )
    database = Database(settings.database_path)
    corrupt_id = database.save_digest("daily:corrupt", "start", "end", "UTC", "corrupt")
    good_id = database.save_digest("daily:good", "start", "end", "UTC", "good")
    assert corrupt_id is not None and good_id is not None
    database.connection.execute(
        "UPDATE digests SET discord_message_ids_json='not json',discord_destination_key=? WHERE id=?",
        (settings.discord_destinations()[0].key, corrupt_id),
    )
    database.connection.commit()
    monkeypatch.setattr(pipeline, "deliver", lambda *args: ["discord-id"])

    assert pipeline.retry_delivery(settings, database).model_dump() == {
        "status": "partial",
        "delivered": 1,
        "failed": 1,
        "failed_by_error_code": {"DISCORD_MESSAGE_IDS_CORRUPT": 1},
    }
    error_code = database.connection.execute(
        "SELECT last_error_code FROM digest_deliveries WHERE digest_id=?", (corrupt_id,)
    ).fetchone()[0]
    assert error_code == "DISCORD_MESSAGE_IDS_CORRUPT"
    assert database.pending_digest(good_id) is None
    database.close()


def test_reset_corrupt_delivery_checkpoint(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
        discord_webhook_url="https://discord.com/api/webhooks/123456789012345678/test-webhook-token",
    )
    database = Database(settings.database_path)
    destinations = settings.discord_destinations()
    digest_id = database.save_digest("daily:corrupt", "start", "end", "UTC", "corrupt", destinations=destinations)
    assert digest_id is not None
    delivery_id = int(database.digest_deliveries(digest_id, destinations)[0]["id"])
    database.record_digest_delivery_progress(delivery_id, ["partial"])
    database.fail_digest_delivery(delivery_id, "DISCORD_MESSAGE_IDS_CORRUPT", destinations)
    database.close()

    assert pipeline.reset_corrupt_delivery(settings, delivery_id).model_dump() == {"status": "ok", "delivery_id": delivery_id}

    database = Database(settings.database_path)
    row = database.connection.execute(
        "SELECT state,discord_message_ids_json,last_error_code FROM digest_deliveries WHERE id=?", (delivery_id,)
    ).fetchone()
    assert tuple(row) == ("pending", None, None)
    assert not database.reset_corrupt_digest_delivery(delivery_id, destinations)
    database.close()


def test_retry_sends_only_the_failed_destination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
        discord_delivery_mode="both",
        discord_webhook_url="https://discord.com/api/webhooks/123456789012345678/test-webhook-token",
        discord_bot_token="token",
        discord_bot_channel_id="123",
    )
    destinations = settings.discord_destinations()
    database = Database(settings.database_path)
    digest_id = database.save_digest("daily:both", "start", "end", "UTC", "digest", destinations=destinations)
    assert digest_id is not None
    webhook, bot = database.digest_deliveries(digest_id, destinations)
    database.finish_digest_delivery(int(webhook["id"]), ["webhook-message"], destinations)
    database.fail_digest_delivery(int(bot["id"]), "DISCORD_BOT_FORBIDDEN", destinations)

    calls: list[str] = []
    monkeypatch.setattr(
        pipeline, "deliver", lambda destination, *args, **kwargs: calls.append(destination.transport) or ["bot-message"]
    )

    assert pipeline.retry_delivery(settings, database).model_dump() == {
        "status": "ok",
        "delivered": 1,
        "failed": 0,
        "failed_by_error_code": {},
    }
    assert calls == ["bot"]
    database.close()


def test_retry_preserves_pre_validation_webhook_checkpoint_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw_webhook_url = "https://DISCORD.COM/api/webhooks/123456789012345678/test-webhook-token"
    webhook_key = f"webhook:{sha256(raw_webhook_url.encode()).hexdigest()}"
    settings = Settings(
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
        discord_delivery_mode="both",
        discord_webhook_url=raw_webhook_url,
        discord_bot_token="token",
        discord_bot_channel_id="123",
    )
    previous_destinations = [
        DiscordDestination("webhook", webhook_key, webhook_url=raw_webhook_url),
        DiscordDestination("bot", "bot:123", bot_token="token", bot_channel_id="123"),
    ]
    database = Database(settings.database_path)
    digest_id = database.save_digest(
        "daily:validated-webhook", "start", "end", "UTC", "digest", destinations=previous_destinations
    )
    assert digest_id is not None
    webhook, bot = database.digest_deliveries(digest_id, previous_destinations)
    database.finish_digest_delivery(int(webhook["id"]), ["webhook-message"], previous_destinations)
    database.fail_digest_delivery(int(bot["id"]), "DISCORD_BOT_FORBIDDEN", previous_destinations)
    calls: list[str] = []
    monkeypatch.setattr(
        pipeline, "deliver", lambda destination, *args, **kwargs: calls.append(destination.transport) or ["bot-message"]
    )

    assert pipeline.retry_delivery(settings, database).delivered == 1
    assert calls == ["bot"]
    assert settings.discord_destinations()[0].key == webhook_key
    database.close()


def test_retry_retires_a_removed_failed_destination(tmp_path: Path) -> None:
    both_settings = Settings(
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
        discord_delivery_mode="both",
        discord_webhook_url="https://discord.com/api/webhooks/123456789012345678/test-webhook-token",
        discord_bot_token="token",
        discord_bot_channel_id="123",
    )
    settings = both_settings.model_copy(update={"discord_delivery_mode": "webhook"})
    both_destinations = both_settings.discord_destinations()
    database = Database(settings.database_path)
    digest_id = database.save_digest("daily:retired", "start", "end", "UTC", "digest", destinations=both_destinations)
    assert digest_id is not None
    webhook, bot = database.digest_deliveries(digest_id, both_destinations)
    database.finish_digest_delivery(int(webhook["id"]), ["webhook-message"], both_destinations)
    database.fail_digest_delivery(int(bot["id"]), "DISCORD_BOT_FORBIDDEN", both_destinations)

    assert pipeline.retry_delivery(settings, database).model_dump() == {
        "status": "ok",
        "delivered": 0,
        "failed": 0,
        "failed_by_error_code": {},
    }
    assert database.pending_digests() == []
    retired_at = database.connection.execute("SELECT retired_at FROM digest_deliveries WHERE id=?", (int(bot["id"]),)).fetchone()[
        0
    ]
    assert retired_at is not None
    database.close()


def test_retry_adds_a_new_destination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old_settings = Settings(
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
        discord_webhook_url="https://discord.com/api/webhooks/123456789012345678/old-webhook",
    )
    settings = Settings(
        database_path=old_settings.database_path,
        lock_path=old_settings.lock_path,
        discord_webhook_url="https://discord.com/api/webhooks/123456789012345678/new-webhook",
    )
    old_destinations = old_settings.discord_destinations()
    destinations = settings.discord_destinations()
    database = Database(settings.database_path)
    digest_id = database.save_digest("daily:new-destination", "start", "end", "UTC", "digest", destinations=old_destinations)
    assert digest_id is not None
    old_delivery_id = int(database.digest_deliveries(digest_id, old_destinations)[0]["id"])
    database.fail_digest_delivery(old_delivery_id, "DISCORD_DELIVERY_FAILED", old_destinations)
    calls: list[str] = []
    monkeypatch.setattr(pipeline, "deliver", lambda destination, *args, **kwargs: calls.append(destination.key) or ["message"])

    assert pipeline.retry_delivery(settings, database).model_dump() == {
        "status": "ok",
        "delivered": 1,
        "failed": 0,
        "failed_by_error_code": {},
    }
    assert calls == [destinations[0].key]
    database.close()


def test_retry_legacy_digest_preserves_bot_checkpoint_in_both_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
        discord_delivery_mode="both",
        discord_webhook_url="https://discord.com/api/webhooks/123456789012345678/test-webhook-token",
        discord_bot_token="token",
        discord_bot_channel_id="123",
    )
    database = Database(settings.database_path)
    digest_id = database.save_digest("daily:legacy", "start", "end", "UTC", "digest")
    assert digest_id is not None
    bot_key = settings.discord_destinations()[1].key
    database.record_delivery_progress(digest_id, ["old-bot-message"], bot_key)
    database.fail_delivery(digest_id)
    calls: list[tuple[str, list[str] | None]] = []

    def fake_deliver(destination, _content, _username, message_ids, *_args, **_kwargs):
        calls.append((destination.transport, message_ids))
        return [*(message_ids or []), f"new-{destination.transport}-message"]

    monkeypatch.setattr(pipeline, "deliver", fake_deliver)

    assert pipeline.retry_delivery(settings, database).model_dump() == {
        "status": "ok",
        "delivered": 2,
        "failed": 0,
        "failed_by_error_code": {},
    }
    assert calls == [("webhook", []), ("bot", ["old-bot-message"])]
    assert database.pending_digests() == []
    assert database.has_digest_deliveries(digest_id)
    database.close()


def test_reset_legacy_corrupt_digest_checkpoint(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
        discord_webhook_url="https://discord.com/api/webhooks/123456789012345678/test-webhook-token",
    )
    database = Database(settings.database_path)
    digest_id = database.save_digest("daily:legacy-corrupt", "start", "end", "UTC", "digest")
    assert digest_id is not None
    database.record_delivery_progress(digest_id, ["partial"])
    database.fail_delivery(digest_id, "DISCORD_MESSAGE_IDS_CORRUPT")
    database.close()

    assert pipeline.reset_corrupt_delivery(settings, digest_id).model_dump() == {"status": "ok", "delivery_id": digest_id}

    database = Database(settings.database_path)
    row = database.pending_digest(digest_id)
    assert row is not None
    assert (row["discord_message_ids_json"], row["last_error_code"]) == (None, None)
    database.close()


def test_reset_delivery_does_not_fall_back_to_a_colliding_legacy_digest(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
        discord_webhook_url="https://discord.com/api/webhooks/123456789012345678/test-webhook-token",
    )
    destinations = settings.discord_destinations()
    database = Database(settings.database_path)
    legacy_id = database.save_digest("daily:legacy", "start", "end", "UTC", "legacy")
    assert legacy_id is not None
    database.record_delivery_progress(legacy_id, ["partial"])
    database.fail_delivery(legacy_id, "DISCORD_MESSAGE_IDS_CORRUPT")
    digest_id = database.save_digest("daily:current", "start", "end", "UTC", "current", destinations=destinations)
    assert digest_id is not None
    delivery_id = int(database.digest_deliveries(digest_id, destinations)[0]["id"])
    assert delivery_id == legacy_id
    database.close()

    with pytest.raises(ValueError, match="not a failed corrupt checkpoint"):
        pipeline.reset_corrupt_delivery(settings, delivery_id)

    database = Database(settings.database_path)
    row = database.pending_digest(legacy_id)
    assert row is not None
    assert row["last_error_code"] == "DISCORD_MESSAGE_IDS_CORRUPT"
    database.close()


def test_invalid_discord_config_queues_digest_for_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources_path = tmp_path / "sources.yaml"
    write_sources(sources_path)
    settings = Settings(
        sources_config_path=sources_path,
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
        discord_delivery_mode="bot",
    )

    class FakeGmailClient:
        def ensure_labels(self) -> None:
            pass

    def process_source(
        database: Database, *_args: object, **_kwargs: object
    ) -> tuple[int, int, int, int, list[int], list[tuple[int, str]]]:
        document_id = discover_gmail_document(database, "message", "alphasignal")
        assert document_id is not None
        return 1, 1, 1, 0, [document_id], []

    monkeypatch.setattr(pipeline, "credentials", lambda *args: object())
    monkeypatch.setattr(pipeline, "GmailClient", lambda _: FakeGmailClient())
    monkeypatch.setattr(pipeline, "create_ollama_client", lambda _: object())
    monkeypatch.setattr(pipeline, "_process_source", process_source)
    monkeypatch.setattr(pipeline, "render_digest", lambda *args: "digest")

    result = run_pipeline(settings, now=datetime(2026, 1, 1, tzinfo=UTC))

    assert (result.status, result.delivery_failed) == ("partial", 1)
    database = Database(settings.database_path)
    digest = database.pending_digests()[0]
    assert (digest["state"], digest["last_error_code"], digest["rendered_content"]) == (
        "failed",
        "DISCORD_CONFIG_INVALID",
        "digest",
    )
    assert database.connection.execute("SELECT state FROM documents").fetchone()["state"] == "processed"
    database.close()

    corrected_settings = Settings(
        database_path=settings.database_path,
        lock_path=settings.lock_path,
        discord_delivery_mode="bot",
        discord_bot_token="token",
        discord_bot_channel_id="123",
    )
    monkeypatch.setattr(pipeline, "deliver", lambda *args: ["message"])
    assert pipeline.retry_delivery(corrected_settings).model_dump() == {
        "status": "ok",
        "delivered": 1,
        "failed": 0,
        "failed_by_error_code": {},
    }


def test_no_deliver_queues_configured_destinations(newsletter_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = newsletter_settings
    write_sources(settings.sources_config_path)

    class FakeGmailClient:
        def ensure_labels(self) -> None:
            pass

    monkeypatch.setattr(pipeline, "credentials", lambda *args: object())
    monkeypatch.setattr(pipeline, "GmailClient", lambda _: FakeGmailClient())
    monkeypatch.setattr(pipeline, "_process_source", lambda *args, **kwargs: (0, 0, 0, 0, [], []))
    monkeypatch.setattr(pipeline, "render_digest", lambda *args: "digest")

    result = run_pipeline(settings, no_deliver=True, force=True, now=datetime(2026, 1, 1, tzinfo=UTC))

    assert result.delivery_pending == 1
    database = Database(settings.database_path)
    digest = database.pending_digests()[0]
    assert len(database.digest_deliveries(int(digest["id"]), settings.discord_destinations())) == 1
    database.close()


def test_run_pipeline_limits_messages_across_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text(
        "sources:\n"
        "  - id: first\n    name: First\n    gmail_query: from:first@example.com\n"
        "  - id: second\n    name: Second\n    gmail_query: from:second@example.com\n",
        encoding="utf-8",
    )
    settings = Settings(
        sources_config_path=sources_path,
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
    )
    iter_calls: list[str] = []

    class FakeGmailClient:
        def ensure_labels(self) -> None:
            pass

        def iter_messages(self, query: str):
            iter_calls.append(query)
            yield from ["first-1", "first-2"] if "first@example.com" in query else ["second-1"]

        def get_message(self, message_id: str) -> dict[str, object]:
            return {"internalDate": "0", "threadId": message_id, "payload": {"body": message_id}}

        def sync_processing_label(self, message_id: str, state: str) -> None:
            pass

    class FakeOllamaClient:
        def __init__(self, *args: object) -> None:
            pass

        def extract(self, source_id: str, content: str, truncated: bool, max_items: int) -> EmailExtraction:
            return EmailExtraction(
                source_id=source_id, newsletter_title="Test", newsletter_date=None, overview_zh_tw="摘要", items=[]
            )

    monkeypatch.setattr(pipeline, "credentials", lambda *args: object())
    monkeypatch.setattr(pipeline, "GmailClient", lambda credentials: FakeGmailClient())
    monkeypatch.setattr(pipeline, "create_ollama_client", lambda _: FakeOllamaClient())
    monkeypatch.setattr(pipeline, "extract_gmail_payload", lambda payload: str(payload["body"]))

    result = run_pipeline(settings, max_messages=3, no_deliver=True)

    assert len(iter_calls) == 2
    assert result.processed == 3


def test_ollama_failure_marks_one_message_failed_and_continues(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources_path = tmp_path / "sources.yaml"
    write_sources(sources_path)
    settings = Settings(
        sources_config_path=sources_path,
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
        discord_webhook_url="",
    )

    class FakeGmailClient:
        def __init__(self) -> None:
            self.applied_labels: list[tuple[str, str]] = []

        def ensure_labels(self) -> None:
            pass

        def iter_messages(self, query: str):
            yield from ["bad", "good"]

        def get_message(self, message_id: str) -> dict[str, object]:
            return {"internalDate": "0", "threadId": message_id, "payload": {"body": message_id}}

        def sync_processing_label(self, message_id: str, state: str) -> None:
            self.applied_labels.append((message_id, state))

    class FakeOllamaClient:
        def __init__(self, *args: object) -> None:
            pass

        def extract(self, source_id: str, content: str, truncated: bool, max_items: int) -> EmailExtraction:
            if content == "bad":
                raise OllamaSchemaError("OLLAMA_SCHEMA_INVALID error='missing category' response_preview='newsletter body'")
            return EmailExtraction(
                source_id=source_id,
                newsletter_title="Good news",
                newsletter_date=None,
                overview_zh_tw="摘要",
                items=[
                    {
                        "title": "Good item",
                        "category": "AI_MODEL",
                        "summary_zh_tw": "內容",
                        "why_it_matters_zh_tw": "原因",
                        "importance": 8,
                        "confidence": 0.9,
                    }
                ],
            )

    gmail = FakeGmailClient()
    monkeypatch.setattr(pipeline, "credentials", lambda *args: object())
    monkeypatch.setattr(pipeline, "GmailClient", lambda credentials: gmail)
    monkeypatch.setattr(pipeline, "create_ollama_client", lambda _: FakeOllamaClient())
    monkeypatch.setattr(pipeline, "extract_gmail_payload", lambda payload: str(payload["body"]))

    statuses: list[str] = []
    result = run_pipeline(settings, no_deliver=True, status=statuses.append)

    assert result.model_dump() == {
        "status": "partial",
        "discovered": 2,
        "processed": 1,
        "failed": 1,
        "delivered": 0,
        "delivery_succeeded": 0,
        "delivery_failed": 0,
        "delivery_pending": 0,
        "reason": None,
    }
    assert gmail.applied_labels == [
        ("bad", "failed"),
        ("good", "processed"),
    ]
    assert statuses == [
        "Starting 1 source(s)",
        "alphasignal: scanning messages",
        "alphasignal: extracting bad",
        "alphasignal: failed bad (OLLAMA_SCHEMA_INVALID error='missing category')",
        "alphasignal: extracting good",
        "alphasignal: processed good",
    ]
    database = Database(settings.database_path)
    rows = database.connection.execute(
        """SELECT g.gmail_message_id,d.state,d.last_error_code FROM documents d
        JOIN gmail_document_state g ON g.document_id=d.id ORDER BY d.id"""
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("bad", "failed", "OLLAMA_SCHEMA_INVALID error='missing category'"),
        ("good", "processed", None),
    ]
    database.close()


@pytest.mark.parametrize(
    ("error_code", "dry_run"),
    [(None, False), ("EMAIL_TOO_LARGE", False), ("EMAIL_TOO_LARGE", True)],
    ids=["empty", "budget", "budget-dry-run"],
)
def test_mime_failure_marks_one_message_failed_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_code: str | None, dry_run: bool
) -> None:
    sources_path = tmp_path / "sources.yaml"
    write_sources(sources_path)
    settings = Settings(
        sources_config_path=sources_path,
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
        discord_webhook_url="",
    )
    good_body = urlsafe_b64encode(b"good newsletter").decode().rstrip("=")
    gmail = StubGmailClient(
        ["bad", "good"],
        {
            "bad": {
                "internalDate": "0",
                "threadId": "bad",
                "payload": {"mimeType": "text/plain", "headers": [{"name": "Subject", "value": "Bad"}], "body": {}},
            },
            "good": {
                "internalDate": "0",
                "threadId": "good",
                "payload": {
                    "mimeType": "text/plain",
                    "headers": [{"name": "Subject", "value": "Good"}],
                    "body": {"data": good_body},
                },
            },
        },
    )
    extraction = EmailExtraction(
        source_id="alphasignal",
        newsletter_title="Good news",
        newsletter_date=None,
        overview_zh_tw="摘要",
        items=[
            {
                "title": "Good item",
                "category": "AI_MODEL",
                "summary_zh_tw": "內容",
                "why_it_matters_zh_tw": "原因",
                "importance": 8,
                "confidence": 0.9,
            }
        ],
    )
    monkeypatch.setattr(pipeline, "credentials", lambda *args: object())
    monkeypatch.setattr(pipeline, "GmailClient", lambda _: gmail)
    monkeypatch.setattr(pipeline, "create_ollama_client", lambda _: StubOllamaClient(extraction))
    if error_code is not None:
        extract_gmail_payload = pipeline.extract_gmail_payload

        def extract(payload: dict[str, object]):
            if payload is gmail.messages["bad"]["payload"]:
                raise EmailExtractionError(error_code)
            return extract_gmail_payload(payload)

        monkeypatch.setattr(pipeline, "extract_gmail_payload", extract)

    assert run_pipeline(settings, no_deliver=True, dry_run=dry_run).model_dump() == {
        "status": "partial",
        "discovered": 2,
        "processed": 1,
        "failed": 1,
        "delivered": 0,
        "delivery_succeeded": 0,
        "delivery_failed": 0,
        "delivery_pending": 0,
        "reason": None,
    }
    assert gmail.applied_labels == ([] if dry_run else [("bad", "failed"), ("good", "processed")])
    if dry_run:
        assert not settings.database_path.exists()
        return
    database = Database(settings.database_path)
    rows = database.connection.execute(
        """SELECT g.gmail_message_id,d.state,d.last_error_code FROM documents d
        JOIN gmail_document_state g ON g.document_id=d.id ORDER BY d.id"""
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("bad", "failed", error_code or "EMAIL_NO_USABLE_TEXT"),
        ("good", "processed", None),
    ]
    database.close()


def test_digest_render_failure_leaves_extractions_retryable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources_path = tmp_path / "sources.yaml"
    write_sources(sources_path)
    settings = Settings(
        sources_config_path=sources_path,
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
    )
    gmail = StubGmailClient(
        ["newsletter"],
        {"newsletter": {"internalDate": "0", "threadId": "thread", "payload": {"body": {"body": "newsletter"}}}},
    )
    extraction = EmailExtraction(
        source_id="alphasignal",
        newsletter_title="News",
        newsletter_date=None,
        overview_zh_tw="摘要",
        items=[
            {
                "title": "Item",
                "category": "AI_MODEL",
                "summary_zh_tw": "內容",
                "why_it_matters_zh_tw": "原因",
                "importance": 8,
                "confidence": 0.9,
            }
        ],
    )
    monkeypatch.setattr(pipeline, "credentials", lambda *args: object())
    monkeypatch.setattr(pipeline, "GmailClient", lambda _: gmail)
    monkeypatch.setattr(pipeline, "create_ollama_client", lambda _: StubOllamaClient(extraction))
    monkeypatch.setattr(pipeline, "extract_gmail_payload", lambda payload: str(payload["body"]))
    monkeypatch.setattr(pipeline, "render_digest", lambda *args: (_ for _ in ()).throw(RuntimeError("render failed")))

    with pytest.raises(RuntimeError, match="render failed"):
        run_pipeline(settings, no_deliver=True)

    database = Database(settings.database_path)
    assert database.connection.execute("SELECT state FROM documents").fetchone()["state"] == "discovered"
    assert len(database.items_for_documents([1], 10)) == 1
    database.close()
    assert gmail.applied_labels == []

    monkeypatch.setattr(pipeline, "render_digest", lambda *args: "digest")
    assert run_pipeline(settings, no_deliver=True).status == "ok"
    assert gmail.applied_labels == [("newsletter", "processed")]
    database = Database(settings.database_path)
    assert database.connection.execute("SELECT state FROM documents").fetchone()["state"] == "processed"
    assert len(database.items_for_documents([1], 10)) == 1
    database.close()


def test_ollama_transport_failure_remains_retryable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources_path = tmp_path / "sources.yaml"
    write_sources(sources_path)
    settings = Settings(
        sources_config_path=sources_path,
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
        discord_webhook_url="",
    )
    gmail = StubGmailClient(
        ["transient"],
        {"transient": {"internalDate": "0", "threadId": "transient", "payload": {"body": "transient"}}},
    )
    ollama = StubOllamaClient(error=httpx.ConnectError("Ollama unavailable"))
    monkeypatch.setattr(pipeline, "credentials", lambda *args: object())
    monkeypatch.setattr(pipeline, "GmailClient", lambda credentials: gmail)
    monkeypatch.setattr(pipeline, "create_ollama_client", lambda _: ollama)
    monkeypatch.setattr(pipeline, "extract_gmail_payload", lambda payload: str(payload["body"]))

    with pytest.raises(httpx.ConnectError, match="Ollama unavailable"):
        run_pipeline(settings, no_deliver=True)

    assert gmail.applied_labels == []
    database = Database(settings.database_path)
    assert database.connection.execute("SELECT state FROM documents").fetchone()["state"] == "discovered"
    assert tuple(database.connection.execute("SELECT status,error_summary FROM runs").fetchone()) == ("failed", "ConnectError")
    database.close()

    ollama.error = None
    ollama.extraction = EmailExtraction(
        source_id="alphasignal",
        newsletter_title="Recovered",
        newsletter_date=None,
        overview_zh_tw="摘要",
        items=[
            {
                "title": "Recovered item",
                "category": "AI_MODEL",
                "summary_zh_tw": "內容",
                "why_it_matters_zh_tw": "原因",
                "importance": 8,
                "confidence": 0.9,
            }
        ],
    )

    assert run_pipeline(settings, no_deliver=True).model_dump() == {
        "status": "ok",
        "discovered": 1,
        "processed": 1,
        "failed": 0,
        "delivered": 0,
        "delivery_succeeded": 0,
        "delivery_failed": 0,
        "delivery_pending": 0,
        "reason": None,
    }
    assert gmail.applied_labels == [("transient", "processed")]


@pytest.mark.parametrize("state", ["pending", "failed", "delivered"])
def test_existing_daily_digest_skips_before_gmail_access(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    sources_path = tmp_path / "sources.yaml"
    write_sources(sources_path)
    settings = Settings(
        sources_config_path=sources_path,
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
    )
    now = datetime(2026, 7, 22, 8, tzinfo=ZoneInfo("America/Montreal"))
    database = Database(settings.database_path)
    digest_id = database.save_digest("daily:2026-07-22:America/Montreal:all", "start", "end", "America/Montreal", "digest")
    assert digest_id is not None
    if state == "failed":
        database.fail_delivery(digest_id)
    elif state == "delivered":
        database.finish_delivery(digest_id, ["discord-1"])
    database.close()

    monkeypatch.setattr(pipeline, "credentials", lambda *args: pytest.fail("Gmail must not be accessed"))

    assert run_pipeline(settings, now=now).model_dump() == {
        "status": "skipped",
        "discovered": 0,
        "processed": 0,
        "failed": 0,
        "delivered": 0,
        "delivery_succeeded": 0,
        "delivery_failed": 0,
        "delivery_pending": 0,
        "reason": "daily_digest_exists",
    }
    database = Database(settings.database_path)
    assert database.connection.execute("SELECT status FROM runs ORDER BY id DESC").fetchone()[0] == "skipped"
    database.close()


def test_forced_run_uses_a_separate_digest_key_after_daily_reservation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources_path = tmp_path / "sources.yaml"
    write_sources(sources_path)
    settings = Settings(
        sources_config_path=sources_path,
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
    )
    now = datetime(2026, 7, 22, 8, tzinfo=ZoneInfo("America/Montreal"))
    database = Database(settings.database_path)
    assert database.save_digest("daily:2026-07-22:America/Montreal:all", "start", "end", "America/Montreal", "digest")
    database.close()

    class FakeGmailClient:
        def ensure_labels(self) -> None:
            pass

    monkeypatch.setattr(pipeline, "credentials", lambda *args: object())
    monkeypatch.setattr(pipeline, "GmailClient", lambda _: FakeGmailClient())
    monkeypatch.setattr(pipeline, "_process_source", lambda *args, **kwargs: (0, 0, 0, 0, [], []))
    monkeypatch.setattr(pipeline, "render_digest", lambda *args: "forced digest")

    assert run_pipeline(settings, force=True, no_deliver=True, now=now).status == "ok"
    database = Database(settings.database_path)
    keys = [row[0] for row in database.connection.execute("SELECT digest_key FROM digests ORDER BY id")]
    assert keys == [
        "daily:2026-07-22:America/Montreal:all",
        "daily:2026-07-22:America/Montreal:all:force:2026-07-22T12:00:00+00:00",
    ]
    database.close()


def test_label_sync_failure_is_repaired_without_reextracting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources_path = tmp_path / "sources.yaml"
    write_sources(sources_path)
    settings = Settings(
        sources_config_path=sources_path,
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
    )
    calls: list[str] = []

    class FakeGmailClient:
        fail_sync = True

        def ensure_labels(self) -> None:
            pass

        def iter_messages(self, query: str):
            yield "gmail-1"

        def get_message(self, message_id: str) -> dict[str, object]:
            calls.append("get")
            return {"internalDate": "0", "threadId": message_id, "payload": {"body": "body"}}

        def sync_processing_label(self, message_id: str, state: str) -> None:
            calls.append(f"label:{state}")
            if self.fail_sync:
                raise RuntimeError("Gmail unavailable")

    class FakeOllama:
        def extract(self, *args: object) -> EmailExtraction:
            calls.append("extract")
            return EmailExtraction(
                source_id="alphasignal", newsletter_title="News", newsletter_date=None, overview_zh_tw="摘要", items=[]
            )

    gmail = FakeGmailClient()
    monkeypatch.setattr(pipeline, "credentials", lambda *args: object())
    monkeypatch.setattr(pipeline, "GmailClient", lambda _: gmail)
    monkeypatch.setattr(pipeline, "create_ollama_client", lambda _: FakeOllama())
    monkeypatch.setattr(pipeline, "extract_gmail_payload", lambda payload: str(payload["body"]))

    assert (
        run_pipeline(settings, no_deliver=True, now=datetime(2026, 7, 22, tzinfo=ZoneInfo("America/Montreal"))).status
        == "partial"
    )
    database = Database(settings.database_path)
    assert tuple(
        database.connection.execute("SELECT label_sync_state,label_sync_error_code FROM gmail_document_state").fetchone()
    ) == (
        "failed",
        "GMAIL_LABEL_SYNC_FAILED",
    )
    database.close()

    gmail.fail_sync = False
    assert run_pipeline(settings, no_deliver=True, now=datetime(2026, 7, 23, tzinfo=ZoneInfo("America/Montreal"))).processed == 0
    assert calls == ["get", "extract", "label:processed", "label:processed"]
    database = Database(settings.database_path)
    assert tuple(
        database.connection.execute("SELECT label_sync_state,label_sync_error_code FROM gmail_document_state").fetchone()
    ) == (
        "synced",
        None,
    )
    database.close()


def test_stale_label_reconciliation_does_not_use_the_message_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources_path = tmp_path / "sources.yaml"
    write_sources(sources_path)
    settings = Settings(
        sources_config_path=sources_path,
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
    )
    database = Database(settings.database_path)
    stale_id = discover_gmail_document(database, "stale", "alphasignal")
    assert stale_id is not None
    database.store_extraction(
        stale_id,
        EmailExtraction(source_id="alphasignal", newsletter_title="Old", newsletter_date=None, overview_zh_tw="摘要", items=[]),
    )
    database.close()
    fetched: list[str] = []

    class FakeGmailClient:
        def ensure_labels(self) -> None:
            pass

        def list_messages(self, query: str, limit: int | None = None) -> list[str]:
            return ["stale"]

        def iter_messages(self, query: str):
            yield from ["stale", "new"]

        def get_message(self, message_id: str) -> dict[str, object]:
            fetched.append(message_id)
            return {"internalDate": "0", "threadId": message_id, "payload": {"body": message_id}}

        def sync_processing_label(self, message_id: str, state: str) -> None:
            assert message_id == "stale" or state == "processed"

    class FakeOllama:
        def extract(self, *args: object) -> EmailExtraction:
            return EmailExtraction(
                source_id="alphasignal", newsletter_title="New", newsletter_date=None, overview_zh_tw="摘要", items=[]
            )

    monkeypatch.setattr(pipeline, "credentials", lambda *args: object())
    monkeypatch.setattr(pipeline, "GmailClient", lambda _: FakeGmailClient())
    monkeypatch.setattr(pipeline, "create_ollama_client", lambda _: FakeOllama())
    monkeypatch.setattr(pipeline, "extract_gmail_payload", lambda payload: str(payload["body"]))

    assert run_pipeline(settings, max_messages=1, no_deliver=True).processed == 1
    assert fetched == ["new"]


def test_forced_recovery_clears_the_failure_and_remote_failed_label(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources_path = tmp_path / "sources.yaml"
    write_sources(sources_path)
    settings = Settings(
        sources_config_path=sources_path,
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
    )
    database = Database(settings.database_path)
    message_id = discover_gmail_document(database, "gmail-1", "alphasignal")
    assert message_id is not None
    database.fail_document(message_id, "OLLAMA_SCHEMA_INVALID")
    database.close()
    synced: list[tuple[str, str]] = []

    class FakeGmailClient:
        def ensure_labels(self) -> None:
            pass

        def iter_messages(self, query: str):
            yield "gmail-1"

        def get_message(self, message_id: str) -> dict[str, object]:
            return {"internalDate": "0", "threadId": "thread", "payload": {"body": "body"}}

        def sync_processing_label(self, message_id: str, state: str) -> None:
            synced.append((message_id, state))

    class FakeOllama:
        def extract(self, *args: object) -> EmailExtraction:
            return EmailExtraction(
                source_id="alphasignal", newsletter_title="Recovered", newsletter_date=None, overview_zh_tw="摘要", items=[]
            )

    monkeypatch.setattr(pipeline, "credentials", lambda *args: object())
    monkeypatch.setattr(pipeline, "GmailClient", lambda _: FakeGmailClient())
    monkeypatch.setattr(pipeline, "create_ollama_client", lambda _: FakeOllama())
    monkeypatch.setattr(pipeline, "extract_gmail_payload", lambda payload: str(payload["body"]))

    assert run_pipeline(settings, force=True, no_deliver=True).processed == 1
    assert synced == [("gmail-1", "processed")]
    database = Database(settings.database_path)
    assert tuple(database.connection.execute("SELECT state,last_error_code FROM documents").fetchone()) == ("processed", None)
    database.close()


def test_dry_run_skips_gmail_label_writes_and_persistent_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources_path = tmp_path / "sources.yaml"
    write_sources(sources_path)
    settings = Settings(
        sources_config_path=sources_path,
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
    )

    class FakeGmailClient:
        def ensure_labels(self) -> None:
            pytest.fail("dry-run must not create labels")

        def iter_messages(self, query: str):
            yield "gmail-1"

        def get_message(self, message_id: str) -> dict[str, object]:
            return {"internalDate": "0", "threadId": "thread", "payload": {"body": "body"}}

        def sync_processing_label(self, message_id: str, state: str) -> None:
            pytest.fail("dry-run must not modify labels")

    class FakeOllama:
        def extract(self, *args: object) -> EmailExtraction:
            return EmailExtraction(
                source_id="alphasignal", newsletter_title="Preview", newsletter_date=None, overview_zh_tw="摘要", items=[]
            )

    monkeypatch.setattr(pipeline, "credentials", lambda *args: object())
    monkeypatch.setattr(pipeline, "GmailClient", lambda _: FakeGmailClient())
    monkeypatch.setattr(pipeline, "create_ollama_client", lambda _: FakeOllama())
    monkeypatch.setattr(pipeline, "extract_gmail_payload", lambda payload: str(payload["body"]))

    assert run_pipeline(settings, dry_run=True).processed == 1
    assert not settings.database_path.exists()


def test_labels_reconcile_repairs_terminal_messages_and_records_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(database_path=tmp_path / "digest.sqlite3", lock_path=tmp_path / "digest.lock")
    database = Database(settings.database_path)
    processed_id = discover_gmail_document(database, "processed", "source")
    failed_id = discover_gmail_document(database, "failed", "source")
    assert processed_id is not None and failed_id is not None
    database.store_extraction(
        processed_id,
        EmailExtraction(source_id="source", newsletter_title="News", newsletter_date=None, overview_zh_tw="摘要", items=[]),
    )
    database.fail_document(failed_id, "OLLAMA_SCHEMA_INVALID")
    database.close()
    calls: list[tuple[str, str]] = []

    class FakeGmailClient:
        def ensure_labels(self) -> None:
            pass

        def sync_processing_label(self, gmail_id: str, state: str) -> None:
            calls.append((gmail_id, state))
            if gmail_id == "failed":
                raise RuntimeError("Gmail unavailable")

    monkeypatch.setattr(mail_operations, "credentials", lambda *args: object())
    monkeypatch.setattr(mail_operations, "GmailClient", lambda _: FakeGmailClient())

    assert mail_operations.reconcile_labels(settings).model_dump() == {"status": "partial", "reconciled": 1, "failed": 1}
    assert calls == [("processed", "processed"), ("failed", "failed")]
    database = Database(settings.database_path)
    rows = database.connection.execute(
        "SELECT label_sync_state,label_sync_error_code FROM gmail_document_state ORDER BY document_id"
    ).fetchall()
    assert [tuple(row) for row in rows] == [("synced", None), ("failed", "GMAIL_LABEL_SYNC_FAILED")]
    database.close()
