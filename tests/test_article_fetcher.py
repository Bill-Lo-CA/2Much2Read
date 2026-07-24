from __future__ import annotations

import httpx
import pytest

from two_much_two_read.article_fetcher import MAX_RESPONSE_BYTES, ArticleFetcher, ArticleFetchError


def public_dns(_: str) -> list[str]:
    return ["93.184.216.34"]


def article_body() -> bytes:
    return ("<article><p>Useful article content. </p>" * 40 + "</article>").encode()


def test_blocks_unsafe_urls_before_request() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("request should not be sent")))
    fetcher = ArticleFetcher(client, public_dns)
    try:
        for url in ("http://127.0.0.1", "https://user:pass@example.com", "https://example.com:8080"):
            with pytest.raises(ArticleFetchError, match="ARTICLE_URL_BLOCKED"):
                fetcher.fetch(url)
    finally:
        client.close()


def test_validates_redirects_and_robots_before_fetching_article() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, headers={"content-type": "text/plain"}, text="User-agent: *\nAllow: /")
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "https://other.example/article"})
        return httpx.Response(200, headers={"content-type": "text/html"}, content=article_body())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetcher = ArticleFetcher(client, public_dns)
    try:
        fetched = fetcher.fetch("https://example.com/start")
    finally:
        client.close()

    assert fetched.requested_url == "https://example.com/start"
    assert fetched.final_url == "https://other.example/article"
    assert requests == [
        "https://example.com/robots.txt",
        "https://example.com/start",
        "https://other.example/robots.txt",
        "https://other.example/article",
    ]


def test_returns_typed_errors_for_redirects_robots_and_response_limits() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            if request.url.host == "denied.example":
                return httpx.Response(200, text="User-agent: *\nDisallow: /")
            return httpx.Response(200, text="not a robots file")
        if request.url.host == "redirect.example":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/"})
        if request.url.host == "large.example":
            return httpx.Response(200, headers={"content-type": "text/html", "content-length": str(MAX_RESPONSE_BYTES + 1)})
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"pdf")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetcher = ArticleFetcher(client, public_dns)
    try:
        for url, code in (
            ("https://redirect.example/article", "ARTICLE_REDIRECT_BLOCKED"),
            ("https://denied.example/article", "ARTICLE_ROBOTS_DENIED"),
            ("https://large.example/article", "ARTICLE_TOO_LARGE"),
            ("https://type.example/article", "ARTICLE_CONTENT_TYPE_UNSUPPORTED"),
        ):
            with pytest.raises(ArticleFetchError, match=code):
                fetcher.fetch(url)
    finally:
        client.close()
