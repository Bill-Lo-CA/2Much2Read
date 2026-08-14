import pytest
from pydantic import ValidationError

from two_much_two_read.schemas import DigestItem, EmailExtraction, NewsletterItemAnalysis


def item_values(**updates: object) -> dict[str, object]:
    return {
        "title": "Model release",
        "category": "AI_MODEL",
        "summary_zh_tw": "摘要",
        "why_it_matters_zh_tw": "重要原因",
        "importance": 8,
        "confidence": 0.9,
        "tags": ["AI Model"],
        **updates,
    }


def analysis_values(**updates: object) -> dict[str, object]:
    """source_title lives on the extraction schema only; DigestItem forbids it."""
    return item_values(**{"source_title": "Model release", **updates})


def extraction_values(**updates: object) -> dict[str, object]:
    return {
        "source_id": "source",
        "newsletter_title": "Newsletter",
        "newsletter_date": None,
        "overview_zh_tw": "本日摘要",
        "items": [analysis_values()],
        **updates,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", "x" * 201),
        ("summary_zh_tw", "x" * 801),
        ("why_it_matters_zh_tw", "x" * 801),
    ],
)
def test_item_text_bounds(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        DigestItem.model_validate(item_values(**{field: value}))


def test_item_text_and_tag_bounds_accept_the_limits() -> None:
    result = DigestItem.model_validate(
        item_values(
            title="x" * 200,
            summary_zh_tw="x" * 800,
            why_it_matters_zh_tw="x" * 800,
            tags=["a " * 20] * 8,
        )
    )

    assert len(result.title) == 200
    assert len(result.summary_zh_tw) == len(result.why_it_matters_zh_tw) == 800
    assert len(result.tags) == 8 and all(len(tag) == 39 for tag in result.tags)


def test_overview_bound() -> None:
    assert len(EmailExtraction.model_validate(extraction_values(overview_zh_tw="x" * 1500)).overview_zh_tw) == 1500

    with pytest.raises(ValidationError):
        EmailExtraction.model_validate(extraction_values(overview_zh_tw="x" * 1501))


@pytest.mark.parametrize("field", ["title", "summary_zh_tw", "why_it_matters_zh_tw"])
@pytest.mark.parametrize("value", ["Read https://example.com", "Read [this](https://example.com)"])
def test_item_text_rejects_urls_and_markdown_links(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        NewsletterItemAnalysis.model_validate(analysis_values(**{field: value}))


def test_persisted_digest_item_defers_untrusted_links_to_renderer_sanitization() -> None:
    item = DigestItem.model_validate(item_values(title="Read https://example.com"))

    assert item.title == "Read https://example.com"


def test_overview_rejects_urls_and_markdown_links() -> None:
    for value in ("Read https://example.com", "Read [this](https://example.com)"):
        with pytest.raises(ValidationError):
            EmailExtraction.model_validate(extraction_values(overview_zh_tw=value))


def test_tags_are_normalized_and_bounded() -> None:
    result = DigestItem.model_validate(item_values(tags=["AI Model", "a " * 20]))

    assert result.tags == ["ai-model", "a-" * 19 + "a"]

    with pytest.raises(ValidationError):
        DigestItem.model_validate(item_values(tags=["tag"] * 9))
    with pytest.raises(ValidationError):
        DigestItem.model_validate(item_values(tags=["a " * 21]))
    with pytest.raises(ValidationError):
        NewsletterItemAnalysis.model_validate(analysis_values(tags=["[read](https://example.com)"]))


def test_the_matcher_only_field_never_reaches_a_persisted_digest_item() -> None:
    """source_title exists to match a translated item back to its link, and nothing renders it."""
    assert "source_title" in NewsletterItemAnalysis.model_fields
    assert "source_title" not in DigestItem.model_fields

    with pytest.raises(ValidationError):
        DigestItem.model_validate(analysis_values())


def test_a_verbatim_headline_may_contain_a_url_the_display_title_may_not() -> None:
    """It is copied out of untrusted text, so rejecting it would fail the whole email."""
    item = NewsletterItemAnalysis.model_validate(analysis_values(source_title="Read https://example.com"))

    assert item.source_title == "Read https://example.com"
