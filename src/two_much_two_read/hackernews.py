from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, TypeAdapter, ValidationError

from two_read_runtime.locking import ProcessLock

from .command_models import HackerNewsInspectResult, HackerNewsListResult, HackerNewsStoryView, HackerNewsSyncResult
from .config import HackerNewsSource, Settings, load_sources
from .schemas import SourceDocument
from .storage import Database

API_BASE_URL = "https://hacker-news.firebaseio.com/v0/"
HTTP_URL = TypeAdapter(HttpUrl)


class HackerNewsError(ValueError):
    pass


class HackerNewsItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int = Field(gt=0)
    type: str
    time: int = Field(gt=0)
    title: str | None = None
    by: str | None = None
    url: str | None = None
    text: str | None = None
    score: int | None = None
    descendants: int | None = None
    dead: bool = False
    deleted: bool = False


@dataclass(frozen=True)
class HackerNewsCandidate:
    document: SourceDocument
    feed: str
    feed_rank: int
    score: int
    comments: int
    content_kind: Literal["external", "self_post"]


@dataclass(frozen=True)
class HackerNewsDiscovery:
    candidates: list[HackerNewsCandidate]
    skipped: int


class HackerNewsClient:
    def __init__(self, client: httpx.Client | None = None) -> None:
        self._owned_client = client is None
        self.client = client or httpx.Client(
            base_url=API_BASE_URL,
            headers={"User-Agent": "2much2read/0.1"},
            timeout=httpx.Timeout(15, connect=5),
            follow_redirects=False,
            trust_env=False,
        )

    def close(self) -> None:
        if self._owned_client:
            self.client.close()

    def _json(self, path: str, error_code: str) -> object:
        try:
            response = self.client.get(path)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise HackerNewsError(error_code) from error

    def feed_ids(self, source: HackerNewsSource) -> list[int]:
        payload = self._json(f"{source.feed}.json", "HN_FEED_FETCH_FAILED")
        if not isinstance(payload, list) or any(not isinstance(item, int) or isinstance(item, bool) for item in payload):
            raise HackerNewsError("HN_FEED_FETCH_FAILED")
        return payload[: source.max_story_candidates]

    def item(self, story_id: int) -> HackerNewsItem | None:
        payload = self._json(f"item/{story_id}.json", "HN_ITEM_FETCH_FAILED")
        if not isinstance(payload, dict):
            return None
        try:
            item = HackerNewsItem.model_validate(payload)
        except ValidationError:
            return None
        return item if item.id == story_id else None

    @staticmethod
    def _candidate(source: HackerNewsSource, item: HackerNewsItem, feed_rank: int, now: datetime) -> HackerNewsCandidate | None:
        if item.type != "story" or item.deleted or item.dead:
            return None
        title = (item.title or "").strip()
        if not title:
            return None
        try:
            published_at = datetime.fromtimestamp(item.time, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
        if (now - published_at).total_seconds() > source.max_age_hours * 3600:
            return None
        score = item.score or 0
        comments = item.descendants or 0
        if score < source.min_score or comments < source.min_comments:
            return None
        discussion_url = HTTP_URL.validate_python(f"https://news.ycombinator.com/item?id={item.id}")
        content_kind: Literal["external", "self_post"]
        if item.url:
            try:
                document = SourceDocument(
                    source_type="hackernews",
                    source_id=source.id,
                    external_id=str(item.id),
                    title=title,
                    author=item.by,
                    published_at=published_at,
                    source_url=HTTP_URL.validate_python(item.url),
                    discussion_url=discussion_url,
                    metadata={"feed": source.feed, "feed_rank": feed_rank, "score": score, "comments": comments},
                )
            except ValidationError:
                return None
            content_kind = "external"
        elif item.text and item.text.strip():
            document = SourceDocument(
                source_type="hackernews",
                source_id=source.id,
                external_id=str(item.id),
                title=title,
                author=item.by,
                published_at=published_at,
                discussion_url=discussion_url,
                metadata={"feed": source.feed, "feed_rank": feed_rank, "score": score, "comments": comments},
            )
            content_kind = "self_post"
        else:
            return None
        return HackerNewsCandidate(document, source.feed, feed_rank, score, comments, content_kind)

    def discover(self, source: HackerNewsSource, now: datetime | None = None, limit: int | None = None) -> HackerNewsDiscovery:
        active_now = now or datetime.now(UTC)
        candidate_limit = min(source.max_articles_per_run, limit) if limit is not None else source.max_articles_per_run
        candidates: list[HackerNewsCandidate] = []
        skipped = 0
        for feed_rank, story_id in enumerate(self.feed_ids(source), start=1):
            try:
                item = self.item(story_id)
            except HackerNewsError:
                skipped += 1
                continue
            if item is None:
                skipped += 1
                continue
            candidate = self._candidate(source, item, feed_rank, active_now)
            if candidate is None:
                skipped += 1
                continue
            candidates.append(candidate)
            if len(candidates) >= candidate_limit:
                break
        return HackerNewsDiscovery(candidates, skipped)

    def inspect(self, source: HackerNewsSource, story_id: int, now: datetime | None = None) -> HackerNewsCandidate:
        try:
            feed_rank = self.feed_ids(source).index(story_id) + 1
        except ValueError as error:
            raise HackerNewsError("HN_ITEM_NOT_IN_CONFIGURED_FEED") from error
        item = self.item(story_id)
        if item is None:
            raise HackerNewsError("HN_ITEM_INVALID")
        candidate = self._candidate(source, item, feed_rank, now or datetime.now(UTC))
        if candidate is None:
            raise HackerNewsError("HN_ITEM_NOT_ELIGIBLE")
        return candidate


def hackernews_source(settings: Settings, source_id: str) -> HackerNewsSource:
    source = next((item for item in load_sources(settings.sources_config_path).sources if item.id == source_id), None)
    if source is None:
        raise ValueError(f"unknown source id {source_id!r}")
    if not isinstance(source, HackerNewsSource):
        raise ValueError(f"source {source_id!r} is not a Hacker News source")
    if not source.enabled:
        raise ValueError(f"source {source_id!r} is disabled")
    return source


def story_view(candidate: HackerNewsCandidate) -> HackerNewsStoryView:
    document = candidate.document
    assert document.discussion_url is not None
    return HackerNewsStoryView(
        source_id=document.source_id,
        story_id=int(document.external_id),
        feed=candidate.feed,
        feed_rank=candidate.feed_rank,
        title=document.title,
        author=document.author,
        published_at=document.published_at,
        score=candidate.score,
        comments=candidate.comments,
        requested_url=document.source_url,
        discussion_url=document.discussion_url,
        content_kind=candidate.content_kind,
    )


def _client(client: HackerNewsClient | None) -> tuple[HackerNewsClient, bool]:
    return (client, False) if client is not None else (HackerNewsClient(), True)


def list_hackernews(
    settings: Settings, source_id: str, limit: int, client: HackerNewsClient | None = None
) -> HackerNewsListResult:
    source = hackernews_source(settings, source_id)
    active_client, close_client = _client(client)
    try:
        discovery = active_client.discover(source, limit=limit)
        return HackerNewsListResult(stories=[story_view(item) for item in discovery.candidates], skipped=discovery.skipped)
    finally:
        if close_client:
            active_client.close()


def inspect_hackernews(
    settings: Settings, source_id: str, story_id: int, client: HackerNewsClient | None = None
) -> HackerNewsInspectResult:
    source = hackernews_source(settings, source_id)
    active_client, close_client = _client(client)
    try:
        return HackerNewsInspectResult(story=story_view(active_client.inspect(source, story_id)))
    finally:
        if close_client:
            active_client.close()


def sync_hackernews(
    settings: Settings, source_id: str, force: bool = False, client: HackerNewsClient | None = None
) -> HackerNewsSyncResult:
    source = hackernews_source(settings, source_id)
    active_client, close_client = _client(client)
    try:
        with ProcessLock(settings.lock_path):
            discovery = active_client.discover(source)
            database = Database(settings.database_path)
            try:
                discovered = 0
                existing = 0
                for candidate in discovery.candidates:
                    _, created = database.store_hackernews_metadata(
                        candidate.document,
                        candidate.feed,
                        candidate.feed_rank,
                        candidate.score,
                        candidate.comments,
                        force=force,
                    )
                    if created:
                        discovered += 1
                    else:
                        existing += 1
                return HackerNewsSyncResult(discovered=discovered, existing=existing, skipped=discovery.skipped)
            finally:
                database.close()
    finally:
        if close_client:
            active_client.close()
