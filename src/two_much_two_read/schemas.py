from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, TypeAdapter, field_validator

HTTP_URL = TypeAdapter(HttpUrl)

DigestCategory = Literal[
    "AI_MODEL",
    "AI_RESEARCH",
    "AI_ENGINEERING",
    "DEV_TOOL",
    "SECURITY",
    "BUSINESS",
    "OTHER",
]


class SourceDocument(BaseModel):
    source_type: Literal["gmail", "hackernews"]
    source_id: str
    external_id: str
    title: str
    author: str | None = None
    published_at: datetime
    source_url: HttpUrl | None = None
    discussion_url: HttpUrl | None = None
    metadata: dict[str, object] = Field(default_factory=dict)


class ResolvedContent(BaseModel):
    document: SourceDocument
    text: str
    basis: Literal["newsletter", "article", "hn_self_post", "metadata"]
    final_url: HttpUrl | None = None
    truncated: bool


class LinkCandidate(BaseModel):
    candidate_id: str = Field(pattern=r"link-\d{4}")
    raw_url: HttpUrl
    anchor_text: str = ""
    nearby_text: str = ""
    position: int = Field(ge=0)
    kind: Literal["article", "non_article", "unknown"] = "unknown"


class ExtractedEmailContent(BaseModel):
    analysis_text: str = Field(min_length=1)
    original_characters: int | None = None
    link_candidates: list[LinkCandidate] = Field(default_factory=list)


class ItemAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    category: DigestCategory
    summary_zh_tw: str = Field(min_length=1)
    why_it_matters_zh_tw: str = Field(min_length=1)
    importance: int = Field(ge=1, le=10)
    confidence: float = Field(ge=0, le=1)
    tags: list[str] = Field(default_factory=list)

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, values: list[str]) -> list[str]:
        return ["-".join(value.lower().strip().split()) for value in values if value.strip()]


class NewsletterItemAnalysis(ItemAnalysis):
    pass


class DigestItem(ItemAnalysis):
    source_url: HttpUrl | None = None
    raw_url: HttpUrl | None = None
    resolved_url: HttpUrl | None = None
    canonical_url: HttpUrl | None = None
    url_match_status: Literal["not_applicable", "pending", "matched", "unmatched", "ambiguous"] = "not_applicable"
    url_match_method: Literal["exact_anchor", "heading_context", "fuzzy_anchor", "url_slug"] | None = None
    url_match_confidence: float | None = Field(default=None, ge=0, le=1)
    url_resolution_status: Literal["not_applicable", "not_requested", "resolved", "failed", "blocked"] = "not_applicable"
    url_error_code: str | None = None
    url_checked_at: datetime | None = None


class DigestReviewSelection(BaseModel):
    candidate_id: int = Field(gt=0)
    score: int = Field(ge=0, le=100)
    reason_zh_tw: str = Field(min_length=1, max_length=300)


class DigestReview(BaseModel):
    selected: list[DigestReviewSelection]


class ArticleAnalysis(ItemAnalysis):
    pass


class EmailExtraction(BaseModel):
    source_id: str
    newsletter_title: str
    newsletter_date: date | None
    overview_zh_tw: str
    items: list[NewsletterItemAnalysis]
    truncated_input: bool = False
