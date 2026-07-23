from __future__ import annotations

import base64
import binascii
import re
from email import policy
from email.message import Message
from email.parser import BytesParser
from urllib.parse import urlparse

from bs4 import BeautifulSoup


class EmptyEmailError(ValueError):
    pass


def _safe_url(url: str) -> str | None:
    value = url.strip()
    return value if urlparse(value).scheme.lower() in {"http", "https"} else None


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for node in soup.select("script,style,noscript,form,[hidden]"):
        node.decompose()
    for image in soup.find_all("img"):
        if image.get("width") in {"0", "1"} or image.get("height") in {"0", "1"}:
            image.decompose()
    for anchor in soup.find_all("a"):
        label = anchor.get_text(" ", strip=True)
        url = _safe_url(str(anchor.get("href", "")))
        anchor.replace_with(f"[{label}]({url})" if label and url else label)
    text = soup.get_text("\n")
    lines = [line.strip() for line in text.splitlines()]
    footer = re.compile(r"^(unsubscribe|manage preferences|privacy policy|取消訂閱)\b", re.I)
    kept: list[str] = []
    for line in lines:
        if footer.search(line):
            break
        if line or (kept and kept[-1]):
            kept.append(line)
    return "\n".join(kept).strip()


def _decode(part: Message) -> str:
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace") if isinstance(payload, bytes) else str(payload)


def extract_mime(raw: bytes) -> str:
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
    text = "\n".join(value.strip() for value in plain if value.strip())
    if not text:
        text = html_to_text("\n".join(html))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise EmptyEmailError("email contains no usable text")
    return text


def extract_gmail_payload(payload: dict[str, object]) -> str:
    def walk(node: dict[str, object], wanted: str) -> list[str]:
        found: list[str] = []
        if node.get("mimeType") == wanted:
            body = node.get("body")
            data = body.get("data") if isinstance(body, dict) else None
            if isinstance(data, str):
                try:
                    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
                except (ValueError, binascii.Error):
                    raw = None
                if raw is not None:
                    charset = "utf-8"
                    headers = node.get("headers", [])
                    if isinstance(headers, list):
                        content_type = next(
                            (str(h.get("value", "")) for h in headers if isinstance(h, dict) and h.get("name") == "Content-Type"),
                            "",
                        )
                        match = re.search(r"charset=[\"']?([^;\"']+)", content_type, re.I)
                        charset = match.group(1) if match else charset
                    try:
                        found.append(raw.decode(charset, errors="replace"))
                    except LookupError:
                        found.append(raw.decode("utf-8", errors="replace"))
        parts = node.get("parts", [])
        if isinstance(parts, list):
            for part in parts:
                if isinstance(part, dict):
                    found.extend(walk(part, wanted))
        return found

    plain = "\n".join(walk(payload, "text/plain")).strip()
    text = plain or html_to_text("\n".join(walk(payload, "text/html")))
    if not text:
        raise EmptyEmailError("email contains no usable text")
    return re.sub(r"\n{3,}", "\n\n", text).strip()
