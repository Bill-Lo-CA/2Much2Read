from __future__ import annotations

import pytest

from two_much_two_read.article_fetcher import (
    MAX_RESPONSE_BYTES,
    ArticleFetcher,
    ArticleFetchError,
    ArticleResponse,
    ValidatedURL,
)


def public_dns(_: str) -> list[str]:
    return ["93.184.216.34"]


def article_body() -> bytes:
    return ("<article><p>Useful article content. </p>" * 40 + "</article>").encode()


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
