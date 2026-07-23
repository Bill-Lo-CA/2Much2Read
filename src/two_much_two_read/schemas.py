from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator


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


class DigestItem(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    category: Literal[
        "AI_MODEL",
        "AI_RESEARCH",
        "AI_ENGINEERING",
        "DEV_TOOL",
        "SECURITY",
        "BUSINESS",
        "OTHER",
    ]
    summary_zh_tw: str = Field(min_length=1)
    why_it_matters_zh_tw: str = Field(min_length=1)
    source_url: HttpUrl | None = None
    importance: int = Field(ge=1, le=10)
    confidence: float = Field(ge=0, le=1)
    tags: list[str] = Field(default_factory=list)

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, values: list[str]) -> list[str]:
        return ["-".join(value.lower().strip().split()) for value in values if value.strip()]


class EmailExtraction(BaseModel):
    source_id: str
    newsletter_title: str
    newsletter_date: date | None
    overview_zh_tw: str
    items: list[DigestItem]
    truncated_input: bool = False
