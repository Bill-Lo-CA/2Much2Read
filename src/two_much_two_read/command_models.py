from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

from .config import Source


class CommandResult(BaseModel):
    status: str = "ok"


class NewsletterRunResult(CommandResult):
    status: Literal["ok", "partial", "no_content", "skipped"]
    discovered: int
    processed: int
    failed: int
    # Hacker News feed items rejected before processing, including normal filtering and unreadable
    # items. Omitted when zero so an ordinary run's output is unchanged.
    skipped: int | None = None
    delivered: int
    delivery_succeeded: int = 0
    delivery_failed: int = 0
    delivery_pending: int = 0
    reason: Literal["daily_digest_exists"] | None = None


class NewsletterRetryResult(CommandResult):
    status: Literal["ok", "partial", "failed"] = "ok"
    delivered: int
    failed: int
    failed_by_error_code: dict[str, int] = Field(default_factory=dict)


class DeliveryCheckpointResetResult(CommandResult):
    delivery_id: int


class MaintenancePruneResult(CommandResult):
    retention_days: int
    cutoff: str
    dry_run: bool = False
    # Per table, so it is visible that the document and Gmail-state ledgers were not touched.
    deleted: dict[str, int]
    reclaimed_bytes: int = 0


class MailSelector(BaseModel):
    source: str | None = None
    query: str | None = None
    subscription: str | None = None

    @field_validator("source", "query", "subscription")
    @classmethod
    def nonblank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("selector values must not be blank")
        return value

    @model_validator(mode="after")
    def at_most_one(self) -> MailSelector:
        if sum(value is not None for value in (self.source, self.query, self.subscription)) > 1:
            raise ValueError("at most one of --source, --query, or --subscription is allowed")
        return self


class MailSummary(BaseModel):
    id: str
    received: object | None
    sender: str | None
    subject: str | None


class MailListResult(CommandResult):
    mails: list[MailSummary]


class MailMetadata(BaseModel):
    received: object | None
    headers: list[dict[str, object]]
    label_ids: list[object]
    mime_type: object | None


class ParsedMail(BaseModel):
    text: str
    original_characters: int
    input_characters: int
    truncated: bool


class MailInspectionResult(CommandResult):
    id: str
    metadata: MailMetadata
    parsed: ParsedMail
    extraction: dict[str, object] | None = None


class SubscriptionCandidate(BaseModel):
    id: str
    name: str
    key: str
    sender: str
    from_header: str
    list_id: str | None
    subject: str | None
    label: str | None
    configured: bool
    base_query: str
    filter_criteria: dict[str, str | bool | int]
    query_ambiguous: bool
    proposal: Source


class SubscriptionView(BaseModel):
    id: str
    name: str
    key: str
    sender: str
    from_header: str
    list_id: str | None
    subject: str | None
    label: str | None
    configured: bool
    base_query: str
    query_ambiguous: bool


class SubscriptionListResult(CommandResult):
    subscriptions: list[SubscriptionView]


class SubscriptionSyncResult(CommandResult):
    sources: list[Source]
    ambiguous: list[Source] = Field(default_factory=list)


class LabelsResult(CommandResult):
    labels: list[str]


class LabelsReconcileResult(CommandResult):
    status: Literal["ok", "partial"] = "ok"
    reconciled: int
    failed: int


class FilterView(BaseModel):
    source_id: str
    label: str
    filter_id: str | None
    status: str


class FiltersResult(CommandResult):
    filters: list[FilterView]


class DoctorResult(CommandResult):
    checks: dict[str, str]
    # Which keys, not just that something is wrong. `checks` values are status words from a closed
    # vocabulary because that is what the healthy/warning verdict reads; the operator's own key
    # names would break it, so they are reported alongside.
    unknown_env_keys: list[str] | None = None


HackerNewsFetchStatus = Literal[
    "not_requested",
    "fetched",
    "ARTICLE_URL_BLOCKED",
    "ARTICLE_REDIRECT_BLOCKED",
    "ARTICLE_ROBOTS_DENIED",
    "ARTICLE_FETCH_TIMEOUT",
    "ARTICLE_FETCH_FAILED",
    "ARTICLE_TOO_LARGE",
    "ARTICLE_CONTENT_TYPE_UNSUPPORTED",
    "ARTICLE_NO_USABLE_TEXT",
]


class HackerNewsStoryView(BaseModel):
    source_id: str
    story_id: int
    feed: str
    feed_rank: int
    title: str
    author: str | None
    published_at: datetime
    score: int
    comments: int
    requested_url: HttpUrl | None
    discussion_url: HttpUrl
    content_kind: Literal["external", "self_post"]
    content_basis: Literal["metadata", "article", "hn_self_post"] = "metadata"
    final_url: HttpUrl | None = None
    article_title: str | None = None
    content_characters: int = 0
    content_preview: str | None = None
    fetch_status: HackerNewsFetchStatus = "not_requested"


class HackerNewsListResult(CommandResult):
    stories: list[HackerNewsStoryView]
    skipped: int


class HackerNewsInspectResult(CommandResult):
    story: HackerNewsStoryView


class HackerNewsSyncResult(CommandResult):
    discovered: int
    existing: int
    skipped: int
    fetched: int = 0
    failed: int = 0
