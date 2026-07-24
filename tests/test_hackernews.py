from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from two_much_two_read.config import HackerNewsSource, Settings
from two_much_two_read.hackernews import (
    HackerNewsClient,
    HackerNewsError,
    inspect_hackernews,
    list_hackernews,
    sync_hackernews,
)
from two_much_two_read.storage import Database

NOW = datetime(2026, 7, 23, tzinfo=UTC)


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
    assert first_sync.model_dump() == {"status": "ok", "discovered": 2, "existing": 0, "skipped": 0}
    assert second_sync.model_dump() == {"status": "ok", "discovered": 0, "existing": 2, "skipped": 0}
    database = Database(settings.database_path)
    row = database.connection.execute(
        """SELECT d.source_type,d.external_id,d.content_basis,d.content_characters,h.feed,h.feed_rank,h.fetch_status
        FROM documents d JOIN hackernews_document_state h ON h.document_id=d.id ORDER BY h.feed_rank"""
    ).fetchone()
    assert tuple(row) == ("hackernews", "1", "metadata", 0, "beststories", 1, "not_requested")
    database.close()
