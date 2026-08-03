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
