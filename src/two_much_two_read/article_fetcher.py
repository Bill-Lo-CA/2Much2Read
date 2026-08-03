from __future__ import annotations

import http.client
import ipaddress
import logging
import re
import socket
import ssl
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib import robotparser
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

USER_AGENT = "2much2read/0.1"
MAX_REDIRECTS = 5
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_METADATA_BYTES = 512 * 1024
MAX_ROBOTS_BYTES = 256 * 1024
CONNECT_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 15
ARTICLE_FETCH_DEADLINE_SECONDS = 30
URL_RESOLUTION_DEADLINE_SECONDS = 10
ROBOTS_DEADLINE_SECONDS = 2

logger = logging.getLogger(__name__)


class ArticleFetchError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class UrlResolutionError(ValueError):
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
class ResolvedUrl:
    requested_url: str
    final_url: str
    canonical_url: str | None


@dataclass(frozen=True)
class ArticleResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class ValidatedURL:
    url: str
    hostname: str
    host_header: str
    port: int
    address: str
    target: str


ResponseProvider = Callable[[ValidatedURL], ArticleResponse]


def _resolve(hostname: str) -> list[str]:
    try:
        return [str(entry[4][0]) for entry in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)]
    except OSError as error:
        raise ArticleFetchError("ARTICLE_URL_BLOCKED") from error


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, hostname: str, port: int, address: str, timeout: float = CONNECT_TIMEOUT_SECONDS) -> None:
        super().__init__(hostname, port, timeout=timeout)
        self.address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self.address, self.port), self.timeout)
        self.sock.settimeout(READ_TIMEOUT_SECONDS)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, port: int, address: str, timeout: float = CONNECT_TIMEOUT_SECONDS) -> None:
        self.context = ssl.create_default_context()
        super().__init__(hostname, port, timeout=timeout, context=self.context)
        self.address = address

    def connect(self) -> None:
        sock = socket.create_connection((self.address, self.port), self.timeout)
        self.sock = self.context.wrap_socket(sock, server_hostname=self.host)
        self.sock.settimeout(READ_TIMEOUT_SECONDS)


class ArticleFetcher:
    def __init__(
        self,
        resolver: Callable[[str], list[str]] = _resolve,
        response_provider: ResponseProvider | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.resolver = resolver
        self.response_provider = response_provider
        self.clock = clock

    def fetch(self, requested_url: str) -> FetchedArticle:
        deadline = self.clock() + ARTICLE_FETCH_DEADLINE_SECONDS
        self._check_deadline(deadline)
        current = self._validate_url(requested_url, redirect=False, deadline=deadline)
        seen_urls = {current.url}
        for redirects in range(MAX_REDIRECTS + 1):
            self._check_deadline(deadline)
            self._check_robots(current, deadline)
            self._check_deadline(deadline)
            response = self._read(current, MAX_RESPONSE_BYTES, deadline)
            self._check_deadline(deadline)
            if 300 <= response.status_code < 400:
                location = response.headers.get("location")
                if not location or redirects == MAX_REDIRECTS:
                    raise ArticleFetchError("ARTICLE_REDIRECT_BLOCKED")
                next_url = self._validate_url(urljoin(current.url, location), redirect=True, deadline=deadline)
                if next_url.url in seen_urls:
                    raise ArticleFetchError("ARTICLE_REDIRECT_BLOCKED")
                seen_urls.add(next_url.url)
                current = next_url
                continue
            if not 200 <= response.status_code < 300:
                raise ArticleFetchError("ARTICLE_FETCH_FAILED")
            content_type = response.headers.get("content-type", "").split(";", 1)[0].lower().strip()
            if content_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
                raise ArticleFetchError("ARTICLE_CONTENT_TYPE_UNSUPPORTED")
            return FetchedArticle(requested_url, current.url, content_type, response.body)
        raise ArticleFetchError("ARTICLE_REDIRECT_BLOCKED")

    def resolve_url(self, requested_url: str) -> ResolvedUrl:
        deadline = self.clock() + URL_RESOLUTION_DEADLINE_SECONDS
        try:
            self._check_deadline(deadline)
            current = self._validate_url(requested_url, redirect=False, deadline=deadline)
            seen_urls = {current.url}
            for redirects in range(MAX_REDIRECTS + 1):
                self._check_deadline(deadline)
                response = self._read(current, MAX_METADATA_BYTES, deadline)
                self._check_deadline(deadline)
                if 300 <= response.status_code < 400:
                    location = response.headers.get("location")
                    if not location or redirects == MAX_REDIRECTS:
                        raise ArticleFetchError("ARTICLE_REDIRECT_BLOCKED")
                    next_url = self._validate_url(urljoin(current.url, location), redirect=True, deadline=deadline)
                    if next_url.url in seen_urls:
                        raise ArticleFetchError("ARTICLE_REDIRECT_BLOCKED")
                    seen_urls.add(next_url.url)
                    current = next_url
                    continue
                if not 200 <= response.status_code < 300:
                    raise ArticleFetchError("ARTICLE_FETCH_FAILED")
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower().strip()
                if content_type not in {"text/html", "application/xhtml+xml"}:
                    raise ArticleFetchError("ARTICLE_CONTENT_TYPE_UNSUPPORTED")
                canonical_url = self._canonical_url(current, response.body, deadline)
                self._check_deadline(deadline)
                return ResolvedUrl(requested_url, current.url, canonical_url)
        except ArticleFetchError as error:
            raise UrlResolutionError(_url_error_code(error.code)) from None
        raise AssertionError("unreachable")

    def _canonical_url(self, page: ValidatedURL, body: bytes, deadline: float) -> str | None:
        self._check_deadline(deadline)
        soup = BeautifulSoup(body, "lxml")
        self._check_deadline(deadline)
        canonical = soup.find("link", rel=lambda value: value and "canonical" in value)
        value = str(canonical.get("href", "")) if canonical else ""
        if not value:
            metadata = soup.find("meta", attrs={"property": "og:url"})
            value = str(metadata.get("content", "")) if metadata else ""
        if not value:
            return None
        try:
            candidate = self._validate_url(urljoin(page.url, value), redirect=False, deadline=deadline)
        except ArticleFetchError:
            return None
        if page.url.startswith("https://") and candidate.url.startswith("http://"):
            return None
        if page.hostname.removeprefix("www.") != candidate.hostname.removeprefix("www."):
            return None
        return candidate.url

    def _validate_url(self, value: str, *, redirect: bool, deadline: float | None = None) -> ValidatedURL:
        if deadline is not None:
            self._check_deadline(deadline)
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
        if not hostname or hostname == "localhost":
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
            if deadline is not None:
                self._check_deadline(deadline)
        if not addresses:
            raise ArticleFetchError(code)
        try:
            if any(not ipaddress.ip_address(address).is_global for address in addresses):
                raise ArticleFetchError(code)
        except ValueError as error:
            raise ArticleFetchError(code) from error
        actual_port = port or (443 if parsed.scheme.lower() == "https" else 80)
        host_header = f"[{hostname}]" if ":" in hostname else hostname
        if port is not None:
            host_header = f"{host_header}:{port}"
        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"
        if deadline is not None:
            self._check_deadline(deadline)
        return ValidatedURL(
            urlunsplit((parsed.scheme.lower(), host_header, parsed.path or "/", parsed.query, "")),
            hostname,
            host_header,
            actual_port,
            addresses[0],
            target,
        )

    def _read(
        self,
        url: ValidatedURL,
        limit: int,
        deadline: float,
        *,
        timeout_deadline: float | None = None,
    ) -> ArticleResponse:
        try:
            self._check_deadline(deadline)
            if self.response_provider is not None:
                provided_response = self.response_provider(url)
                self._check_deadline(deadline)
                return self._bounded_response(provided_response, limit, deadline)
            connection: http.client.HTTPConnection
            timeout = min(CONNECT_TIMEOUT_SECONDS, self._remaining(deadline))
            if timeout_deadline is not None:
                timeout = min(timeout, self._remaining(timeout_deadline))
            if url.url.startswith("https://"):
                connection = _PinnedHTTPSConnection(url.hostname, url.port, url.address, timeout)
            else:
                connection = _PinnedHTTPConnection(url.hostname, url.port, url.address, timeout)
            try:
                self._set_socket_timeout(connection, deadline, timeout_deadline)
                connection.request(
                    "GET",
                    url.target,
                    headers={
                        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9",
                        "Accept-Encoding": "identity",
                        "Host": url.host_header,
                        "User-Agent": USER_AGENT,
                    },
                )
                self._check_deadline(deadline)
                self._set_socket_timeout(connection, deadline, timeout_deadline)
                http_response = connection.getresponse()
                self._check_deadline(deadline)
                headers = {name.lower(): value for name, value in http_response.getheaders()}
                self._check_deadline(deadline)
                content_length = headers.get("content-length")
                if content_length and int(content_length) > limit:
                    raise ArticleFetchError("ARTICLE_TOO_LARGE")
                body = bytearray()
                while True:
                    self._set_socket_timeout(connection, deadline, timeout_deadline)
                    chunk = http_response.read(64 * 1024)
                    self._check_deadline(deadline)
                    if not chunk:
                        break
                    body.extend(chunk)
                    if len(body) > limit:
                        raise ArticleFetchError("ARTICLE_TOO_LARGE")
                return ArticleResponse(http_response.status, headers, bytes(body))
            finally:
                connection.close()
        except ArticleFetchError:
            raise
        except TimeoutError as error:
            if self.clock() >= deadline:
                raise ArticleFetchError("ARTICLE_FETCH_DEADLINE_EXCEEDED") from error
            raise ArticleFetchError("ARTICLE_FETCH_TIMEOUT") from error
        except (http.client.HTTPException, OSError, ValueError) as error:
            raise ArticleFetchError("ARTICLE_FETCH_FAILED") from error

    def _bounded_response(self, response: ArticleResponse, limit: int, deadline: float) -> ArticleResponse:
        self._check_deadline(deadline)
        headers = {name.lower(): value for name, value in response.headers.items()}
        content_length = headers.get("content-length")
        try:
            declared_length = int(content_length) if content_length else None
        except ValueError as error:
            raise ArticleFetchError("ARTICLE_FETCH_FAILED") from error
        if declared_length is not None and declared_length > limit:
            raise ArticleFetchError("ARTICLE_TOO_LARGE")
        if len(response.body) > limit:
            raise ArticleFetchError("ARTICLE_TOO_LARGE")
        self._check_deadline(deadline)
        return ArticleResponse(response.status_code, headers, response.body)

    def _check_robots(self, article_url: ValidatedURL, deadline: float) -> None:
        parsed = urlsplit(article_url.url)
        robots_url = urlunsplit((parsed.scheme, parsed.netloc, "/robots.txt", "", ""))
        robots_deadline = min(deadline, self.clock() + ROBOTS_DEADLINE_SECONDS)
        try:
            self._check_deadline(deadline)
            response = self._fetch_robots(robots_url, deadline, robots_deadline)
            self._check_deadline(deadline)
            if response is None or response.status_code != 200:
                return
            robots_text = response.body.decode("utf-8", errors="replace")
            if not (re.search(r"(?im)^\s*user-agent\s*:", robots_text) and re.search(r"(?im)^\s*disallow\s*:", robots_text)):
                return
            parser = robotparser.RobotFileParser()
            parser.parse(robots_text.splitlines())
            if not parser.can_fetch(USER_AGENT, article_url.url):
                raise ArticleFetchError("ARTICLE_ROBOTS_DENIED")
            self._check_deadline(deadline)
        except ArticleFetchError as error:
            if error.code == "ARTICLE_ROBOTS_DENIED":
                raise
            if self.clock() >= deadline:
                raise ArticleFetchError("ARTICLE_FETCH_DEADLINE_EXCEEDED") from None
            logger.debug("robots check unavailable for %s: %s", article_url.url, error.code)

    def _fetch_robots(self, robots_url: str, deadline: float, robots_deadline: float) -> ArticleResponse | None:
        current = self._validate_url(robots_url, redirect=False, deadline=robots_deadline)
        seen_urls = {current.url}
        for redirects in range(MAX_REDIRECTS + 1):
            self._check_deadline(deadline)
            response = self._read(
                current,
                MAX_ROBOTS_BYTES,
                deadline,
                timeout_deadline=robots_deadline,
            )
            self._check_deadline(deadline)
            if not 300 <= response.status_code < 400:
                return response
            if self.clock() >= robots_deadline:
                return None
            location = response.headers.get("location")
            if not location or redirects == MAX_REDIRECTS:
                return None
            next_url = self._validate_url(urljoin(current.url, location), redirect=True, deadline=robots_deadline)
            if next_url.url in seen_urls:
                return None
            seen_urls.add(next_url.url)
            current = next_url
        return None

    def _check_deadline(self, deadline: float) -> None:
        if self.clock() >= deadline:
            raise ArticleFetchError("ARTICLE_FETCH_DEADLINE_EXCEEDED")

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise ArticleFetchError("ARTICLE_FETCH_DEADLINE_EXCEEDED")
        return remaining

    def _set_socket_timeout(
        self,
        connection: http.client.HTTPConnection,
        deadline: float,
        timeout_deadline: float | None = None,
    ) -> None:
        if connection.sock is not None:
            timeout = self._remaining(deadline)
            if timeout_deadline is not None:
                timeout = min(timeout, self._remaining(timeout_deadline))
            connection.sock.settimeout(min(READ_TIMEOUT_SECONDS, timeout))


def _url_error_code(article_code: str) -> str:
    return {
        "ARTICLE_URL_BLOCKED": "URL_POLICY_BLOCKED",
        "ARTICLE_REDIRECT_BLOCKED": "URL_REDIRECT_BLOCKED",
        "ARTICLE_FETCH_DEADLINE_EXCEEDED": "URL_RESOLUTION_DEADLINE_EXCEEDED",
        "ARTICLE_FETCH_TIMEOUT": "URL_RESOLUTION_TIMEOUT",
        "ARTICLE_CONTENT_TYPE_UNSUPPORTED": "URL_CONTENT_TYPE_UNSUPPORTED",
        "ARTICLE_TOO_LARGE": "URL_METADATA_TOO_LARGE",
    }.get(article_code, "URL_RESOLUTION_FAILED")
