from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .config import Source


class CommandResult(BaseModel):
    status: str = "ok"


class NewsletterRunResult(CommandResult):
    status: Literal["ok", "partial", "no_content", "skipped"]
    discovered: int
    processed: int
    failed: int
    delivered: int
    reason: Literal["daily_digest_exists"] | None = None


class NewsletterRetryResult(CommandResult):
    delivered: int
    failed: int
    failed_by_error_code: dict[str, int] = Field(default_factory=dict)


class DeliveryCheckpointResetResult(CommandResult):
    digest_id: int


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
