from __future__ import annotations

import base64
import binascii
import re
from email import policy
from email.message import Message
from email.parser import BytesParser
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .schemas import HTTP_URL, ExtractedEmailContent, LinkCandidate

MAX_MIME_DEPTH = 20
MAX_MIME_PARTS = 200
MAX_TOTAL_DECODED_BYTES = 5 * 1024 * 1024
MAX_PLAIN_BYTES = 2 * 1024 * 1024
MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_ANALYSIS_CHARS = 45_000
MAX_LINK_CANDIDATES = 200

FOOTER_LINE_PATTERN = re.compile(
    r"^(?:unsubscribe|manage preferences|privacy policy|取消訂閱)(?:\s*[|·/]\s*"
    r"(?:unsubscribe|manage preferences|privacy policy|取消訂閱))*$",
    re.I,
)
CONTROL_LABEL_PATTERN = re.compile(
    r"(?:unsubscribe(?: from (?:this|all) emails?)?|manage preferences|privacy policy|terms(?: of (?:service|use))?|"
    r"view (?:this )?email|view in browser|sign in|manage (?:your )?account|share(?: on \w+)?|"
    r"follow us(?: on \w+)?|linkedin|twitter|facebook|instagram)",
    re.I,
)
MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
URL_PATTERN = re.compile(r"https?://[^\s<>\"'\]]+")


class EmailExtractionError(ValueError):
    code: str

    def __init__(self, message: str, code: str | None = None) -> None:
        if code is None:
            code = message
        super().__init__(message)
        self.code = code


class EmptyEmailError(EmailExtractionError):
    def __init__(self, message: str = "email contains no usable text") -> None:
        super().__init__(message, "EMAIL_NO_USABLE_TEXT")


class _TraversalBudget:
    def __init__(self) -> None:
        self.parts = 0
        self.total_bytes = 0
        self.plain_bytes = 0
        self.html_bytes = 0

    def visit(self, depth: int) -> None:
        if depth > MAX_MIME_DEPTH or self.parts >= MAX_MIME_PARTS:
            raise EmailExtractionError("email MIME structure is too complex", "EMAIL_STRUCTURE_TOO_COMPLEX")
        self.parts += 1

    def add_decoded_bytes(self, content_type: str, payload: bytes) -> None:
        size = len(payload)
        plain_bytes = self.plain_bytes + size if content_type == "text/plain" else self.plain_bytes
        html_bytes = self.html_bytes + size if content_type == "text/html" else self.html_bytes
        if self.total_bytes + size > MAX_TOTAL_DECODED_BYTES or plain_bytes > MAX_PLAIN_BYTES or html_bytes > MAX_HTML_BYTES:
            raise EmailExtractionError("email exceeds its extraction size budget", "EMAIL_TOO_LARGE")
        self.total_bytes += size
        self.plain_bytes = plain_bytes
        self.html_bytes = html_bytes


def _safe_url(url: str) -> str | None:
    value = url.strip()
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return None
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path, parsed.query, ""))


def _visible_soup(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "lxml")
    for node in soup.select("script,style,noscript,form,[hidden],footer"):
        node.decompose()
    for image in soup.find_all("img"):
        if image.get("width") in {"0", "1"} or image.get("height") in {"0", "1"}:
            image.decompose()
    return soup


def _nearby_text(anchor: Tag) -> str:
    heading = anchor.find_previous(["h1", "h2", "h3", "h4", "h5", "h6"])
    parent = anchor.parent
    parent_text = "" if parent is None or parent.name in {"body", "html", "[document]"} else parent.get_text(" ", strip=True)
    values = [heading.get_text(" ", strip=True) if heading else "", parent_text]
    return " ".join(value for value in values if value)[:400]


def _plain_context(text: str, position: int, raw_url: str) -> str:
    line_start = text.rfind("\n", 0, position) + 1
    line_end = text.find("\n", position)
    line = text[line_start:] if line_end < 0 else text[line_start:line_end]
    return line.replace(raw_url, "").strip(" -:()")[:400]


def _link_candidates(plain: str, html: str) -> list[LinkCandidate]:
    candidates: list[LinkCandidate] = []
    seen: set[str] = set()
    processed = 0

    def add(raw_url: str, anchor_text: str, nearby_text: str, kind: Literal["article", "unknown"] = "article") -> None:
        nonlocal processed
        processed += 1
        if processed > MAX_LINK_CANDIDATES:
            raise EmailExtractionError("email contains too many link candidates", "EMAIL_TOO_MANY_LINKS")
        safe_url = _safe_url(raw_url)
        if safe_url is None or safe_url in seen:
            return
        if CONTROL_LABEL_PATTERN.fullmatch(anchor_text.strip()):
            return
        try:
            validated_url = HTTP_URL.validate_python(safe_url)
        except ValueError:
            return
        seen.add(safe_url)
        candidates.append(
            LinkCandidate(
                candidate_id=f"link-{len(candidates) + 1:04d}",
                raw_url=validated_url,
                anchor_text=anchor_text,
                nearby_text=nearby_text,
                position=len(candidates),
                kind=kind,
            )
        )

    for anchor in _visible_soup(html).find_all("a", limit=MAX_LINK_CANDIDATES + 1):
        anchor_text = anchor.get_text(" ", strip=True)
        nearby_text = _nearby_text(anchor)
        add(str(anchor.get("href", "")), anchor_text, nearby_text, "article" if anchor_text else "unknown")
    for match in MARKDOWN_LINK_PATTERN.finditer(plain):
        raw_url = match.group(2)
        add(raw_url, match.group(1), _plain_context(plain, match.start(), raw_url))
    for match in URL_PATTERN.finditer(plain):
        matched_url = match.group()
        raw_url = matched_url.rstrip(".,;:!?]}")
        while raw_url.endswith(")") and raw_url.count("(") < raw_url.count(")"):
            raw_url = raw_url[:-1]
        context = _plain_context(plain, match.start(), matched_url)
        add(raw_url, context, context, "unknown")
    return candidates


def html_to_text(html: str) -> str:
    soup = _visible_soup(html)
    for anchor in soup.find_all("a"):
        label = anchor.get_text(" ", strip=True)
        url = _safe_url(str(anchor.get("href", "")))
        anchor.replace_with(f"[{label}]({url})" if label and url else label)
    text = soup.get_text("\n")
    lines = [line.strip() for line in text.splitlines()]
    kept: list[str] = []
    for line in lines:
        if FOOTER_LINE_PATTERN.fullmatch(line):
            break
        if line or (kept and kept[-1]):
            kept.append(line)
    return "\n".join(kept).strip()


def _content(plain: list[str], html: list[str]) -> ExtractedEmailContent:
    plain_content = "\n".join(value.strip() for value in plain if value.strip())
    analysis_text = plain_content
    html_content = "\n".join(value for value in html if value.strip())
    if not analysis_text:
        analysis_text = html_to_text(html_content)
    analysis_text = re.sub(r"\n{3,}", "\n\n", analysis_text).strip()
    if not analysis_text:
        raise EmptyEmailError("email contains no usable text")
    original_characters = len(analysis_text)
    analysis_text = analysis_text[:MAX_ANALYSIS_CHARS].rstrip()
    return ExtractedEmailContent(
        analysis_text=analysis_text,
        original_characters=original_characters,
        link_candidates=_link_candidates(plain_content, html_content),
    )


def _decoded_payload(part: Message) -> bytes:
    payload = part.get_payload(decode=True) or b""
    return payload if isinstance(payload, bytes) else str(payload).encode()


def _decode(part: Message, payload: bytes) -> str:
    charset = part.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace")


def extract_mime(raw: bytes) -> ExtractedEmailContent:
    if len(raw) > MAX_TOTAL_DECODED_BYTES:
        raise EmailExtractionError("raw email exceeds its extraction size budget", "EMAIL_TOO_LARGE")
    message = BytesParser(policy=policy.default).parsebytes(raw)
    budget = _TraversalBudget()
    plain: list[str] = []
    html: list[str] = []
    budget.visit(0)
    stack: list[tuple[Message, int]] = [(message, 0)]
    while stack:
        part, depth = stack.pop()
        if part.is_multipart():
            children = part.get_payload()
            if isinstance(children, list):
                if len(children) > MAX_MIME_PARTS - budget.parts:
                    raise EmailExtractionError("email MIME structure is too complex", "EMAIL_STRUCTURE_TOO_COMPLEX")
                for child in reversed(children):
                    if isinstance(child, Message):
                        budget.visit(depth + 1)
                        stack.append((child, depth + 1))
            continue
        payload = _decoded_payload(part)
        content_type = part.get_content_type()
        budget.add_decoded_bytes(content_type, payload)
        if part.get_content_disposition() == "attachment":
            continue
        if content_type == "text/plain":
            plain.append(_decode(part, payload))
        elif content_type == "text/html":
            html.append(_decode(part, payload))
    return _content(plain, html)


def _gmail_payload_bytes(data: str) -> bytes | None:
    if len(data) > 4 * ((MAX_TOTAL_DECODED_BYTES + 2) // 3):
        raise EmailExtractionError("email exceeds its extraction size budget", "EMAIL_TOO_LARGE")
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (ValueError, binascii.Error):
        return None


def extract_gmail_payload(payload: dict[str, object]) -> ExtractedEmailContent:
    budget = _TraversalBudget()
    plain: list[str] = []
    html: list[str] = []
    budget.visit(0)
    stack: list[tuple[dict[str, object], int]] = [(payload, 0)]
    while stack:
        node, depth = stack.pop()
        body = node.get("body")
        data = body.get("data") if isinstance(body, dict) else None
        content_type = str(node.get("mimeType", ""))
        raw = _gmail_payload_bytes(data) if isinstance(data, str) else None
        if raw is not None:
            budget.add_decoded_bytes(content_type, raw)
        headers = node.get("headers", [])
        header_values = headers if isinstance(headers, list) else []
        disposition = " ".join(
            str(header.get("value", ""))
            for header in header_values
            if isinstance(header, dict) and str(header.get("name", "")).casefold() == "content-disposition"
        )
        if (
            raw is not None
            and content_type in {"text/plain", "text/html"}
            and not node.get("filename")
            and "attachment" not in disposition.casefold()
        ):
            charset_header = " ".join(
                str(header.get("value", ""))
                for header in header_values
                if isinstance(header, dict) and str(header.get("name", "")).casefold() == "content-type"
            )
            match = re.search(r"charset=[\"']?([^;\"']+)", charset_header, re.I)
            charset = match.group(1) if match else "utf-8"
            try:
                text = raw.decode(charset, errors="replace")
            except LookupError:
                text = raw.decode("utf-8", errors="replace")
            (plain if content_type == "text/plain" else html).append(text)

        parts = node.get("parts", [])
        if isinstance(parts, list):
            if len(parts) > MAX_MIME_PARTS - budget.parts:
                raise EmailExtractionError("email MIME structure is too complex", "EMAIL_STRUCTURE_TOO_COMPLEX")
            for part in reversed(parts):
                if isinstance(part, dict):
                    budget.visit(depth + 1)
                    stack.append((part, depth + 1))
    return _content(plain, html)
