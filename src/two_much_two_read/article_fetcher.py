from __future__ import annotations

import ipaddress
import logging
import re
import socket
from collections.abc import Callable
from dataclasses import dataclass
from urllib import robotparser
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

USER_AGENT = "2much2read/0.1"
MAX_REDIRECTS = 5
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_ROBOTS_BYTES = 256 * 1024

logger = logging.getLogger(__name__)


class ArticleFetchError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class FetchedArticle:
    requested_url: str
    final_url: str
    content_type: str
    body: bytes


@dataclass(frozen=True)
class _Response:
    status_code: int
    headers: httpx.Headers
    body: bytes


def _resolve(hostname: str) -> list[str]:
    try:
        return list({str(entry[4][0]) for entry in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)})
    except OSError as error:
        raise ArticleFetchError("ARTICLE_URL_BLOCKED") from error


class ArticleFetcher:
    def __init__(self, client: httpx.Client | None = None, resolver: Callable[[str], list[str]] = _resolve) -> None:
        self._owned_client = client is None
        self.client = client or httpx.Client(
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9"},
            timeout=httpx.Timeout(connect=5, read=15, write=15, pool=15),
            trust_env=False,
        )
        self.resolver = resolver

    def close(self) -> None:
        if self._owned_client:
            self.client.close()

    def fetch(self, requested_url: str) -> FetchedArticle:
        current_url = self._validate_url(requested_url, redirect=False)
        seen_urls = {current_url}
        for redirects in range(MAX_REDIRECTS + 1):
            self._check_robots(current_url)
            response = self._read(current_url, MAX_RESPONSE_BYTES)
            if 300 <= response.status_code < 400:
                location = response.headers.get("location")
                if not location or redirects == MAX_REDIRECTS:
                    raise ArticleFetchError("ARTICLE_REDIRECT_BLOCKED")
                next_url = self._validate_url(urljoin(current_url, location), redirect=True)
                if next_url in seen_urls:
                    raise ArticleFetchError("ARTICLE_REDIRECT_BLOCKED")
                seen_urls.add(next_url)
                current_url = next_url
                continue
            if not 200 <= response.status_code < 300:
                raise ArticleFetchError("ARTICLE_FETCH_FAILED")
            content_type = response.headers.get("content-type", "").split(";", 1)[0].lower().strip()
            if content_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
                raise ArticleFetchError("ARTICLE_CONTENT_TYPE_UNSUPPORTED")
            return FetchedArticle(requested_url, current_url, content_type, response.body)
        raise ArticleFetchError("ARTICLE_REDIRECT_BLOCKED")

    def _validate_url(self, value: str, *, redirect: bool) -> str:
        code = "ARTICLE_REDIRECT_BLOCKED" if redirect else "ARTICLE_URL_BLOCKED"
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError as error:
            raise ArticleFetchError(code) from error
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ArticleFetchError(code)
        if parsed.username is not None or parsed.password is not None:
            raise ArticleFetchError(code)
        if port not in {None, 80, 443}:
            raise ArticleFetchError(code)
        hostname = parsed.hostname.rstrip(".").lower()
        if hostname == "localhost":
            raise ArticleFetchError(code)
        try:
            addresses = [str(ipaddress.ip_address(hostname))]
        except ValueError:
            try:
                addresses = self.resolver(hostname)
            except ArticleFetchError:
                raise
            except Exception as error:
                raise ArticleFetchError(code) from error
        if not addresses:
            raise ArticleFetchError(code)
        try:
            if any(not ipaddress.ip_address(address).is_global for address in addresses):
                raise ArticleFetchError(code)
        except ValueError as error:
            raise ArticleFetchError(code) from error
        netloc = f"[{hostname}]" if ":" in hostname else hostname
        if port is not None:
            netloc = f"{netloc}:{port}"
        return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, ""))

    def _read(self, url: str, limit: int) -> _Response:
        try:
            self.client.cookies.clear()
            with self.client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response:
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > limit:
                    raise ArticleFetchError("ARTICLE_TOO_LARGE")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > limit:
                        raise ArticleFetchError("ARTICLE_TOO_LARGE")
                return _Response(response.status_code, response.headers, bytes(body))
        except ArticleFetchError:
            raise
        except httpx.TimeoutException as error:
            raise ArticleFetchError("ARTICLE_FETCH_TIMEOUT") from error
        except (httpx.HTTPError, ValueError) as error:
            raise ArticleFetchError("ARTICLE_FETCH_FAILED") from error

    def _check_robots(self, article_url: str) -> None:
        parsed = urlsplit(article_url)
        robots_url = urlunsplit((parsed.scheme, parsed.netloc, "/robots.txt", "", ""))
        try:
            response = self._fetch_robots(robots_url)
            if response is None or response.status_code != 200:
                return
            robots_text = response.body.decode("utf-8", errors="replace")
            if not (re.search(r"(?im)^\s*user-agent\s*:", robots_text) and re.search(r"(?im)^\s*disallow\s*:", robots_text)):
                return
            parser = robotparser.RobotFileParser()
            parser.parse(robots_text.splitlines())
            if not parser.can_fetch(USER_AGENT, article_url):
                raise ArticleFetchError("ARTICLE_ROBOTS_DENIED")
        except ArticleFetchError as error:
            if error.code == "ARTICLE_ROBOTS_DENIED":
                raise
            logger.debug("robots check unavailable for %s: %s", article_url, error.code)

    def _fetch_robots(self, robots_url: str) -> _Response | None:
        current_url = self._validate_url(robots_url, redirect=False)
        seen_urls = {current_url}
        for redirects in range(MAX_REDIRECTS + 1):
            response = self._read(current_url, MAX_ROBOTS_BYTES)
            if not 300 <= response.status_code < 400:
                return response
            location = response.headers.get("location")
            if not location or redirects == MAX_REDIRECTS:
                return None
            next_url = self._validate_url(urljoin(current_url, location), redirect=True)
            if next_url in seen_urls:
                return None
            seen_urls.add(next_url)
            current_url = next_url
        return None
