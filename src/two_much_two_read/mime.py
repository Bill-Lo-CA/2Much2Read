from __future__ import annotations

import base64
import binascii
import re
from email import policy
from email.message import Message
from email.parser import BytesParser
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .schemas import HTTP_URL, ExtractedEmailContent, LinkCandidate

FOOTER_PATTERN = re.compile(r"\b(unsubscribe|manage preferences|privacy policy|取消訂閱)\b", re.I)
NON_ARTICLE_PATTERN = re.compile(
    r"\b(unsubscribe|preferences?|privacy|terms|view (this )?email|view in browser|login|account|share|"
    r"follow us|linkedin|twitter|facebook|instagram)\b",
    re.I,
)


class EmptyEmailError(ValueError):
    pass


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


def _link_candidates(html: str) -> list[LinkCandidate]:
    candidates: list[LinkCandidate] = []
    seen: set[str] = set()
    for position, anchor in enumerate(_visible_soup(html).find_all("a")):
        raw_url = _safe_url(str(anchor.get("href", "")))
        if raw_url is None or raw_url in seen:
            continue
        anchor_text = anchor.get_text(" ", strip=True)
        nearby_text = _nearby_text(anchor)
        if NON_ARTICLE_PATTERN.search(" ".join((anchor_text, nearby_text, raw_url))):
            continue
        seen.add(raw_url)
        candidates.append(
            LinkCandidate(
                candidate_id=f"link-{len(candidates) + 1:04d}",
                raw_url=HTTP_URL.validate_python(raw_url),
                anchor_text=anchor_text,
                nearby_text=nearby_text,
                position=position,
                kind="article" if anchor_text else "unknown",
            )
        )
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
        if FOOTER_PATTERN.search(line):
            break
        if line or (kept and kept[-1]):
            kept.append(line)
    return "\n".join(kept).strip()


def _content(plain: list[str], html: list[str]) -> ExtractedEmailContent:
    analysis_text = "\n".join(value.strip() for value in plain if value.strip())
    html_content = "\n".join(value for value in html if value.strip())
    if not analysis_text:
        analysis_text = html_to_text(html_content)
    analysis_text = re.sub(r"\n{3,}", "\n\n", analysis_text).strip()
    if not analysis_text:
        raise EmptyEmailError("email contains no usable text")
    return ExtractedEmailContent(analysis_text=analysis_text, link_candidates=_link_candidates(html_content))


def _decode(part: Message) -> str:
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace") if isinstance(payload, bytes) else str(payload)


def extract_mime(raw: bytes) -> ExtractedEmailContent:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    plain: list[str] = []
    html: list[str] = []
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        if part.get_content_type() == "text/plain":
            plain.append(_decode(part))
        elif part.get_content_type() == "text/html":
            html.append(_decode(part))
    return _content(plain, html)


def _gmail_parts(node: dict[str, object], wanted: str) -> list[str]:
    found: list[str] = []
    body = node.get("body")
    data = body.get("data") if isinstance(body, dict) else None
    headers = node.get("headers", [])
    header_values = headers if isinstance(headers, list) else []
    disposition = " ".join(
        str(header.get("value", ""))
        for header in header_values
        if isinstance(header, dict) and str(header.get("name", "")).casefold() == "content-disposition"
    )
    if (
        node.get("mimeType") == wanted
        and not node.get("filename")
        and "attachment" not in disposition.casefold()
        and isinstance(data, str)
    ):
        try:
            raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
        except (ValueError, binascii.Error):
            raw = None
        if raw is not None:
            content_type = " ".join(
                str(header.get("value", ""))
                for header in header_values
                if isinstance(header, dict) and str(header.get("name", "")).casefold() == "content-type"
            )
            match = re.search(r"charset=[\"']?([^;\"']+)", content_type, re.I)
            charset = match.group(1) if match else "utf-8"
            try:
                found.append(raw.decode(charset, errors="replace"))
            except LookupError:
                found.append(raw.decode("utf-8", errors="replace"))
    parts = node.get("parts", [])
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, dict):
                found.extend(_gmail_parts(part, wanted))
    return found


def extract_gmail_payload(payload: dict[str, object]) -> ExtractedEmailContent:
    return _content(_gmail_parts(payload, "text/plain"), _gmail_parts(payload, "text/html"))
