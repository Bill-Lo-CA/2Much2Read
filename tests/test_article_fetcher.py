from __future__ import annotations

import socket
from threading import Event

import pytest

import two_much_two_read.article_fetcher as article_fetcher
from two_much_two_read.article_fetcher import (
    ARTICLE_FETCH_DEADLINE_SECONDS,
    MAX_RESPONSE_BYTES,
    ROBOTS_DEADLINE_SECONDS,
    URL_RESOLUTION_DEADLINE_SECONDS,
    ArticleFetcher,
    ArticleFetchError,
    ArticleResponse,
    UrlResolutionError,
    ValidatedURL,
)


def public_dns(_: str) -> list[str]:
    return ["93.184.216.34"]


def article_body() -> bytes:
    return ("<article><p>Useful article content. </p>" * 40 + "</article>").encode()


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_article_fetch_deadline_expires_before_first_request() -> None:
    calls = 0

    def clock() -> float:
        nonlocal calls
        calls += 1
        return 0 if calls == 1 else ARTICLE_FETCH_DEADLINE_SECONDS

    fetcher = ArticleFetcher(public_dns, lambda _: pytest.fail("request should not be sent"), clock=clock)

    with pytest.raises(ArticleFetchError, match="ARTICLE_FETCH_DEADLINE_EXCEEDED"):
        fetcher.fetch("https://example.com/article")


def test_article_dns_resolution_respects_fetch_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    release = Event()

    def stalled_getaddrinfo(*_: object, **__: object) -> list[object]:
        release.wait()
        return []

    monkeypatch.setattr(socket, "getaddrinfo", stalled_getaddrinfo)
    monkeypatch.setattr(article_fetcher, "ARTICLE_FETCH_DEADLINE_SECONDS", 0.01)
    try:
        with pytest.raises(ArticleFetchError, match="ARTICLE_FETCH_DEADLINE_EXCEEDED"):
            ArticleFetcher(response_provider=lambda _: pytest.fail("request should not be sent")).fetch(
                "https://example.com/article"
            )
    finally:
        release.set()


def test_blocks_unsafe_urls_before_request() -> None:
    fetcher = ArticleFetcher(public_dns, lambda _: pytest.fail("request should not be sent"))

    for url in ("http://127.0.0.1", "https://user:pass@example.com", "https://example.com:8080"):
        with pytest.raises(ArticleFetchError, match="ARTICLE_URL_BLOCKED"):
            fetcher.fetch(url)


def test_pins_validated_addresses_for_robots_redirects_and_article_requests() -> None:
    requests: list[ValidatedURL] = []

    def response_provider(request: ValidatedURL) -> ArticleResponse:
        requests.append(request)
        if request.target == "/robots.txt":
            return ArticleResponse(200, {"content-type": "text/plain"}, b"User-agent: *\nAllow: /")
        if request.target == "/start":
            return ArticleResponse(302, {"location": "https://other.example/article"}, b"")
        return ArticleResponse(200, {"content-type": "text/html"}, article_body())

    fetched = ArticleFetcher(public_dns, response_provider).fetch("https://example.com/start")

    assert fetched.requested_url == "https://example.com/start"
    assert fetched.final_url == "https://other.example/article"
    assert [(request.url, request.address) for request in requests] == [
        ("https://example.com/robots.txt", "93.184.216.34"),
        ("https://example.com/start", "93.184.216.34"),
        ("https://other.example/robots.txt", "93.184.216.34"),
        ("https://other.example/article", "93.184.216.34"),
    ]


def test_returns_typed_errors_for_redirects_robots_and_response_limits() -> None:
    def response_provider(request: ValidatedURL) -> ArticleResponse:
        if request.target == "/robots.txt":
            if request.hostname == "denied.example":
                return ArticleResponse(200, {}, b"User-agent: *\nDisallow: /")
            return ArticleResponse(200, {}, b"not a robots file")
        if request.hostname == "redirect.example":
            return ArticleResponse(302, {"location": "http://127.0.0.1/"}, b"")
        if request.hostname == "large.example":
            return ArticleResponse(200, {"content-type": "text/html", "content-length": str(MAX_RESPONSE_BYTES + 1)}, b"")
        return ArticleResponse(200, {"content-type": "application/pdf"}, b"pdf")

    fetcher = ArticleFetcher(public_dns, response_provider)
    for url, code in (
        ("https://redirect.example/article", "ARTICLE_REDIRECT_BLOCKED"),
        ("https://denied.example/article", "ARTICLE_ROBOTS_DENIED"),
        ("https://large.example/article", "ARTICLE_TOO_LARGE"),
        ("https://type.example/article", "ARTICLE_CONTENT_TYPE_UNSUPPORTED"),
    ):
        with pytest.raises(ArticleFetchError, match=code):
            fetcher.fetch(url)


def test_resolves_redirects_and_same_host_canonical_metadata() -> None:
    def response_provider(request: ValidatedURL) -> ArticleResponse:
        if request.target == "/start":
            return ArticleResponse(302, {"location": "https://example.com/article"}, b"")
        return ArticleResponse(
            200,
            {"content-type": "text/html"},
            b'<html><link rel="canonical" href="/canonical?utm_source=newsletter"></html>',
        )

    resolved = ArticleFetcher(public_dns, response_provider).resolve_url("https://example.com/start")

    assert resolved.final_url == "https://example.com/article"
    assert resolved.canonical_url == "https://example.com/canonical?utm_source=newsletter"


def test_rejects_unsafe_or_cross_host_canonical_metadata() -> None:
    def response_provider(_: ValidatedURL) -> ArticleResponse:
        return ArticleResponse(
            200,
            {"content-type": "text/html"},
            b'<html><meta property="og:url" content="http://127.0.0.1/private"></html>',
        )

    resolved = ArticleFetcher(public_dns, response_provider).resolve_url("https://example.com/article")

    assert resolved.final_url == "https://example.com/article"
    assert resolved.canonical_url is None


def test_rejects_canonical_metadata_that_downgrades_https() -> None:
    def response_provider(_: ValidatedURL) -> ArticleResponse:
        return ArticleResponse(
            200,
            {"content-type": "text/html"},
            b'<html><link rel="canonical" href="http://example.com/canonical"></html>',
        )

    resolved = ArticleFetcher(public_dns, response_provider).resolve_url("https://example.com/article")

    assert resolved.final_url == "https://example.com/article"
    assert resolved.canonical_url is None


def test_article_fetch_deadline_expires_during_body_operation() -> None:
    clock = FakeClock()

    def response_provider(request: ValidatedURL) -> ArticleResponse:
        if request.target == "/robots.txt":
            return ArticleResponse(404, {}, b"")
        clock.advance(ARTICLE_FETCH_DEADLINE_SECONDS)
        return ArticleResponse(200, {"content-type": "text/html"}, article_body())

    with pytest.raises(ArticleFetchError, match="ARTICLE_FETCH_DEADLINE_EXCEEDED"):
        ArticleFetcher(public_dns, response_provider, clock=clock).fetch("https://example.com/article")


def test_article_fetch_deadline_expires_after_robots() -> None:
    clock = FakeClock()

    def response_provider(request: ValidatedURL) -> ArticleResponse:
        if request.target != "/robots.txt":
            pytest.fail("article request should not be sent")
        clock.advance(ARTICLE_FETCH_DEADLINE_SECONDS)
        return ArticleResponse(404, {}, b"")

    with pytest.raises(ArticleFetchError, match="ARTICLE_FETCH_DEADLINE_EXCEEDED"):
        ArticleFetcher(public_dns, response_provider, clock=clock).fetch("https://example.com/article")


def test_expired_robots_subdeadline_remains_permissive() -> None:
    clock = FakeClock()

    def response_provider(request: ValidatedURL) -> ArticleResponse:
        if request.target == "/robots.txt":
            clock.advance(ROBOTS_DEADLINE_SECONDS + 1)
            return ArticleResponse(503, {}, b"")
        return ArticleResponse(200, {"content-type": "text/html"}, article_body())

    fetched = ArticleFetcher(public_dns, response_provider, clock=clock).fetch("https://example.com/article")

    assert fetched.body == article_body()


def test_explicit_robots_deny_survives_subdeadline() -> None:
    clock = FakeClock()

    def response_provider(request: ValidatedURL) -> ArticleResponse:
        if request.target == "/robots.txt":
            clock.advance(ROBOTS_DEADLINE_SECONDS + 1)
            return ArticleResponse(200, {}, b"User-agent: *\nDisallow: /")
        return ArticleResponse(200, {"content-type": "text/html"}, article_body())

    with pytest.raises(ArticleFetchError, match="ARTICLE_ROBOTS_DENIED"):
        ArticleFetcher(public_dns, response_provider, clock=clock).fetch("https://example.com/article")


def test_article_fetch_deadline_expires_during_redirect_validation() -> None:
    clock = FakeClock()

    def resolver(hostname: str) -> list[str]:
        if hostname == "other.example":
            clock.advance(ARTICLE_FETCH_DEADLINE_SECONDS)
        return public_dns(hostname)

    def response_provider(request: ValidatedURL) -> ArticleResponse:
        if request.target == "/robots.txt":
            return ArticleResponse(404, {}, b"")
        return ArticleResponse(302, {"location": "https://other.example/article"}, b"")

    with pytest.raises(ArticleFetchError, match="ARTICLE_FETCH_DEADLINE_EXCEEDED"):
        ArticleFetcher(resolver, response_provider, clock=clock).fetch("https://example.com/start")


def test_metadata_resolution_deadline_maps_internal_article_deadline() -> None:
    clock = FakeClock()

    def response_provider(_: ValidatedURL) -> ArticleResponse:
        clock.advance(URL_RESOLUTION_DEADLINE_SECONDS)
        return ArticleResponse(200, {"content-type": "text/html"}, b"<html></html>")

    with pytest.raises(UrlResolutionError, match="URL_RESOLUTION_DEADLINE_EXCEEDED"):
        ArticleFetcher(public_dns, response_provider, clock=clock).resolve_url("https://example.com/article")


def test_url_resolution_dns_deadline_maps_to_resolution_error(monkeypatch: pytest.MonkeyPatch) -> None:
    release = Event()

    def stalled_getaddrinfo(*_: object, **__: object) -> list[object]:
        release.wait()
        return []

    monkeypatch.setattr(socket, "getaddrinfo", stalled_getaddrinfo)
    monkeypatch.setattr(article_fetcher, "URL_RESOLUTION_DEADLINE_SECONDS", 0.01)
    try:
        with pytest.raises(UrlResolutionError, match="URL_RESOLUTION_DEADLINE_EXCEEDED"):
            ArticleFetcher().resolve_url("https://example.com/article")
    finally:
        release.set()


HUBSPOT_CLICK = "https://info.example.io/e3t/Ctc/L2+113/d5qLSh04/VXfV_Z8Npr8qW5"
# The real hop carries a query HubSpot needs; cut before it, the hop answers with the click page again.
HUBSPOT_NEXT = "https://info.example.io/events/public/v1/encoded/track/tc/L2+113/d5qLSh04/VXfV_Z8N?_ud=3595975d&x=1"


def _hubspot_page(next_hop: str) -> ArticleResponse:
    # The shape of the real click page: bot checks in script, and the next hop in an href, where an
    # ampersand is written as an entity.
    href = next_hop.replace("&", "&amp;")
    body = f'<html><script>function isWebDriver() {{}}</script><a href="{href}">continue</a></html>'
    return ArticleResponse(200, {"content-type": "text/html;charset=utf-8"}, body.encode())


def test_a_hubspot_click_page_is_followed_to_the_article() -> None:
    # HubSpot answers its click links with a page that redirects by script, which is where the
    # resolver used to stop: 17 of one run's items had the tracking page and so no article at all.
    def response_provider(request: ValidatedURL) -> ArticleResponse:
        if request.url == HUBSPOT_CLICK:
            return _hubspot_page(HUBSPOT_NEXT)
        if request.url == HUBSPOT_NEXT:
            return ArticleResponse(307, {"location": "https://thenewstack.io/story?_hsenc=p2ANqtz-x&ecid=AC1"}, b"")
        return ArticleResponse(200, {"content-type": "text/html"}, b'<link rel="canonical" href="/story">')

    resolved = ArticleFetcher(public_dns, response_provider).resolve_url(HUBSPOT_CLICK)

    assert resolved.final_url == "https://thenewstack.io/story?_hsenc=p2ANqtz-x&ecid=AC1"
    assert resolved.canonical_url == "https://thenewstack.io/story"


@pytest.mark.parametrize(
    "page",
    [
        # Another host named on the click page is not HubSpot's own next hop, nor is a downgrade.
        _hubspot_page("https://evil.example/events/public/v1/encoded/track/tc/L2+113/x"),
        _hubspot_page("http://info.example.io/events/public/v1/encoded/track/tc/L2+113/x"),
        # Script is never run, so a page that only redirects by script is the destination.
        ArticleResponse(200, {"content-type": "text/html"}, b'<script>location="https://evil.example/"</script>'),
    ],
    ids=["other-host", "downgrade", "script-only"],
)
def test_a_page_hop_is_taken_only_where_it_is_declared(page: ArticleResponse) -> None:
    requested: list[str] = []

    def response_provider(request: ValidatedURL) -> ArticleResponse:
        requested.append(request.url)
        return page

    resolved = ArticleFetcher(public_dns, response_provider).resolve_url(HUBSPOT_CLICK)

    assert resolved.final_url == HUBSPOT_CLICK
    assert requested == [HUBSPOT_CLICK]


def test_a_meta_refresh_is_a_redirect_and_is_validated_like_one() -> None:
    def response_provider(request: ValidatedURL) -> ArticleResponse:
        refresh = {
            "/soon": b'<meta http-equiv="Refresh" content="0; url=https://example.com/article">',
            "/private": b'<meta http-equiv="refresh" content="0;url=http://127.0.0.1/admin">',
            "/itself": b'<meta http-equiv="refresh" content="5; url=/itself"><link rel="canonical" href="/itself">',
            "/reloads": b'<meta http-equiv="refresh" content="300; url=https://example.com/elsewhere">',
        }.get(request.target, b"<html></html>")
        return ArticleResponse(200, {"content-type": "text/html"}, refresh)

    fetcher = ArticleFetcher(public_dns, response_provider)

    assert fetcher.resolve_url("https://example.com/soon").final_url == "https://example.com/article"
    with pytest.raises(UrlResolutionError, match="URL_REDIRECT_BLOCKED"):
        fetcher.resolve_url("https://example.com/private")
    assert fetcher.resolve_url("https://example.com/itself").canonical_url == "https://example.com/itself"
    assert fetcher.resolve_url("https://example.com/reloads").final_url == "https://example.com/reloads"


@pytest.mark.parametrize("status", [401, 403, 429])
def test_a_destination_that_turns_crawlers_away_is_still_the_link(status: int) -> None:
    # openai.com, Dark Reading and Medium answer 403 and the WSJ 401: the redirect chain has already
    # reached the article, which opens in a reader's browser.
    def response_provider(request: ValidatedURL) -> ArticleResponse:
        if request.target == "/c":
            return ArticleResponse(302, {"location": "https://news.example/story"}, b"")
        return ArticleResponse(status, {"content-type": "text/html"}, b"denied")

    resolved = ArticleFetcher(public_dns, response_provider).resolve_url("https://links.example/c")

    assert (resolved.final_url, resolved.canonical_url) == ("https://news.example/story", None)


@pytest.mark.parametrize(("status", "code"), [(404, "URL_RESOLUTION_FAILED"), (503, "URL_RESOLUTION_FAILED")])
def test_a_missing_or_failing_destination_is_no_link(status: int, code: str) -> None:
    def response_provider(_: ValidatedURL) -> ArticleResponse:
        return ArticleResponse(status, {"content-type": "text/html"}, b"")

    with pytest.raises(UrlResolutionError, match=code):
        ArticleFetcher(public_dns, response_provider).resolve_url("https://news.example/story")


def test_a_page_too_large_to_read_whole_is_named_from_its_head() -> None:
    # claude.com's Marketplace post is 556 KB; the canonical link is in the first few hundred bytes.
    head = b'<html><head><link rel="canonical" href="https://claude.com/blog/claude-marketplace"></head><body>'
    body = head + b"x" * (article_fetcher.MAX_METADATA_BYTES * 2)

    def response_provider(_: ValidatedURL) -> ArticleResponse:
        return ArticleResponse(200, {"content-type": "text/html", "content-length": str(len(body))}, body)

    resolved = ArticleFetcher(public_dns, response_provider).resolve_url("https://claude.com/blog/claude-marketplace")

    assert resolved.canonical_url == "https://claude.com/blog/claude-marketplace"


@pytest.mark.parametrize(
    ("content_type", "linked"),
    [("application/pdf", True), ("application/octet-stream", False), ("application/zip", False)],
)
def test_only_a_page_or_a_pdf_may_be_the_link(content_type: str, linked: bool) -> None:
    # A paper is worth linking; a download of anything else is never put in front of a reader.
    def response_provider(_: ValidatedURL) -> ArticleResponse:
        return ArticleResponse(200, {"content-type": content_type}, b"bytes")

    fetcher = ArticleFetcher(public_dns, response_provider)
    if linked:
        assert fetcher.resolve_url("https://arxiv.example/paper").final_url == "https://arxiv.example/paper"
    else:
        with pytest.raises(UrlResolutionError, match="URL_CONTENT_TYPE_UNSUPPORTED"):
            fetcher.resolve_url("https://files.example/setup")


def test_an_overlong_url_is_refused_before_any_request() -> None:
    fetcher = ArticleFetcher(public_dns, lambda _: pytest.fail("request should not be sent"))

    with pytest.raises(UrlResolutionError, match="URL_POLICY_BLOCKED"):
        fetcher.resolve_url("https://example.com/" + "a" * article_fetcher.MAX_URL_LENGTH)
