from __future__ import annotations

import re
from collections import Counter
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
    reranker_score: float | None = None
    review_score: int | None = None
    # Filled by merge_related_entries when other newsletters covered the same story.
    also_from: tuple[str, ...] = ()
    merged_summaries: tuple[str, ...] = ()


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


def _entry_rank(entry: DigestEntry) -> tuple[int, float, int, float, int, int, float]:
    return (
        entry.review_score if entry.review_score is not None else -1,
        # Only the headline items carry a review score, so the reranker decides the order of the
        # secondary mentions, which is the order it already ranked them in.
        entry.reranker_score if entry.reranker_score is not None else -1.0,
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


def _absorbed(primary: DigestEntry, other: DigestEntry) -> DigestEntry:
    names = list(primary.also_from)
    for name in (other.source_name, *other.also_from):
        if name and name != primary.source_name and name not in names:
            names.append(name)
    summaries = list(primary.merged_summaries) or [primary.item.summary_zh_tw]
    for summary in (other.item.summary_zh_tw, *other.merged_summaries):
        if summary not in summaries:
            summaries.append(summary)
    return replace(
        # The absorbed entry stops being rendered, so its Hacker News discussion, score, and comment
        # count would be lost with it.
        _preserve_hn_attribution(primary, other),
        also_from=tuple(names),
        merged_summaries=tuple(summaries),
        # A newsletter that only names a story often carries no link while another one does.
        article_url=primary.article_url or other.article_url,
    )


def dedupe_entries(items: list[DigestEntry]) -> list[DigestEntry]:
    """Fold entries that are literally the same story into one, keeping the strongest.

    Two newsletters linking the same canonical article is the least ambiguous repeat coverage there
    is - it needs none of the token heuristics that related-story merging rests on, and so it holds
    in every digest language. It was also the case that lost the most: the loser was dropped here,
    before merging ever ran, so the surviving entry never recorded the other newsletter or its
    wording. Absorbing carries both across, exactly as the related-story merge does.
    """
    # ponytail: one-pass in-memory dedupe; move history lookup to SQLite when volume warrants it.
    winners: dict[str, DigestEntry] = {}
    for item in items:
        key = _entry_key(item)
        current = winners.get(key)
        if current is None:
            winners[key] = item
        elif _entry_rank(item) > _entry_rank(current):
            winners[key] = _absorbed(item, current)
        else:
            winners[key] = _absorbed(current, item)
    return list(winners.values())


# Newsletters translate a headline differently and link to different pages for the same event, so
# neither the canonical URL nor the rendered title identifies a story across sources. Product and
# vendor names survive translation in Latin script, so they carry the identity instead. TLDR's
# section markers are stripped first: two unrelated product launches otherwise share most of their
# tokens, which was the single worst false match when this was measured against a day of real items.
STORY_BOILERPLATE = re.compile(r"\((?:product launch|sponsor|\d+\s*minute read)\)", re.IGNORECASE)
STORY_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9.\-]*")
# Measured over a day of real items: every pair covering the same story shared between two and five
# tokens, and every pair that did not shared at most one, always a bare vendor or topic word such as
# "openai", "deepseek", or "ai". Items carry between 1 and 15 tokens, so a ratio alone is fragile -
# on a short item a single shared vendor name already clears any useful threshold. Requiring two
# distinct shared tokens is what actually separates them; the ratio then guards the long items.
STORY_MINIMUM_SHARED_TOKENS = 2
# Only tokens shaped like an identity count: capitalised in the source, or carrying a version
# number. A zh-TW digest leaves nothing but proper nouns in Latin script, so any Latin token was
# identifying; a digest in a Latin-script language does not, and "OpenAI launches new AI model for
# coding" then shares launches, new, ai, model and for with the same sentence about Google. Filtering
# by document frequency was tried first and does not work: no cutoff both keeps gpt-5.6 and drops
# model, because fifteen items are far too few to infer a stopword list. Shape separates them at
# once - on the measured zh-TW items and on an English set, this takes every true pair and no false
# one. Products that are lowercase in their own name are the known cost of that.
STORY_IDENTITY_TOKEN = re.compile(r"^(?:[A-Z].*|.*\d.*)$")
# Shape keeps ordinary vocabulary out but not a generic acronym: "AI" is capitalised, so two
# unrelated OpenAI products shared "openai" and "ai" and merged at 0.5. Such a word is identifiable
# by being everywhere - across a day's real candidates "ai" appeared in 28.8% of them while the next
# token, a vendor name, appeared in 8.5%. The corpus has to be the whole ranked candidate set: over
# the handful of entries that reach merging, the duplicates being looked for inflate their own
# identifying tokens, and the same measurement inverts to "gpt-5.6" at 26.7% against "ai" at 6.7%.
# The floor keeps a small corpus from treating a true pair's two mentions as common.
STORY_COMMON_TOKEN_FRACTION = 0.15
STORY_COMMON_TOKEN_MINIMUM = 3


def _tokens(text: str) -> set[str]:
    found = STORY_TOKEN.findall(STORY_BOILERPLATE.sub(" ", text))
    return {token.casefold() for token in found if len(token) > 1 and STORY_IDENTITY_TOKEN.match(token)}


def story_tokens(entry: DigestEntry) -> set[str]:
    return _tokens(f"{entry.item.title} {entry.item.summary_zh_tw}")


def common_story_tokens(entries: Sequence[DigestEntry]) -> frozenset[str]:
    """Tokens too widespread in this run to identify anything, such as a domain's own topic word."""
    counts: Counter[str] = Counter()
    for entry in entries:
        counts.update(story_tokens(entry))
    limit = max(STORY_COMMON_TOKEN_MINIMUM, len(entries) * STORY_COMMON_TOKEN_FRACTION)
    return frozenset(token for token, count in counts.items() if count > limit)


def story_similarity(left: DigestEntry, right: DigestEntry, common: frozenset[str] = frozenset()) -> float:
    shared = (story_tokens(left) & story_tokens(right)) - common
    if len(shared) < STORY_MINIMUM_SHARED_TOKENS:
        return 0.0
    # A summary routinely names another story to compare against it: one run had Grok 4.6 described
    # as "matching GPT-5.6 Sol", which shares both of that item's identifying tokens without being
    # the same story at all. Merging them handed the GPT-5.6 headline the Grok article's link, and
    # the headline rewrite then restated the whole item from it. A story's own subject is named in
    # its title, so the titles have to overlap too, not merely the bodies.
    if not ((_tokens(left.item.title) & _tokens(right.item.title)) - common):
        return 0.0
    return len(shared) / len((story_tokens(left) | story_tokens(right)) - common)


def merge_related_entries(
    headlines: list[DigestEntry],
    mentions: list[DigestEntry],
    threshold: float,
    common: frozenset[str] = frozenset(),
) -> tuple[list[DigestEntry], list[DigestEntry]]:
    """Fold repeat coverage of a headline story into that headline, then dedupe the mentions.

    The reviewer already drops duplicates from its own selection, so the copies it rejected land in
    the mention list and reappear under the headline they duplicate. Merging keeps the strongest
    entry and records the other sources, which is worth showing: several newsletters carrying one
    story is itself a signal.
    """
    # story_similarity returns 0.0 as a sentinel for "these are not the same story" - it is what
    # both the two-token and the title-overlap conditions report - so a zero can never be compared
    # against the threshold. Leaving it to ">= threshold" made a threshold of 0 absorb every
    # mention into some headline on no shared identity at all.
    merged = list(headlines)
    remaining: list[DigestEntry] = []
    for mention in mentions:
        scores = [(story_similarity(mention, headline, common), index) for index, headline in enumerate(merged)]
        best, index = max(scores, default=(0.0, -1))
        if index >= 0 and best > 0 and best >= threshold:
            merged[index] = _absorbed(merged[index], mention)
            continue
        remaining.append(mention)
    deduped: list[DigestEntry] = []
    for mention in remaining:
        scores = [(story_similarity(mention, kept, common), index) for index, kept in enumerate(deduped)]
        best, index = max(scores, default=(0.0, -1))
        if index >= 0 and best > 0 and best >= threshold:
            deduped[index] = _absorbed(deduped[index], mention)
            continue
        deduped.append(mention)
    return merged, deduped


LANGUAGE_ALIASES = {
    "zh-tw": "zh-tw",
    "zh-hant": "zh-tw",
    "zh-hk": "zh-tw",
    "zh-mo": "zh-tw",
    "zh-cn": "zh-cn",
    "zh-hans": "zh-cn",
}
SUPPORTED_DIGEST_LANGUAGES = ("zh-tw", "zh-cn", "en")


def digest_language_code(language: str) -> str:
    normalized = language.casefold().replace("_", "-")
    return LANGUAGE_ALIASES.get(normalized, normalized.split("-", maxsplit=1)[0])


def merging_applies(language: str) -> bool:
    """Whether repeat coverage can be identified at all in this digest language.

    Merging identifies a story by the identity-shaped tokens its title and summary keep in Latin
    script, which works because a Chinese digest leaves nothing else in Latin script. An English
    digest leaves ordinary vocabulary there too, and no amount of shaping separates the two: lower
    case was filtered by shape, a bare acronym by frequency, and Title Case defeats both, since
    "OpenAI Launches New AI Model for Coding" and the same sentence about Search then share five
    accepted tokens and score 0.714. Identifying stories across an English digest needs embeddings
    or entity recognition, not another pattern, so merging stays off until that exists.
    """
    return digest_language_code(language) in {"zh-tw", "zh-cn"}


def _labels(language: str) -> dict[str, str]:
    return LABELS.get(digest_language_code(language), NEUTRAL_LABELS)


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
            names = ", ".join((value.source_name, *value.also_from))
            lines.append(f"   {labels['source']}：{sanitize_discord_text(names)}")
        if value.hn_item_id:
            if value.hn_score is not None and value.hn_comments is not None:
                lines.append(f"   {labels['hn']}：{value.hn_score} {labels['points']} · {value.hn_comments} {labels['comments']}")
            if value.content_basis == "metadata":
                lines.append(f"   {labels['metadata']}")
            if value.article_url and value.article_url != value.discussion_url:
                lines.append(f"   {labels['article']}：<{value.article_url}>")
            if value.discussion_url:
                lines.append(f"   {labels['discussion']}：<{value.discussion_url}>")
        elif url := (value.article_url or (str(item.source_url) if item.source_url else None)):
            # article_url may have been borrowed from a merged entry whose newsletter linked the story.
            lines.append(f"   {labels['article']}：<{url}>")
        return "\n".join(lines)

    def mention(value: DigestEntry) -> str:
        """The secondary section is for scanning, so each item is one line without its summary."""
        item = value.item
        parts = [f"• {sanitize_discord_text(item.title)}"]
        if value.source_name:
            parts.append(sanitize_discord_text(", ".join((value.source_name, *value.also_from))))
        url = value.article_url or (str(item.source_url) if item.source_url else None) or value.discussion_url
        if url:
            parts.append(f"<{url}>")
        return " · ".join(parts)

    # Only what the reviewer selected may hold a headline slot. Entries without a review score are
    # the candidates it passed over, so filling spare headline slots from them would republish
    # exactly what the final quality filter rejected. Rendering a plain item list keeps every slot.
    scored = [value for value in eligible if value.review_score is not None]
    top = (scored or eligible)[:top_items]
    rest = eligible[len(top) :]
    sections = [
        f"📰 {safe_topic} 2much2read — {when:%Y-%m-%d}",
        labels["top"] + "\n" + "\n\n".join(entry(item, f"{i}.") for i, item in enumerate(top, 1)),
    ]
    if rest:
        sections.append(labels["rest"] + "\n" + "\n".join(mention(item) for item in rest))
    sections.append(
        f"{labels['processed']}\n{labels['topic']}{safe_topic}\n{labels['sources']}{safe_source_names} · "
        f"{len(eligible)} {labels['valid']}"
    )
    return "\n\n".join(sections)
