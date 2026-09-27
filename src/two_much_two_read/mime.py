from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Callable
from email import policy
from email.message import Message
from email.parser import BytesParser
from functools import partial
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
MAX_LINK_OCCURRENCES = 2_000
# A plain part this many times shorter than the HTML's text is a stand-in, not the newsletter: The
# Hacker News sends 580 characters asking to be read "with an HTML friendly email client", and the
# extractor, reading only that, made two items out of the subject line and gave them the HTML's
# first links - both advertisements.
PLAIN_PART_STAND_IN_RATIO = 4

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
# The label may hold no bracket and the URL no parenthesis. Each stops an attempt at the next link's
# start, so a body of brackets costs one pass instead of a scan to the end from every one of them:
# 20,000 "[" took 0.76 s, and a 2 MB part would take half an hour. The URL keeps its brackets, as
# ?filters[]=news or an IPv6 host has them.
MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\[\]]+)\]\((https?://[^\s()]+)\)")
# Text the newsletter itself wrote in the shape of a link code, such as a "[L2]" cache level. A
# Markdown link label is left to the link pass, which takes the brackets off.
LITERAL_LINK_CODE = re.compile(r"\[(\s*L\s*\d{1,4}\s*)\](?!\()", re.IGNORECASE)
URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")
# Characters that print as nothing. Senders pad the preview text with them, alternating with no-break
# spaces, so that a mail client shows no more of the body in the inbox: TLDR's four editions each
# send 52 such pairs, 264 tokens of nothing. Only runs go: one such character alone is part of the
# text - a zero-width non-joiner inside a Persian word, a zero-width space between Thai words - and
# a zero-width joiner, which holds emoji such as 🧑‍💻 together, is not counted at all.
INVISIBLE = "\u200b\u200c\u2060\ufeff\u034f\u00ad"
# A run starts at an invisible character, so a long stretch of spaces is not rescanned from each one.
INVISIBLE_RUN = re.compile(rf"[{INVISIBLE}](?:[ \t\u00a0\u2007\u202f]*[{INVISIBLE}])+[ \t\u00a0\u2007\u202f]*")
# A plain part that lists its links as numbered notes at the end ("Links:", a rule, then "[8] URL"),
# as TLDR's does, with only "[8]" in the text.
FOOTNOTE_TABLE = re.compile(r"\n[ \t]*Links:[ \t]*\n[ \t]*-{3,}[ \t]*\n((?:[ \t]*\[\d{1,4}\][ \t]+\S+[ \t]*(?:\n|\Z))+)\s*\Z")
FOOTNOTE_ENTRY = re.compile(r"\[(\d{1,4})\][ \t]+(\S+)")
FOOTNOTE_REFERENCE = re.compile(r"(?<!\[)\[(\d{1,4})\](?![\](])")


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
    """The links an email offers, in document order, for matching its items to their articles.

    Two bounds, and neither fails the email. MAX_LINK_OCCURRENCES caps the work: every link the
    scan meets counts, repeats included, so a thousand copies of one anchor cost what a thousand
    distinct ones would. MAX_LINK_CANDIDATES caps what is kept. Reaching either ends the scan and
    keeps what came first, which in a newsletter is the stories, with the footer last.

    One counter used to do both jobs and raised when it passed 200. Link-dense issues repeat each
    link in the HTML and the plain part, so they crossed it on repeats alone and the whole email
    failed - AINews lost 11 of 25 issues that way - when the cost of a cut is only that items
    further down match no link.
    """
    candidates: list[LinkCandidate] = []
    seen: set[str] = set()
    scanned = 0

    def full() -> bool:
        return scanned >= MAX_LINK_OCCURRENCES or len(candidates) >= MAX_LINK_CANDIDATES

    def add(
        raw_url: str, anchor_text: str, nearby_text: str | Callable[[], str], kind: Literal["article", "unknown"] = "article"
    ) -> None:
        # An anchor's nearby_text comes as a callable, because it walks back through the document for
        # a heading and only the links that are kept should pay for that. A plain-text link's is one
        # line, cheap enough to take either way.
        nonlocal scanned
        scanned += 1
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
                nearby_text=nearby_text if isinstance(nearby_text, str) else nearby_text(),
                position=len(candidates),
                kind=kind,
            )
        )

    for anchor in _visible_soup(html).find_all("a", limit=MAX_LINK_OCCURRENCES):
        if full():
            return candidates
        anchor_text = anchor.get_text(" ", strip=True)
        add(
            str(anchor.get("href", "")),
            anchor_text,
            partial(_nearby_text, anchor),
            "article" if anchor_text else "unknown",
        )
    for match in MARKDOWN_LINK_PATTERN.finditer(plain):
        if full():
            return candidates
        raw_url = match.group(2)
        add(raw_url, match.group(1), _plain_context(plain, match.start(), raw_url))
    # Bare URLs only: a Markdown link's URL was taken whole above, and read again here it would stop
    # at the first "]" of ?filters[]= and add its own truncated copy.
    bare = MARKDOWN_LINK_PATTERN.sub(lambda match: match.group(1), plain)
    for match in URL_PATTERN.finditer(bare):
        if full():
            return candidates
        matched_url = match.group()
        raw_url = _trimmed_url(matched_url)
        context = _plain_context(bare, match.start(), matched_url)
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


def _trimmed_url(matched_url: str) -> str:
    """A bare URL without the punctuation that ends the sentence around it."""
    raw_url = matched_url.rstrip(".,;:!?]}")
    while raw_url.endswith(")") and raw_url.count("(") < raw_url.count(")"):
        raw_url = raw_url[:-1]
    return raw_url


def _coded_text(text: str, candidates: list[LinkCandidate]) -> str:
    """The analysis text with every URL replaced by its link code, or removed.

    The model never needs a URL: it may not write one, and an item's link comes from the candidate
    list, not from the text. What it can use is which link belongs to which item, and a code such as
    [L7] carries that in three tokens where a tracking URL costs fifty or more. Measured on real
    issues, URLs were 93% of The New Stack's tokens, 63% of AINews's and 54% of Risky Business's,
    and pushed two of them past num_ctx. A URL that is not a candidate - an unsubscribe or share
    link, or one past the candidate cap - is dropped rather than coded, so no code points at it.
    """
    codes = {str(candidate.raw_url): candidate.code for candidate in candidates}

    def code_for(raw_url: str) -> str | None:
        safe_url = _safe_url(raw_url)
        if safe_url is None:
            return None
        try:
            return codes.get(str(HTTP_URL.validate_python(safe_url)))
        except ValueError:
            return None

    def markdown(match: re.Match[str]) -> str:
        code = code_for(match.group(2))
        return f"{match.group(1)} [{code}]" if code else match.group(1)

    def bare(match: re.Match[str]) -> str:
        raw_url = _trimmed_url(match.group())
        code = code_for(raw_url)
        return (f"[{code}]" if code else "") + match.group()[len(raw_url) :]

    # Bracketed code-shaped text the newsletter wrote itself would read as a code: the model could
    # take it for a link, and the text fields would drop it. Parentheses keep the words and lose the
    # shape - unlike full-width brackets, which a model may write back as ASCII when it copies them.
    text = LITERAL_LINK_CODE.sub(r"(\1)", text)
    return URL_PATTERN.sub(bare, MARKDOWN_LINK_PATTERN.sub(markdown, text))


def _readable(text: str) -> str:
    """The text with Windows line ends made plain and runs of characters that print as nothing gone."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return INVISIBLE_RUN.sub(" ", text)


def _inlined_footnotes(text: str) -> str:
    """The text with each numbered note's URL where its number stood, and the list of notes gone.

    The model reads "[8]" in the story and "[8] [L44]" some 3,000 tokens later, and has to connect
    the two to know which link an item has. With the URL in place, the link pass codes it where the
    story is, and the list - 290 to 360 tokens in each TLDR issue - is left with nothing to say. Only
    a list closing the text is read, and only its numbers are replaced: a "[1]" in an essay without
    one is a citation, and stays as it is.
    """
    table = FOOTNOTE_TABLE.search(text)
    if table is None:
        return text
    # A note that is no web address (a mailto:) has no code to become, so its number goes with it.
    notes = {number: target if URL_PATTERN.fullmatch(target) else "" for number, target in FOOTNOTE_ENTRY.findall(table.group(1))}
    return FOOTNOTE_REFERENCE.sub(lambda match: notes.get(match.group(1), match.group()), text[: table.start()])


def _content(plain: list[str], html: list[str]) -> ExtractedEmailContent:
    # Stripped after cleaning: a plain part that was only padding is empty, not a space that outweighs
    # nothing and then trims to nothing.
    plain_content = _inlined_footnotes(_readable("\n".join(value.strip() for value in plain if value.strip())).strip())
    analysis_text = plain_content
    html_content = "\n".join(value for value in html if value.strip())
    html_text = _readable(html_to_text(html_content)) if html_content else ""
    # Words against words: the HTML's text still carries every link's URL, which the plain part may not.
    if len(URL_PATTERN.sub("", analysis_text)) * PLAIN_PART_STAND_IN_RATIO < len(URL_PATTERN.sub("", html_text)):
        analysis_text = html_text
    candidates = _link_candidates(plain_content, html_content)
    # Coded before the length is measured and cut, so the cut is spent on text rather than URLs.
    analysis_text = _coded_text(analysis_text, candidates)
    # Line by line rather than with [ \t]+\n, which rescans a long run of spaces from each of them.
    analysis_text = "\n".join(line.rstrip(" \t\u00a0") for line in analysis_text.split("\n"))
    analysis_text = re.sub(r"\n{3,}", "\n\n", analysis_text).strip()
    if not analysis_text:
        raise EmptyEmailError("email contains no usable text")
    original_characters = len(analysis_text)
    analysis_text = analysis_text[:MAX_ANALYSIS_CHARS].rstrip()
    return ExtractedEmailContent(
        analysis_text=analysis_text,
        original_characters=original_characters,
        link_candidates=candidates,
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
