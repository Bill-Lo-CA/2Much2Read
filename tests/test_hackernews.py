from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from two_much_two_read.article_fetcher import ArticleFetcher
from two_much_two_read.config import HackerNewsSource, Settings
from two_much_two_read.hackernews import (
    HackerNewsClient,
    HackerNewsError,
    inspect_hackernews,
    list_hackernews,
    sync_hackernews,
)
from two_much_two_read.storage import Database

NOW = datetime.now(UTC)


def source(**values: object) -> HackerNewsSource:
    return HackerNewsSource.model_validate({"id": "hn-best", "name": "HN Best", **values})


def client(responses: dict[str, object]) -> tuple[HackerNewsClient, httpx.Client]:
    def handler(request: httpx.Request) -> httpx.Response:
        response = responses[request.url.path]
        if isinstance(response, int):
            return httpx.Response(response)
        return httpx.Response(200, json=response)

    http_client = httpx.Client(base_url="https://hacker-news.firebaseio.com/v0/", transport=httpx.MockTransport(handler))
    return HackerNewsClient(http_client), http_client


def item(story_id: int, **values: object) -> dict[str, object]:
    return {
        "id": story_id,
        "type": "story",
        "time": int(NOW.timestamp()),
        "title": f"Story {story_id}",
        "by": "author",
        "score": 10,
        "descendants": 2,
        "url": f"https://example.com/{story_id}",
        **values,
    }


def test_discovery_filters_items_preserves_rank_and_bounds_requests() -> None:
    active_client, http_client = client(
        {
            "/v0/beststories.json": [1, 2, 3, 4, 5],
            "/v0/item/1.json": item(1, type="job"),
            "/v0/item/2.json": item(2, score=1),
            "/v0/item/3.json": item(3),
            "/v0/item/4.json": item(4, url=None, text="<p>Self post</p>"),
        }
    )
    try:
        discovery = active_client.discover(source(max_story_candidates=5, max_articles_per_run=2, min_score=5), NOW)
    finally:
        http_client.close()

    assert [
        (candidate.document.external_id, candidate.feed_rank, candidate.content_kind) for candidate in discovery.candidates
    ] == [
        ("3", 3, "external"),
        ("4", 4, "self_post"),
    ]
    assert discovery.skipped == 2


def test_discovery_skips_item_fetch_failures_and_continues() -> None:
    active_client, http_client = client(
        {
            "/v0/beststories.json": [1, 2],
            "/v0/item/1.json": 500,
            "/v0/item/2.json": item(2),
        }
    )
    try:
        discovery = active_client.discover(source(max_story_candidates=2), NOW)
    finally:
        http_client.close()

    assert [candidate.document.external_id for candidate in discovery.candidates] == ["2"]
    assert discovery.skipped == 1


def test_discovery_limit_stops_before_fetching_later_items() -> None:
    active_client, http_client = client(
        {
            "/v0/beststories.json": [1, 2],
            "/v0/item/1.json": item(1),
        }
    )
    try:
        discovery = active_client.discover(source(max_story_candidates=2, max_articles_per_run=2), NOW, limit=1)
    finally:
        http_client.close()

    assert [candidate.document.external_id for candidate in discovery.candidates] == ["1"]


def test_discovery_rejects_item_payloads_with_the_wrong_id() -> None:
    active_client, http_client = client(
        {
            "/v0/beststories.json": [1, 2],
            "/v0/item/1.json": item(99),
            "/v0/item/2.json": item(2),
        }
    )
    try:
        discovery = active_client.discover(source(max_story_candidates=2), NOW)
    finally:
        http_client.close()

    assert [candidate.document.external_id for candidate in discovery.candidates] == ["2"]
    assert discovery.skipped == 1


def test_malformed_feed_is_a_source_error() -> None:
    active_client, http_client = client({"/v0/beststories.json": {"ids": [1]}})
    try:
        with pytest.raises(HackerNewsError, match="HN_FEED_FETCH_FAILED"):
            active_client.discover(source(), NOW)
    finally:
        http_client.close()


def test_list_inspect_and_sync_are_bounded_and_deduplicated(tmp_path: Path) -> None:
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text(
        "sources:\n  - type: hackernews\n    id: hn-best\n    name: HN Best\n    max_story_candidates: 2\n",
        encoding="utf-8",
    )
    settings = Settings(
        sources_config_path=sources_path,
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
    )
    active_client, http_client = client(
        {
            "/v0/beststories.json": [1, 2],
            "/v0/item/1.json": item(1),
            "/v0/item/2.json": item(2),
        }
    )
    try:
        listed = list_hackernews(settings, "hn-best", 1, active_client)
        inspected = inspect_hackernews(settings, "hn-best", 1, active_client)
        first_sync = sync_hackernews(settings, "hn-best", client=active_client)
        second_sync = sync_hackernews(settings, "hn-best", client=active_client)
    finally:
        http_client.close()

    assert [story.story_id for story in listed.stories] == [1]
    assert inspected.story.story_id == 1
    assert first_sync.model_dump() == {"status": "ok", "discovered": 2, "existing": 0, "skipped": 0, "fetched": 0, "failed": 0}
    assert second_sync.model_dump() == {"status": "ok", "discovered": 0, "existing": 2, "skipped": 0, "fetched": 0, "failed": 0}
    database = Database(settings.database_path)
    row = database.connection.execute(
        """SELECT d.source_type,d.external_id,d.content_basis,d.content_characters,h.feed,h.feed_rank,h.fetch_status
        FROM documents d JOIN hackernews_document_state h ON h.document_id=d.id ORDER BY h.feed_rank"""
    ).fetchone()
    assert tuple(row) == ("hackernews", "1", "metadata", 0, "beststories", 1, "not_requested")
    database.close()


def test_inspect_fetches_a_safe_article_without_writing_local_state(tmp_path: Path) -> None:
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text("sources:\n  - type: hackernews\n    id: hn-best\n    name: HN Best\n", encoding="utf-8")
    settings = Settings(
        sources_config_path=sources_path,
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
    )
    active_client, http_client = client({"/v0/beststories.json": [1], "/v0/item/1.json": item(1)})

    def article_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(200, headers={"content-type": "text/html"}, content=("<article><p>Useful text. </p>" * 50).encode())

    article_client = httpx.Client(transport=httpx.MockTransport(article_handler))
    try:
        result = inspect_hackernews(
            settings,
            "hn-best",
            1,
            active_client,
            fetch_article=True,
            article_fetcher=ArticleFetcher(article_client, lambda _: ["93.184.216.34"]),
        )
    finally:
        http_client.close()
        article_client.close()

    assert result.story.fetch_status == "fetched"
    assert result.story.content_basis == "article"
    assert str(result.story.final_url) == "https://example.com/1"
    assert result.story.content_characters >= 500
    assert result.story.content_preview is not None
    assert not settings.database_path.exists()


def test_sync_fetch_articles_persists_metadata_but_not_article_text(tmp_path: Path) -> None:
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text("sources:\n  - type: hackernews\n    id: hn-best\n    name: HN Best\n", encoding="utf-8")
    settings = Settings(
        sources_config_path=sources_path,
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
    )
    active_client, http_client = client({"/v0/beststories.json": [1], "/v0/item/1.json": item(1)})
    article_requests = 0

    def article_handler(request: httpx.Request) -> httpx.Response:
        nonlocal article_requests
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        article_requests += 1
        return httpx.Response(200, headers={"content-type": "text/html"}, content=("<article><p>Useful text. </p>" * 50).encode())

    article_client = httpx.Client(transport=httpx.MockTransport(article_handler))
    fetcher = ArticleFetcher(article_client, lambda _: ["93.184.216.34"])
    try:
        first = sync_hackernews(settings, "hn-best", client=active_client, fetch_articles=True, article_fetcher=fetcher)
        second = sync_hackernews(settings, "hn-best", client=active_client, fetch_articles=True, article_fetcher=fetcher)
    finally:
        http_client.close()
        article_client.close()

    assert first.model_dump() == {"status": "ok", "discovered": 1, "existing": 0, "skipped": 0, "fetched": 1, "failed": 0}
    assert second.model_dump() == {"status": "ok", "discovered": 0, "existing": 1, "skipped": 0, "fetched": 0, "failed": 0}
    assert article_requests == 1
    database = Database(settings.database_path)
    row = database.connection.execute(
        """SELECT d.content_basis,d.content_sha256,d.content_characters,d.state,h.final_url,h.fetch_status
        FROM documents d JOIN hackernews_document_state h ON h.document_id=d.id"""
    ).fetchone()
    database.close()
    assert row["content_basis"] == "article"
    assert row["content_characters"] >= 500
    assert row["state"] == "discovered"
    assert row["final_url"] == "https://example.com/1"
    assert row["fetch_status"] == "fetched"
    assert len(str(row[1])) == 64

    active_client, http_client = client({"/v0/beststories.json": [1], "/v0/item/1.json": item(1)})
    try:
        inspected = inspect_hackernews(settings, "hn-best", 1, active_client)
    finally:
        http_client.close()
    assert inspected.story.fetch_status == "fetched"
    assert inspected.story.content_basis == "article"
    assert inspected.story.content_characters >= 500
    assert inspected.story.content_preview is None
