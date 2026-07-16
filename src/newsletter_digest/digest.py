from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .schemas import DigestItem


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
        if current is None or (item.confidence, item.importance) > (
            current.confidence,
            current.importance,
        ):
            winners[key] = item
    return list(winners.values())


def render_digest(
    items: list[DigestItem],
    when: datetime,
    topic: str,
    source_names: str,
    top_items: int = 5,
) -> str:
    eligible = [item for item in dedupe(items) if item.confidence >= 0.45]
    eligible.sort(key=lambda item: (item.importance, item.confidence), reverse=True)
    if not eligible:
        return ""

    def entry(item: DigestItem, prefix: str) -> str:
        link = f"\n   來源：<{item.source_url}>" if item.source_url else ""
        return f"{prefix} {item.title}\n   摘要：{item.summary_zh_tw}\n   為什麼重要：{item.why_it_matters_zh_tw}{link}"

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
