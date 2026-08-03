from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from two_read_runtime.discord import sanitize_discord_text

from .schemas import DigestItem

LABELS = {
    "zh-tw": {
        "summary": "摘要",
        "why": "為什麼重要",
        "top": "🔥 今日重點",
        "rest": "🧰 其他值得注意",
        "processed": "📊 本次處理",
        "topic": "主題：",
        "sources": "來源：",
        "valid": "則有效項目",
        "hn": "HN",
        "points": "points",
        "comments": "comments",
        "metadata": "內容：僅 metadata",
        "article": "文章",
        "discussion": "討論",
        "source": "來源",
    },
    "zh-cn": {
        "summary": "摘要",
        "why": "为什么重要",
        "top": "🔥 今日重点",
        "rest": "🧰 其他值得关注",
        "processed": "📊 本次处理",
        "topic": "主题：",
        "sources": "来源：",
        "valid": "条有效项目",
        "hn": "HN",
        "points": "分",
        "comments": "条评论",
        "metadata": "内容：仅元数据",
        "article": "文章",
        "discussion": "讨论",
        "source": "来源",
    },
    "en": {
        "summary": "Summary",
        "why": "Why it matters",
        "top": "🔥 Top stories",
        "rest": "🧰 More worth noting",
        "processed": "📊 Processed",
        "topic": "Topic: ",
        "sources": "Sources: ",
        "valid": "valid items",
        "hn": "HN",
        "points": "points",
        "comments": "comments",
        "metadata": "Content: metadata only",
        "article": "Article",
        "discussion": "Discussion",
        "source": "Source",
    },
    "fr": {
        "summary": "Résumé",
        "why": "Pourquoi c’est important",
        "top": "🔥 À la une",
        "rest": "🧰 Autres éléments à noter",
        "processed": "📊 Traitement",
        "topic": "Sujet : ",
        "sources": "Sources : ",
        "valid": "éléments valides",
        "hn": "HN",
        "points": "points",
        "comments": "commentaires",
        "metadata": "Contenu : métadonnées uniquement",
        "article": "Article",
        "discussion": "Discussion",
        "source": "Source",
    },
    "ja": {
        "summary": "要約",
        "why": "重要な理由",
        "top": "🔥 今日の注目",
        "rest": "🧰 その他の注目",
        "processed": "📊 処理結果",
        "topic": "トピック：",
        "sources": "情報源：",
        "valid": "件の有効項目",
        "hn": "HN",
        "points": "ポイント",
        "comments": "件のコメント",
        "metadata": "内容：メタデータのみ",
        "article": "記事",
        "discussion": "議論",
        "source": "出典",
    },
}
NEUTRAL_LABELS = {
    "summary": "•",
    "why": "→",
    "top": "🔥",
    "rest": "🧰",
    "processed": "📊",
    "topic": "",
    "sources": "",
    "valid": "",
    "hn": "HN",
    "points": "↑",
    "comments": "💬",
    "metadata": "ℹ️",
    "article": "🔗",
    "discussion": "💬",
    "source": "🔗",
}


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
    candidate_id: int | None = None
    source_id: str | None = None
    source_name: str | None = None
    review_score: int | None = None


def canonical_url(value: str | None) -> str | None:
    if not value:
        return None
    parts = urlsplit(value)
    blocked = {"ref", "source", "campaign", "mc_cid", "mc_eid", "mkt_tok"}
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


def _entry_rank(entry: DigestEntry) -> tuple[int, int, float, int, int, float]:
    return (
        entry.review_score if entry.review_score is not None else -1,
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


def _labels(language: str) -> dict[str, str]:
    normalized = language.casefold().replace("_", "-")
    aliases = {
        "zh-tw": "zh-tw",
        "zh-hant": "zh-tw",
        "zh-hk": "zh-tw",
        "zh-mo": "zh-tw",
        "zh-cn": "zh-cn",
        "zh-hans": "zh-cn",
    }
    return LABELS.get(aliases.get(normalized, normalized.split("-", maxsplit=1)[0]), NEUTRAL_LABELS)


def render_digest(
    items: Sequence[DigestItem | DigestEntry],
    when: datetime,
    topic: str,
    source_names: str,
    top_items: int = 5,
    language: str = "zh-TW",
) -> str:
    labels = _labels(language)
    safe_topic = sanitize_discord_text(topic)
    safe_source_names = sanitize_discord_text(source_names)
    entries = [
        item
        if isinstance(item, DigestEntry)
        else DigestEntry(item, article_url=str(item.source_url) if item.source_url else None)
        for item in items
    ]
    eligible = dedupe_entries(entries)
    eligible.sort(key=_entry_rank, reverse=True)
    if not eligible:
        return ""

    def entry(value: DigestEntry, prefix: str) -> str:
        item = value.item
        lines = [
            f"{prefix} {sanitize_discord_text(item.title)}",
            f"   {labels['summary']}：{sanitize_discord_text(item.summary_zh_tw)}",
            f"   {labels['why']}：{sanitize_discord_text(item.why_it_matters_zh_tw)}",
        ]
        if value.source_name:
            lines.append(f"   {labels['source']}：{sanitize_discord_text(value.source_name)}")
        if value.hn_item_id:
            if value.hn_score is not None and value.hn_comments is not None:
                lines.append(f"   {labels['hn']}：{value.hn_score} {labels['points']} · {value.hn_comments} {labels['comments']}")
            if value.content_basis == "metadata":
                lines.append(f"   {labels['metadata']}")
            if value.article_url and value.article_url != value.discussion_url:
                lines.append(f"   {labels['article']}：<{value.article_url}>")
            if value.discussion_url:
                lines.append(f"   {labels['discussion']}：<{value.discussion_url}>")
        elif item.source_url:
            lines.append(f"   {labels['article']}：<{item.source_url}>")
        return "\n".join(lines)

    top = eligible[:top_items]
    rest = eligible[top_items:]
    sections = [
        f"📰 {safe_topic} 2much2read — {when:%Y-%m-%d}",
        labels["top"] + "\n" + "\n\n".join(entry(item, f"{i}.") for i, item in enumerate(top, 1)),
    ]
    if rest:
        sections.append(labels["rest"] + "\n" + "\n\n".join(entry(item, "•") for item in rest))
    sections.append(
        f"{labels['processed']}\n{labels['topic']}{safe_topic}\n{labels['sources']}{safe_source_names} · "
        f"{len(eligible)} {labels['valid']}"
    )
    return "\n\n".join(sections)
