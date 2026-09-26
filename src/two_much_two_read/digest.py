from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
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
        "repeat": "🔁 前 {window} 天中有 {days} 天也有報導",
        "repeat_short": "🔁 {days}/{window} 天",
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
        "repeat": "🔁 前 {window} 天中有 {days} 天也有报道",
        "repeat_short": "🔁 {days}/{window} 天",
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
        "repeat": "🔁 Also covered on {days} of the previous {window} days",
        "repeat_short": "🔁 {days}/{window} days",
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
    "repeat": "🔁 {days}/{window}",
    "repeat_short": "🔁 {days}/{window}",
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
    # How many of the previous previous_window days carried the same story, from with_previous_coverage.
    previous_days: int = 0
    previous_window: int = 0


def has_source_text(entry: DigestEntry) -> bool:
    """Whether anything fuller than the extractor's own summary stands behind this entry.

    The article itself - read by the extractor for a Hacker News story, or linked from a newsletter
    and there for the headline rewrite to fetch - or another newsletter's coverage of the same
    story. Without either, the summary is whatever the extractor made of the newsletter's text,
    and for a link list that is the headline alone. A Hacker News post whose body could not be read
    falls back to metadata and stores its discussion page as the article link; that page was never
    read, so it counts for nothing. Nor does a second copy from the same newsletter, which merges
    into merged_summaries as well but is the same text again: only another source's coverage counts,
    and that is what also_from records.
    """
    return entry.content_basis in {"article", "hn_self_post"} or _article_url(entry) is not None or bool(entry.also_from)


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


def _entry_rank(entry: DigestEntry) -> tuple[bool, int, float, int, float, int, int, float]:
    return (
        # Headlines before mentions whatever the score. render_digest takes the mentions as what
        # follows the headlines in this order, so a headline sorting among them would render twice
        # and push a mention out; the security floor gives its headline a score below any the
        # reviewer can give, so that it sorts after every reviewed one.
        entry.review_score is not None,
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


def _article_url(entry: DigestEntry) -> str | None:
    """The entry's article link, or None when what it stores is the discussion page instead."""
    if entry.article_url and canonical_url(entry.article_url) != canonical_url(entry.discussion_url):
        return entry.article_url
    return None


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
        # A newsletter that only names a story often carries no link while another one does. A
        # Hacker News self-post stores its discussion URL here, which is not an article at all, so
        # it does not block borrowing one: keeping it would lose both the link the renderer shows
        # and the fuller text the headline rewrite reads.
        article_url=_article_url(primary) or _article_url(other) or primary.article_url,
        previous_days=max(primary.previous_days, other.previous_days),
        previous_window=max(primary.previous_window, other.previous_window),
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
        elif (has_source_text(item), _entry_rank(item)) > (has_source_text(current), _entry_rank(current)):
            winners[key] = _absorbed(item, current)
        else:
            winners[key] = _absorbed(current, item)
    return list(winners.values())


# Newsletters translate a headline differently and link to different pages for the same event, so
# neither the canonical URL nor the rendered title identifies a story across sources. What survives
# translation is the Latin-script product and vendor names, which makes them a usable shortlist for
# which pairs to ask about. TLDR's section markers are stripped first: they are shared by every item
# in a section and would shortlist a whole newsletter against itself.
STORY_BOILERPLATE = re.compile(r"\((?:product launch|sponsor|\d+\s*minute read)\)", re.IGNORECASE)
STORY_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9.\-]*")
# Capitalised in the source, or carrying a version number. This once tried to be a definition of
# identity, which it cannot be - ordinary English vocabulary is capitalised in a Title Case headline.
# It is now only a cost control on how many pairs reach the model: over real runs it holds the
# shortlist to 0-15 pairs per digest, against 17-108 without it. Missing a product that is lowercase
# in its own name costs a merge rather than causing a wrong one.
STORY_IDENTITY_TOKEN = re.compile(r"^(?:[A-Z].*|.*\d.*)$")


def _tokens(text: str) -> set[str]:
    found = STORY_TOKEN.findall(STORY_BOILERPLATE.sub(" ", text))
    return {token.casefold() for token in found if len(token) > 1 and STORY_IDENTITY_TOKEN.match(token)}


def story_tokens(entry: DigestEntry) -> set[str]:
    return _tokens(f"{entry.item.title} {entry.item.summary_zh_tw}")


def share_a_candidate_token(left: DigestEntry, right: DigestEntry) -> bool:
    """Whether two entries are worth asking a model about. A shortlist, not a decision.

    Token overlap cannot answer whether two items are the same story - six rounds of filtering it
    established that the classes it gets wrong are unbounded, because the distinction is semantic
    rather than lexical. "Claude Code sessions can now talk to each other" and "A Claude Code skill
    was eating 200,000 tokens" share two proper nouns and are unrelated, and no rule over token
    shape or frequency separates that from a real duplicate.

    So overlap only decides which pairs get asked. Precision moved to the model, which frees this to
    be loose: one shared identity-shaped token is enough. Measured over real runs that puts 0 to 15
    pairs in front of the model per digest, typically 2 to 5. Dropping the shape requirement as well
    would raise the worst case to 108, which is why it stays.
    """
    return bool(story_tokens(left) & story_tokens(right))


# Words that pass for identity-shaped tokens in a headline but name nothing.
FUNCTION_WORDS = frozenset(
    {"a", "an", "and", "are", "can", "for", "how", "in", "is", "its", "new", "of", "on", "our", "the", "their", "to"}
    | {"what", "why", "with", "you", "your"}
)
# A product and its version - "Opus 5.5", "GPT-6", "Qwen-Image-2.1", "MiMo v2.6". Stories that run for
# days are nearly always launches or incidents that carry one, and a shared one is specific enough to
# ask about on its own. Bounded by ASCII letters and digits, since CJK counts as a word character.
VERSIONED_NAME = re.compile(r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9]*)[\s-]?v?(\d+(?:\.\d+)?)(?![0-9.])")
# How rare a shared token has to be, in log inverse frequency over the background, for a pair to be
# asked about without a shared versioned name: one token in under ~0.3% of recent items, or two
# moderately rare ones such as "Meta" and "Muse".
REPEAT_SHORTLIST_WEIGHT = 6.0
# Earlier items asked about per entry per day, best first. One was not enough: on 2026-09-23 the best
# match for "Claude Opus 5.5" was a roundup of three launches, rightly judged a different story,
# while three items about Opus 5.5 alone went unasked.
REPEAT_JUDGEMENTS_PER_DAY = 3


def versioned_names(entry: DigestEntry) -> set[str]:
    text = f"{entry.item.title} {entry.item.summary_zh_tw}"
    return {f"{name.casefold()} {version}" for name, version in VERSIONED_NAME.findall(text)}


def with_previous_coverage(
    entries: list[DigestEntry],
    previous: list[tuple[date, DigestEntry]],
    same_story: Callable[[DigestEntry, DigestEntry], bool],
    window: int,
    background: list[set[str]],
) -> list[DigestEntry]:
    """Mark each entry with how many of the previous days also carried its story.

    A story newsletters keep returning to over several days is one that matters, so the count is
    shown to the reader and handed to the reviewer. The same article link settles it outright;
    otherwise the model decides, as same-day merging does, but only for a shortlisted pair.

    The same-day shortlist - any shared identity-shaped token - is everything across three days: on
    the 2026-09-25 run, 40 candidates against 202 earlier items shortlisted 1,433 pairs, mostly on
    "AI". Rarity within the window does not fix it, because a story that repeats makes its own name
    common - "Claude" and "Opus" were exactly what that filtered out. So rarity is measured over a
    longer background, a shared versioned name counts on its own, and each day gets the few earlier
    items that score highest, asked in turn until one agrees. Entries arrive in reranker order, so a
    spent judgement budget costs the weakest candidates their mark first.
    """
    frequency = Counter(token for tokens in background for token in tokens)
    size = max(1, len(background))

    def weight(tokens: set[str]) -> float:
        return sum(math.log(size / (1 + frequency[token])) for token in tokens)

    earlier = [
        (day, other, canonical_url(other.article_url), story_tokens(other) - FUNCTION_WORDS, versioned_names(other))
        for day, other in previous
    ]
    marked: list[DigestEntry] = []
    for entry in entries:
        link = canonical_url(entry.article_url)
        tokens = story_tokens(entry) - FUNCTION_WORDS
        names = versioned_names(entry)
        days = {day for day, _, other_link, _, _ in earlier if link is not None and link == other_link}
        shortlist: dict[date, list[tuple[tuple[int, float], int, DigestEntry]]] = {}
        for position, (day, other, _, other_tokens, other_names) in enumerate(earlier):
            score = (len(names & other_names), weight(tokens & other_tokens))
            if day not in days and (score[0] or score[1] >= REPEAT_SHORTLIST_WEIGHT):
                shortlist.setdefault(day, []).append((score, -position, other))
        for day, candidates in sorted(shortlist.items()):
            best_first = sorted(candidates, key=lambda candidate: candidate[:2], reverse=True)
            if any(same_story(entry, other) for _, _, other in best_first[:REPEAT_JUDGEMENTS_PER_DAY]):
                days.add(day)
        marked.append(replace(entry, previous_days=min(len(days), window), previous_window=window) if days else entry)
    return marked


def merge_related_entries(
    headlines: list[DigestEntry],
    mentions: list[DigestEntry],
    same_story: Callable[[DigestEntry, DigestEntry], bool],
) -> tuple[list[DigestEntry], list[DigestEntry]]:
    """Fold repeat coverage of a headline story into that headline, then dedupe the mentions.

    The reviewer already drops duplicates from its own selection, so the copies it rejected land in
    the mention list and reappear under the headline they duplicate. Merging keeps the strongest
    entry and records the other sources, which is worth showing: several newsletters carrying one
    story is itself a signal.

    Whether two entries are the same story is decided by `same_story`, which the pipeline backs with
    a model. Token overlap only shortlists, so the first pair the model accepts wins rather than the
    highest-scoring one - there is no longer a score to rank by, and a mention that is the same story
    as two different headlines is a contradiction rather than a ranking problem.
    """
    merged = list(headlines)
    remaining: list[DigestEntry] = []
    for mention in mentions:
        index = _first_match(mention, merged, same_story)
        if index is None:
            remaining.append(mention)
            continue
        merged[index] = _absorbed(merged[index], mention)
    deduped: list[DigestEntry] = []
    for mention in remaining:
        index = _first_match(mention, deduped, same_story)
        if index is None:
            deduped.append(mention)
            continue
        kept = deduped[index]
        if has_source_text(mention) and not has_source_text(kept):
            # The one with something behind it becomes primary whichever ranked higher: its summary
            # is the one worth keeping, and its article is the one a reader can open. The story keeps
            # the higher rank, which the list position already reflects - the mention quota cuts in
            # this order, so a lower score here would hold a slot a higher-scoring story is denied.
            scores = [score for score in (kept.reranker_score, mention.reranker_score) if score is not None]
            deduped[index] = replace(_absorbed(mention, kept), reranker_score=max(scores, default=None))
        else:
            deduped[index] = _absorbed(kept, mention)
    return merged, deduped


def _first_match(
    entry: DigestEntry, candidates: list[DigestEntry], same_story: Callable[[DigestEntry, DigestEntry], bool]
) -> int | None:
    for index, candidate in enumerate(candidates):
        if share_a_candidate_token(entry, candidate) and same_story(entry, candidate):
            return index
    return None


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


def _labels(language: str) -> dict[str, str]:
    return LABELS.get(digest_language_code(language), NEUTRAL_LABELS)


def render_digest(
    items: Sequence[DigestItem | DigestEntry],
    when: datetime,
    topic: str,
    source_names: str,
    top_items: int = 5,
    language: str = "zh-TW",
    *,
    reviewed: bool = False,
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
        if value.previous_days:
            lines.append(f"   {labels['repeat'].format(days=value.previous_days, window=value.previous_window)}")
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
        if value.previous_days:
            parts.append(labels["repeat_short"].format(days=value.previous_days, window=value.previous_window))
        url = value.article_url or (str(item.source_url) if item.source_url else None) or value.discussion_url
        if url:
            parts.append(f"<{url}>")
        return " · ".join(parts)

    # Only what the reviewer selected may hold a headline slot. Entries without a review score are
    # the candidates it passed over, so filling spare headline slots from them would republish
    # exactly what the final quality filter rejected. That holds even when nothing is scored: a run
    # that skipped the reviewer because nothing had source text can still merge two bare entries
    # from different newsletters into one with coverage, and nobody reviewed it. Only a plain item
    # list, which never went through a review, fills the slots from what has something behind it;
    # an entry with nothing stays a mention even there.
    scored = [value for value in eligible if value.review_score is not None]
    unreviewed = [] if reviewed else [value for value in eligible if has_source_text(value)]
    top = (scored or unreviewed)[:top_items]
    shown = {id(value) for value in top}
    rest = [value for value in eligible if id(value) not in shown]
    sections = [f"📰 {safe_topic} 2much2read — {when:%Y-%m-%d}"]
    if top:
        sections.append(labels["top"] + "\n" + "\n\n".join(entry(item, f"{i}.") for i, item in enumerate(top, 1)))
    if rest:
        sections.append(labels["rest"] + "\n" + "\n".join(mention(item) for item in rest))
    sections.append(
        f"{labels['processed']}\n{labels['topic']}{safe_topic}\n{labels['sources']}{safe_source_names} · "
        f"{len(eligible)} {labels['valid']}"
    )
    return "\n\n".join(sections)
