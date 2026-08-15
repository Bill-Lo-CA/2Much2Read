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
    return LinkCandidate(candidate_id=candidate_id, anchor_text=text, raw_url=url, position=position, kind="article")


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
        raw_url="https://example.com/heading",
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


def test_does_not_display_an_unresolved_tracking_url() -> None:
    enricher = UrlEnricher()
    match = enricher.match([analysis()], [candidate("link-0001", "Useful article", "https://info.example/e3t/token")])[0]

    item = enricher.resolved_item(match, ResolvedUrl("https://info.example/e3t/token", "https://info.example/e3t/token", None))

    assert item.source_url is None
