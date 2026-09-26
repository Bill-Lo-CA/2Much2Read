import pytest
from pydantic import HttpUrl

from two_much_two_read.article_fetcher import ResolvedUrl
from two_much_two_read.schemas import LinkCandidate, NewsletterItemAnalysis
from two_much_two_read.url_enrichment import UrlEnricher


def analysis(title: str = "Useful article", source_title: str | None = None) -> NewsletterItemAnalysis:
    return NewsletterItemAnalysis(
        title=title,
        source_title=source_title if source_title is not None else title,
        category="OTHER",
        summary_zh_tw="摘要",
        why_it_matters_zh_tw="原因",
        importance=5,
        confidence=0.8,
    )


def candidate(candidate_id: str, text: str, url: str, position: int = 0) -> LinkCandidate:
    return LinkCandidate(candidate_id=candidate_id, anchor_text=text, raw_url=HttpUrl(url), position=position, kind="article")


def test_matches_exact_anchor_and_uses_application_owned_resolution() -> None:
    enricher = UrlEnricher()
    match = enricher.match([analysis()], [candidate("link-0001", "Useful article", "https://short.example/go")])[0]

    item = enricher.resolved_item(match, ResolvedUrl("https://short.example/go", "https://example.com/article", None))

    assert match.method == "exact_anchor"
    assert str(item.source_url) == "https://example.com/article"
    assert str(item.raw_url) == "https://short.example/go"
    assert item.url_match_status == "matched"
    assert item.url_resolution_status == "resolved"


def test_keeps_ambiguous_or_failed_items_without_display_url() -> None:
    enricher = UrlEnricher()
    ambiguous = enricher.match(
        [analysis()],
        [
            candidate("link-0001", "Useful article", "https://one.example/article", 0),
            candidate("link-0002", "Useful article", "https://two.example/article", 1),
        ],
    )[0]
    failed = enricher.failed_item(
        enricher.match([analysis()], [candidate("link-0001", "Useful article", "https://example.com/article")])[0],
        "URL_RESOLUTION_DEADLINE_EXCEEDED",
    )

    assert ambiguous.method == "ambiguous"
    assert failed.source_url is None
    assert failed.url_resolution_status == "failed"


def test_exact_anchor_wins_at_the_ambiguity_threshold() -> None:
    exact = candidate("link-0001", "Useful article", "https://example.com/exact")
    heading = LinkCandidate(
        candidate_id="link-0002",
        anchor_text="Read more",
        nearby_text="Useful article",
        raw_url=HttpUrl("https://example.com/heading"),
        position=1,
        kind="article",
    )

    match = UrlEnricher().match([analysis()], [exact, heading])[0]

    assert match.method == "exact_anchor"
    assert match.candidate == exact


def test_does_not_assign_a_longer_anchor_to_a_shorter_title() -> None:
    link = candidate("link-0001", "Python release", "https://example.com/story")

    matches = UrlEnricher().match([analysis("Python"), analysis("Python release")], [link])

    assert [match.candidate for match in matches] == [None, link]
    assert [match.method for match in matches] == ["unmatched", "exact_anchor"]


def test_a_translated_title_still_matches_through_the_verbatim_headline() -> None:
    """The digest title is translated, so only the newsletter's own wording can match the anchor."""
    item = analysis(title="有用的文章", source_title="Useful article")

    match = UrlEnricher().match([item], [candidate("link-0001", "Useful article", "https://example.com/article")])[0]

    assert (match.method, match.confidence) == ("exact_anchor", 1.0)


def test_a_translated_title_alone_matches_nothing() -> None:
    """Why the verbatim headline exists: a translated title shares no tokens with the anchor."""
    item = analysis(title="有用的文章", source_title="有用的文章")

    match = UrlEnricher().match([item], [candidate("link-0001", "Useful article", "https://example.com/article")])[0]

    assert match.method == "unmatched"


@pytest.mark.parametrize(
    "final", ["https://info.example/e3t/token", "https://info.example/events/public/v1/encoded/track/tc/L2+113/x?_ud=1"]
)
def test_does_not_display_an_unresolved_tracking_url(final: str) -> None:
    enricher = UrlEnricher()
    match = enricher.match([analysis()], [candidate("link-0001", "Useful article", "https://info.example/e3t/token")])[0]

    item = enricher.resolved_item(match, ResolvedUrl("https://info.example/e3t/token", final, None))

    assert item.source_url is None


def test_the_link_shown_carries_no_subscriber_identity() -> None:
    # A click tracker hands the destination the subscriber's own ids; shown as it arrived, the link
    # would tell anyone who opens it who received the email.
    enricher = UrlEnricher()
    match = enricher.match([analysis()], [candidate("link-0001", "Useful article", "https://info.example/e3t/token")])[0]
    final = "https://thenewstack.io/story?_hsenc=p2ANqtz&_hsmi=2&ecid=AC1&utm_source=x&page=2&fbclid=F"

    item = enricher.resolved_item(match, ResolvedUrl("https://info.example/e3t/token", final, None))

    assert str(item.source_url) == "https://thenewstack.io/story?page=2"


def test_a_link_too_long_to_store_is_left_out_rather_than_failing_the_email() -> None:
    enricher = UrlEnricher()
    match = enricher.match([analysis()], [candidate("link-0001", "Useful article", "https://short.example/go")])[0]
    # Inside the fetcher's 2083 characters as it arrived; re-encoded without its tracking tags, each
    # apostrophe becomes %27 and the URL no longer fits.
    final = "https://example.com/a?utm_source=x&q=" + "'" * 1000

    item = enricher.resolved_item(match, ResolvedUrl("https://short.example/go", final, None))

    assert item.source_url is None


def coded(title: str, link: str | None) -> NewsletterItemAnalysis:
    return NewsletterItemAnalysis.model_validate({**analysis(title).model_dump(), "source_title": title, "link": link})


def test_the_extractors_link_code_settles_an_article_and_its_comments() -> None:
    # A link-list newsletter puts the story and its comments beside the same headline, so the title
    # scores both alike and the match was abandoned as ambiguous: 10 of 10 on Hacker Newsletter.
    article = candidate("link-0001", "", "https://example.com/grok", 0).model_copy(update={"nearby_text": "Grok 4.7"})
    comments = candidate("link-0002", "", "https://news.example/item?id=1", 1).model_copy(update={"nearby_text": "Grok 4.7"})

    assert UrlEnricher().match([analysis("Grok 4.7")], [article, comments])[0].method == "ambiguous"
    match = UrlEnricher().match([coded("Grok 4.7", "L1")], [article, comments])[0]

    assert match.method == "model_link"
    assert match.candidate is article


def test_a_code_that_names_no_usable_link_falls_back_to_the_title() -> None:
    story = candidate("link-0001", "Useful article", "https://example.com/story", 0)
    footer = LinkCandidate(
        candidate_id="link-0002",
        anchor_text="About us",
        raw_url=HttpUrl("https://example.com/about"),
        position=1,
        kind="non_article",
    )

    for link in ("L9", "L2", None):
        match = UrlEnricher().match([coded("Useful article", link)], [story, footer])[0]
        assert (match.method, match.candidate) == ("exact_anchor", story)


def test_a_code_already_taken_falls_back_to_the_title() -> None:
    first = candidate("link-0001", "First story", "https://example.com/first", 0)
    second = candidate("link-0002", "Second story", "https://example.com/second", 1)

    matches = UrlEnricher().match([coded("First story", "L1"), coded("Second story", "L1")], [first, second])

    assert [(match.method, match.candidate) for match in matches] == [("model_link", first), ("exact_anchor", second)]
