from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

from .mime import html_to_text

MIN_USEFUL_TEXT_CHARACTERS = 500
MAX_EXTRACTED_TEXT_CHARACTERS = 30_000


class ArticleExtractionError(ValueError):
    def __init__(self, code: str = "ARTICLE_NO_USABLE_TEXT") -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ExtractedArticle:
    title: str | None
    text: str
    truncated: bool


def _normalized_text(value: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _usable_text(value: str) -> tuple[str, bool]:
    text = _normalized_text(value)
    if len(text) < MIN_USEFUL_TEXT_CHARACTERS:
        raise ArticleExtractionError()
    return text[:MAX_EXTRACTED_TEXT_CHARACTERS], len(text) > MAX_EXTRACTED_TEXT_CHARACTERS


def extract_article(content_type: str, body: bytes) -> ExtractedArticle:
    if content_type == "text/plain":
        text, truncated = _usable_text(body.decode("utf-8", errors="replace"))
        return ExtractedArticle(None, text, truncated)
    soup = BeautifulSoup(body, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else None
    for node in soup.select(
        "script,style,noscript,form,nav,footer,aside,iframe,svg,canvas,template,[hidden],"
        ".share,.sharing,.social,.advertisement,.ad"
    ):
        node.decompose()
    for node in soup.find_all(style=True):
        style = str(node["style"]).replace(" ", "").lower()
        if "display:none" in style or "visibility:hidden" in style:
            node.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    text, truncated = _usable_text(root.get_text("\n", strip=True))
    return ExtractedArticle(title, text, truncated)


def extract_self_post(html: str) -> ExtractedArticle:
    text, truncated = _usable_text(html_to_text(html))
    return ExtractedArticle(None, text, truncated)
