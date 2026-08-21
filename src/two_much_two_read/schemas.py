from __future__ import annotations

import re
from datetime import date, datetime
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, TypeAdapter, field_validator

HTTP_URL = TypeAdapter(HttpUrl)
MODEL_TEXT_INJECTION = re.compile(r"https?://|\[[^\]\r\n]*\]\([^)]*\)", re.IGNORECASE)

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
    model_owned_text: ClassVar[bool] = True

    title: str = Field(min_length=1, max_length=200, description="Headline translated into the digest language")
    category: DigestCategory
    summary_zh_tw: str = Field(min_length=1, max_length=800)
    why_it_matters_zh_tw: str = Field(min_length=1, max_length=800)
    importance: int = Field(ge=1, le=10)
    confidence: float = Field(ge=0, le=1)
    tags: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("title", "summary_zh_tw", "why_it_matters_zh_tw")
    @classmethod
    def reject_model_links(cls, value: str) -> str:
        if cls.model_owned_text and MODEL_TEXT_INJECTION.search(value):
            raise ValueError("model-owned text must not contain URLs or Markdown links")
        return value

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            if not value.strip():
                continue
            if cls.model_owned_text and MODEL_TEXT_INJECTION.search(value):
                raise ValueError("model-owned text must not contain URLs or Markdown links")
            tag = "-".join(value.lower().strip().split())
            if len(tag) > 40:
                raise ValueError("normalized tags must be at most 40 characters")
            normalized.append(tag)
        return normalized


class NewsletterItemAnalysis(ItemAnalysis):
    # The headline exactly as the newsletter wrote it. `title` is translated into the digest
    # language, which leaves it with no tokens in common with the English anchor text and URL slugs
    # the link matcher scores against: of 170 extracted items, every one of the 107 with a
    # translated title went unmatched while 84% of the untranslated ones matched. This field exists
    # only to give the matcher the original wording, so it is deliberately absent from ItemAnalysis
    # and never reaches DigestItem, storage, or the rendered digest. It is copied out of untrusted
    # newsletter text, so it carries no anti-link validator; nothing renders it.
    source_title: str = Field(
        min_length=1, max_length=200, description="Headline copied verbatim from the newsletter, never translated"
    )


class DigestItem(ItemAnalysis):
    model_owned_text: ClassVar[bool] = False

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


class StoryIdentity(BaseModel):
    """Whether two digest items describe the same event.

    Token overlap can only ask whether two items share vocabulary, and six rounds of filtering it
    never reached the question that matters. "Claude Code sessions can now talk to each other" and
    "A Claude Code skill was eating 200,000 tokens" share two proper nouns and are unrelated; no
    rule over token shape or frequency separates those from a real duplicate, because the
    difference is semantic. A model is asked instead, and it answers with one boolean.

    One boolean, not a score with a threshold: swapping which item is presented first moves a
    0-100 score by up to 35 points, which is wider than the 20-point gap between the true and false
    pairs it would have to separate, while the same swap never flips the boolean. The README records
    the measurement.
    """

    model_config = ConfigDict(extra="forbid")

    same_story: bool = Field(description="True only if both items report the same specific event")


class ItemDeepening(BaseModel):
    """A headline item rewritten from fuller source text than the extractor ever saw.

    The extractor splits one email into up to ten items, so each gets a few lines of input and
    returns a summary around 60 characters. A headline deserves more, and the field bounds have
    always allowed it, so this rewrite runs over the article body or the merged newsletter coverage.
    """

    model_config = ConfigDict(extra="forbid")

    # Decided before the rewrite is written, so the model commits to the judgement first. A link can
    # be wrong - mismatched by the URL matcher or borrowed from a merge - and rewriting from the
    # wrong page turns a wrong link into a headline whose body describes a different story.
    covers_the_item: bool = Field(description="True only if the source text is about this item's own headline")
    summary_zh_tw: str = Field(min_length=1, max_length=800, description="What happened, in the digest language")
    why_it_matters_zh_tw: str = Field(min_length=1, max_length=800, description="Practical significance, in the digest language")

    @field_validator("summary_zh_tw", "why_it_matters_zh_tw")
    @classmethod
    def reject_model_links(cls, value: str) -> str:
        # Written from untrusted article text, so it is held to the same rule as every other
        # model-owned field: the renderer owns links, the model never supplies them.
        if MODEL_TEXT_INJECTION.search(value):
            raise ValueError("model-owned text must not contain URLs or Markdown links")
        return value


class EmailExtraction(BaseModel):
    source_id: str
    newsletter_title: str
    newsletter_date: date | None
    overview_zh_tw: str = Field(max_length=1500)
    items: list[NewsletterItemAnalysis]
    truncated_input: bool = False

    @field_validator("overview_zh_tw")
    @classmethod
    def reject_model_links(cls, value: str) -> str:
        if MODEL_TEXT_INJECTION.search(value):
            raise ValueError("model-owned text must not contain URLs or Markdown links")
        return value
