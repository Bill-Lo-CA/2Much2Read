from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .schemas import DigestItem


@dataclass(frozen=True)
class DigestEntry:
    item: DigestItem
    published_at: datetime | None = None
    article_url: str | None = None
    discussion_url: str | None = None
    hn_score: int | None = None
    hn_comments: int | None = None
    hn_item_id: str | None = None
    content_basis: str | None = None


def canonical_url(value: str | None) -> str | None:
    if not value:
        return None
    parts = urlsplit(value)
    blocked = {"ref", "source", "campaign"}
    query = [
        (key, val)
        for key, val in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in blocked
    ]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(query), ""))


def normalized_title(value: str) -> str:
    return re.sub(r"[^\w]+", " ", value.casefold()).strip()


def dedupe(items: list[DigestItem]) -> list[DigestItem]:
    # ponytail: one-pass in-memory dedupe; move history lookup to SQLite when volume warrants it.
    winners: dict[str, DigestItem] = {}
    for item in items:
        key = (canonical_url(str(item.source_url)) if item.source_url else None) or normalized_title(item.title)
        current = winners.get(key)
        if current is None or (item.importance, item.confidence) > (current.importance, current.confidence):
            winners[key] = item
    return list(winners.values())


def _entry_key(entry: DigestEntry) -> str:
    url = entry.article_url or (str(entry.item.source_url) if entry.item.source_url else None)
    if canonical := canonical_url(url):
        return canonical
    if entry.hn_item_id:
        return f"hn:{entry.hn_item_id}"
    return normalized_title(entry.item.title)


def _entry_rank(entry: DigestEntry) -> tuple[int, float, int, int, float]:
    return (
        entry.item.importance,
        entry.item.confidence,
        entry.hn_score if entry.hn_score is not None else -1,
        entry.hn_comments if entry.hn_comments is not None else -1,
        entry.published_at.timestamp() if entry.published_at else float("-inf"),
    )


def _preserve_hn_attribution(primary: DigestEntry, other: DigestEntry) -> DigestEntry:
    if primary.discussion_url or not other.discussion_url:
        return primary
    return replace(
        primary,
        discussion_url=other.discussion_url,
        hn_score=other.hn_score,
        hn_comments=other.hn_comments,
        hn_item_id=other.hn_item_id,
    )


def dedupe_entries(items: list[DigestEntry]) -> list[DigestEntry]:
    # ponytail: one-pass in-memory dedupe; move history lookup to SQLite when volume warrants it.
    winners: dict[str, DigestEntry] = {}
    for item in items:
        key = _entry_key(item)
        current = winners.get(key)
        if current is None:
            winners[key] = item
        elif _entry_rank(item) > _entry_rank(current):
            winners[key] = _preserve_hn_attribution(item, current)
        else:
            winners[key] = _preserve_hn_attribution(current, item)
    return list(winners.values())


def render_digest(
    items: Sequence[DigestItem | DigestEntry],
    when: datetime,
    topic: str,
    source_names: str,
    top_items: int = 5,
) -> str:
    entries = [
        item
        if isinstance(item, DigestEntry)
        else DigestEntry(item, article_url=str(item.source_url) if item.source_url else None)
        for item in items
    ]
    eligible = [item for item in dedupe_entries(entries) if item.item.confidence >= 0.45]
    eligible.sort(key=_entry_rank, reverse=True)
    if not eligible:
        return ""

    def entry(value: DigestEntry, prefix: str) -> str:
        item = value.item
        lines = [f"{prefix} {item.title}", f"   摘要：{item.summary_zh_tw}", f"   為什麼重要：{item.why_it_matters_zh_tw}"]
        if value.hn_item_id:
            if value.hn_score is not None and value.hn_comments is not None:
                lines.append(f"   HN：{value.hn_score} points · {value.hn_comments} comments")
            if value.content_basis == "metadata":
                lines.append("   內容：僅 metadata")
            if value.article_url and value.article_url != value.discussion_url:
                lines.append(f"   文章：<{value.article_url}>")
            if value.discussion_url:
                lines.append(f"   討論：<{value.discussion_url}>")
        elif item.source_url:
            lines.append(f"   來源：<{item.source_url}>")
        return "\n".join(lines)

    top = eligible[:top_items]
    rest = eligible[top_items:]
    sections = [
        f"📰 {topic} 2much2read — {when:%Y-%m-%d}",
        "🔥 今日重點\n" + "\n\n".join(entry(item, f"{i}.") for i, item in enumerate(top, 1)),
    ]
    if rest:
        sections.append("🧰 其他值得注意\n" + "\n\n".join(entry(item, "•") for item in rest))
    sections.append(f"📊 本次處理\n主題：{topic}\n來源：{source_names} · {len(eligible)} 則有效項目")
    return "\n\n".join(sections).replace("@", "@\u200b")
