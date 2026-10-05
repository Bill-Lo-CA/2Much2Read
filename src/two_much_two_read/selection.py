"""Choosing what a digest shows: loading candidates, ranking, the review, merging, and the security floor."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Protocol
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from two_read_runtime.text import is_inert

from .command_models import (
    SecurityFloorPromotion,
)
from .config import Settings
from .digest import (
    DigestEntry,
    _entry_rank,
    dedupe_entries,
    has_source_text,
    merge_before_selection,
    merge_related_entries,
    story_tokens,
    with_previous_coverage,
)
from .ollama import (
    OllamaSchemaError,
)
from .schemas import (
    DigestItem,
    DigestReview,
)
from .stages import StatusReporter
from .storage import Database

# The category whose reviewer slots are reserved by DIGEST_SECURITY_CANDIDATE_SLOTS, and whose
# stories _with_security_floor keeps in every digest that has one to show.
RESERVED_CATEGORY = "SECURITY"
# Bounded by ASCII letters and digits rather than \b: Python counts CJK as word characters, so \b
# finds no boundary in 修復CVE-2026-77179漏洞, and 10 of the 23 CVE items in the live database are
# written that way.
CVE_PATTERN = re.compile(r"(?<![A-Za-z0-9])CVE-\d{4}-\d{4,}(?![A-Za-z0-9])", re.IGNORECASE)
# Below the 0-100 range DigestReviewSelection allows, so a story the floor promotes sorts after
# every headline the reviewer chose, a zero-scored one included; _entry_rank keeps it a headline.
FLOOR_REVIEW_SCORE = -1


def _items(database: Database, document_ids: list[int], limit: int, source_names: dict[str, str]) -> list[DigestEntry]:
    result: list[DigestEntry] = []
    for row in database.items_for_documents(document_ids, limit):
        item = DigestItem.model_validate(
            {
                "title": row["title"],
                "category": row["category"],
                "summary_zh_tw": row["summary_zh_tw"],
                "why_it_matters_zh_tw": row["why_it_matters_zh_tw"],
                "source_url": row["source_url"],
                "importance": row["importance"],
                "confidence": row["confidence"],
                "tags": json.loads(str(row["tags_json"])),
                "source_title": row.get("source_title"),
            }
        )
        result.append(
            DigestEntry(
                item=item,
                candidate_id=int(str(row["id"])),
                source_type=str(row["source_type"]),
                source_id=str(row["source_id"]),
                source_name=source_names.get(str(row["source_id"]), str(row["source_id"])),
                published_at=datetime.fromisoformat(str(row["published_at"])),
                article_url=str(row["source_url"]) if row["source_url"] else None,
                discussion_url=str(row["discussion_url"]) if row["discussion_url"] else None,
                hn_score=int(str(row["hn_score"])) if row["hn_score"] is not None else None,
                hn_comments=int(str(row["hn_comments"])) if row["hn_comments"] is not None else None,
                hn_item_id=str(row["external_id"]) if row["source_type"] == "hackernews" else None,
                content_basis=str(row["content_basis"]),
            )
        )
    return result


# What each step needs from its collaborators, rather than the whole OllamaClient, reranker or
# Database. Each names only the members its function uses, which lets a test double stand in
# without a cast - and makes mypy check that the double still matches the real thing, which a cast
# would have hidden.
#
# Where a function uses most of an interface - retry_delivery drives a dozen Database methods -
# a Protocol would only be a copy of the class, so those keep the concrete type.
class ReviewsDigest(Protocol):
    def review_digest(
        self,
        candidates: list[dict[str, object]],
        maximum: int,
        reserved_category: str = "",
        reserved: int = 0,
        refill: int = 0,
    ) -> DigestReview: ...


class JudgesSameStory(Protocol):
    def same_story(self, left: dict[str, str], right: dict[str, str]) -> bool: ...


class RanksEntries(Protocol):
    def rank(self, entries: Sequence[DigestEntry]) -> list[DigestEntry]: ...


class NamesRerankerScores(Protocol):
    """The two labels stored beside each score - all that is read from the reranker there."""

    model_name: str
    prompt_version: str


class SavesRerankerScores(Protocol):
    def save_reranker_scores(self, scores: Sequence[tuple[int, float]], model: str, prompt_version: str) -> int: ...


def _ranked_entries(reranker: RanksEntries, entries: list[DigestEntry]) -> list[DigestEntry]:
    if not entries:
        return []
    return reranker.rank(dedupe_entries(entries))


def _save_reranker_scores(
    database: SavesRerankerScores, ranked: list[DigestEntry], reranker: NamesRerankerScores, status: StatusReporter
) -> None:
    scores = [
        (entry.candidate_id, entry.reranker_score)
        for entry in ranked
        if entry.candidate_id is not None and entry.reranker_score is not None
    ]
    if not scores:
        return
    saved = database.save_reranker_scores(scores, reranker.model_name, reranker.prompt_version)
    if saved != len(scores):
        status(f"Warning: recorded {saved} of {len(scores)} reranker scores")


def _review_candidates(ranked: list[DigestEntry], limit: int, security_slots: int) -> list[DigestEntry]:
    """Reserve reviewer slots for security so one global ranking can serve two domains.

    The reranker scores every candidate on a single scale and AI releases consistently outscore
    vulnerability disclosures: across 100 real candidates only 3 of the 23 SECURITY items reached
    the top 20, leaving named CVEs at ranks 35 and 43. Steering the reranker prompt toward
    vulnerabilities lifts those but pushes AI and tooling stories out by as much, so the split
    happens here instead, on the ranking that discriminates best overall. Either group takes the
    other's unused slots, and the kept entries stay in reranker order.
    """
    security = [index for index, entry in enumerate(ranked) if entry.item.category == RESERVED_CATEGORY]
    general = [index for index, entry in enumerate(ranked) if entry.item.category != RESERVED_CATEGORY]
    security_kept = min(security_slots, limit, len(security))
    general_kept = min(limit - security_kept, len(general))
    security_kept = min(len(security), limit - general_kept)
    return [ranked[index] for index in sorted(security[:security_kept] + general[:general_kept])]


# Picks past the headline limit, so a slot the per-source cap frees goes to the reviewer's next choice
# rather than staying empty. Beyond the limit they are ordinary mentions. The reviewer asks for fewer,
# or none, when they would cost it a candidate; see OllamaClient.review_digest.
REVIEW_REFILL_PICKS = 5


def _reviewed_entries(settings: Settings, ollama: ReviewsDigest, ranked: list[DigestEntry]) -> list[DigestEntry]:
    if not ranked:
        return []
    # An item with no article and no other coverage may be a mention but never a headline: its
    # summary is whatever the extractor made of the newsletter's text, the rewrite has nothing
    # fuller to work from, and on 2026-09-25 two of the first six headlines were one-line guesses
    # from Hacker Newsletter's bare titles. So it is kept from the reviewer, whose picks become
    # headlines, and rejoins the passed-over candidates below.
    everything = ranked
    ranked = _review_candidates(
        [entry for entry in everything if has_source_text(entry)],
        settings.digest_review_candidate_limit,
        settings.digest_security_candidate_slots,
    )
    if not ranked:
        return list(everything)
    candidates: list[dict[str, object]] = []
    for entry in ranked:
        assert entry.candidate_id is not None
        candidates.append(
            {
                "candidate_id": entry.candidate_id,
                "title": entry.item.title,
                "category": entry.item.category,
                "summary": entry.item.summary_zh_tw,
                "why_it_matters": entry.item.why_it_matters_zh_tw,
                # Every newsletter carrying the story, merged before the review: coverage by several
                # is a sign it matters, as previous_days is.
                "source": ", ".join(name for name in (entry.source_name, *entry.also_from) if name),
                **({"previous_days": entry.previous_days} if entry.previous_days else {}),
            }
        )
    # The context fitter trims from the tail, where the reserved candidates sit by design.
    # The reviewer is released by the caller, once merging and the headline rewrite have also
    # finished with it: both run on this model, and nothing else loads in between.
    review = ollama.review_digest(
        candidates,
        settings.digest_max_items,
        RESERVED_CATEGORY,
        settings.digest_security_candidate_slots,
        REVIEW_REFILL_PICKS if settings.digest_headlines_per_source else 0,
    )
    scores = {selection.candidate_id: selection.score for selection in review.selected}
    selected = [replace(entry, review_score=scores[entry.candidate_id]) for entry in ranked if entry.candidate_id in scores]
    # The reviewer only picks the headline items, but the candidates behind them were already
    # extracted, ranked, and paid for. They carry no review score, so they sort below every
    # selected item and render as the digest's secondary mentions. The secondary limit is applied
    # after merging, so a mention absorbed into a headline frees its slot for the next candidate.
    reviewed = {id(entry) for entry in ranked}
    unselected = [
        entry
        for entry in everything
        if entry.candidate_id not in scores and (id(entry) in reviewed or not has_source_text(entry))
    ]
    return selected + unselected


# How far back token rarity is measured for the repeat shortlist: long enough that a story's own
# name is not common merely because the story is running this week.
REPEAT_BACKGROUND_DAYS = 30


def _row_entry(row: dict[str, object], source_names: dict[str, str]) -> DigestEntry:
    """An earlier run's item, as much of an entry as story matching needs."""
    return DigestEntry(
        item=DigestItem.model_validate(
            {
                "title": row["title"],
                "category": row["category"],
                "summary_zh_tw": row["summary_zh_tw"],
                "why_it_matters_zh_tw": row["why_it_matters_zh_tw"],
                "importance": row["importance"],
                "confidence": row["confidence"],
            }
        ),
        candidate_id=int(str(row["id"])),
        source_id=str(row["source_id"]),
        source_name=source_names.get(str(row["source_id"]), str(row["source_id"])),
        article_url=str(row["source_url"]) if row["source_url"] else None,
    )


RepeatMarker = Callable[[list[DigestEntry], Callable[[DigestEntry], bool]], list[DigestEntry]]


def _repeat_marker(
    settings: Settings,
    database: Database,
    ollama: JudgesSameStory,
    now: datetime,
    current_documents: list[int],
    source_names: dict[str, str],
    status: StatusReporter,
) -> RepeatMarker:
    """Mark entries with how many of the previous days' newsletters also carried their story.

    Earlier runs' items stand for what newsletters covered on those days, counted by the day each
    newsletter arrived in the digest's timezone. Only whole days before today count: this run's
    own documents are left out so a story never repeats itself, and so is anything another run
    stored earlier today, which is the same day however many runs it took.

    The marker is applied twice - to the reviewer's candidates before the review, which it is told
    about, and to everything shown once merging is done, since merging frees mention slots and
    which mentions fill them is not known before. An entry is judged once, and both passes share
    one judgement budget.
    """
    window = settings.digest_repeat_window_days
    zone = ZoneInfo(settings.digest_timezone)
    today = datetime.combine(now.astimezone(zone).date(), datetime.min.time(), zone)
    previous: list[tuple[date, DigestEntry]] = []
    background: list[set[str]] = []
    if window > 0:
        previous = [
            (datetime.fromisoformat(str(row["published_at"])).astimezone(zone).date(), _row_entry(row, source_names))
            for row in database.items_between(today - timedelta(days=window), today, current_documents)
        ]
    if previous:
        background = [
            story_tokens(_row_entry(row, source_names))
            for row in database.items_between(today - timedelta(days=REPEAT_BACKGROUND_DAYS), today, current_documents)
        ]
    judge = _story_judge(ollama, settings.digest_repeat_judgements, status)
    checked: set[int] = set()

    def mark(entries: list[DigestEntry], wanted: Callable[[DigestEntry], bool]) -> list[DigestEntry]:
        targets = [entry for entry in entries if wanted(entry) and entry.candidate_id not in checked]
        if not previous or not targets:
            return entries
        checked.update(entry.candidate_id for entry in targets if entry.candidate_id is not None)
        status(f"Checking {len(targets)} candidates against {len(previous)} items from the previous {window} days")
        marked = {
            id(entry): value
            for entry, value in zip(targets, with_previous_coverage(targets, previous, judge, window, background), strict=True)
        }
        return [marked.get(id(entry), entry) for entry in entries]

    return mark


class ReviewsAndJudges(ReviewsDigest, JudgesSameStory, Protocol):
    pass


def _selected_entries(
    settings: Settings, ollama: ReviewsAndJudges, ranked: list[DigestEntry], mark_repeats: RepeatMarker, status: StatusReporter
) -> tuple[list[DigestEntry], SecurityFloorPromotion | None]:
    """Merge, review, and apply the security floor, marking repeats on what the reviewer and reader see.

    Merging comes first, over what may be shown, so the reviewer picks among distinct stories. The
    merge after the review still runs, on the same judge and budget; the pairs it asks were answered
    before the review, so it is a safety net that costs nothing.
    """
    same_story = _story_judge(ollama, settings.digest_merge_judgements, status)

    def reviewer_candidates(entries: list[DigestEntry]) -> list[DigestEntry]:
        return _review_candidates(
            [entry for entry in entries if has_source_text(entry)],
            settings.digest_review_candidate_limit,
            settings.digest_security_candidate_slots,
        )

    def may_be_shown(entries: list[DigestEntry]) -> list[DigestEntry]:
        # The reviewer's candidates, whose picks are the headlines and the rest mentions, and the
        # entries without source text that lead the mentions.
        bare = [entry for entry in entries if not has_source_text(entry)]
        return reviewer_candidates(entries) + bare[: settings.digest_secondary_items]

    # Marked before merging as well as after: a repeat found only through a report that folds into
    # another would be lost, since the survivor's own words and link are all later marking reads.
    # Folding keeps the larger count. The marker judges an entry once, so the second pass only
    # reaches candidates a fold let in.
    reviewing = {id(entry) for entry in reviewer_candidates(ranked)}
    ranked = mark_repeats(ranked, lambda entry: id(entry) in reviewing)
    ranked = merge_before_selection(ranked, may_be_shown, same_story)
    reviewing = {id(entry) for entry in reviewer_candidates(ranked)}
    ranked = mark_repeats(ranked, lambda entry: id(entry) in reviewing)
    merged, security_floor = _merged_entries(
        _reviewed_entries(settings, ollama, ranked),
        settings.digest_secondary_items,
        same_story,
        headline_limit=min(settings.digest_max_items, settings.digest_top_items),
        per_source=settings.digest_headlines_per_source,
        picks=settings.digest_max_items,
    )
    return mark_repeats(merged, lambda _entry: True), security_floor


def _story_judge(ollama: JudgesSameStory, budget: int, status: StatusReporter) -> Callable[[DigestEntry, DigestEntry], bool]:
    """Ask the review model whether two shortlisted entries are the same story.

    Bounded and memoised, because the shortlist is loose by design. The budget caps how many
    generations one digest may spend on merging; past it nothing merges, which loses an attribution
    rather than producing a wrong one. A model or transport failure is answered no for the same
    reason: not merging is the safe direction.
    """
    answers: dict[frozenset[int | None], bool] = {}
    spent = 0

    def judge(left: DigestEntry, right: DigestEntry) -> bool:
        nonlocal spent
        # Either way round: merging before the review asks a pair one way, and the merge after it
        # may ask the same pair the other way.
        key = frozenset((left.candidate_id, right.candidate_id))
        identified = None not in key
        if identified and key in answers:
            return answers[key]
        if spent >= budget:
            return False
        spent += 1
        try:
            same = ollama.same_story(_judged(left), _judged(right))
        except (OllamaSchemaError, httpx.HTTPError) as error:
            status(f"Warning: could not compare {left.item.title} ({type(error).__name__}); left unmerged")
            same = False
        if identified:
            answers[key] = same
        return same

    return judge


def _judged(entry: DigestEntry) -> dict[str, str]:
    return {"title": entry.item.title, "summary": entry.item.summary_zh_tw, "source": entry.source_name or ""}


def _merged_entries(
    entries: list[DigestEntry],
    secondary_items: int,
    same_story: Callable[[DigestEntry, DigestEntry], bool],
    *,
    headline_limit: int | None = None,
    per_source: int = 0,
    picks: int | None = None,
) -> tuple[list[DigestEntry], SecurityFloorPromotion | None]:
    """Merge repeat coverage, apply the security floor when headline_limit is given, then cap mentions.

    With per_source as well, the headlines are the reviewer's best picks up to headline_limit with at
    most per_source from any one newsletter. The picks after them, up to picks in all (the
    reviewer's own quota, headline_limit when not given), stay picks; a pick over the cap, and one
    past that quota, becomes a mention, ranked as one.

    Returns the entries, and what the floor promoted if it had to act. secondary_items caps the
    candidates the reviewer passed over; headlines the floor demoted were the reviewer's choices and
    sit outside it, as the ones past the render cap already did before it acted.
    """
    # Taken before merging: an entry with source text now is one the reviewer saw. Merging can give a
    # bare entry another newsletter's coverage, but that coverage arrived after the review did.
    reviewable = {entry.candidate_id for entry in entries if has_source_text(entry) and entry.candidate_id is not None}
    headlines = [entry for entry in entries if entry.review_score is not None]
    mentions = [entry for entry in entries if entry.review_score is None]
    headlines, mentions = merge_related_entries(headlines, mentions, same_story)
    demoted: list[DigestEntry] = []
    promotion = None
    if headline_limit is not None and per_source > 0:
        headlines, overflow = _headlines_per_source(headlines, max(headline_limit, picks or 0), per_source)
        mentions = sorted([*overflow, *mentions], key=_entry_rank, reverse=True)
    if headline_limit is not None:
        headlines, demoted, mentions, promotion = _with_security_floor(
            headlines, mentions, headline_limit, secondary_items, reviewable
        )
    return headlines + demoted + mentions[:secondary_items], promotion


def _headlines_per_source(
    headlines: list[DigestEntry], picks: int, per_source: int
) -> tuple[list[DigestEntry], list[DigestEntry]]:
    """The best picks, at most per_source from one newsletter and picks in all, and the rest as mentions.

    A newsletter that covers one theme in depth hands the reviewer several strong candidates at once,
    and the reviewer rates each on its own: Console's tool list took three of ten headlines, one of
    them the newsletter describing itself. The cap keeps its best two, and the reviewer's next picks
    fill the slots. A pick left out stays in the digest as a mention, ranked by the reranker as every
    mention is.

    With DIGEST_MAX_ITEMS above DIGEST_TOP_ITEMS the picks after the headlines shown are listed past
    them, outside the mention quota, and the cap leaves the ones within it where they were. It holds
    there as well: a newsletter's weaker pick keeps no place its better one lost. The refill
    picks past DIGEST_MAX_ITEMS were asked for only to fill freed slots, and are mentions.
    """
    kept: list[DigestEntry] = []
    overflow: list[DigestEntry] = []
    taken: Counter[str | None] = Counter()
    for entry in sorted(headlines, key=_entry_rank, reverse=True):
        source = _cap_key(entry)
        if len(kept) < picks and taken[source] < per_source:
            kept.append(entry)
            taken[source] += 1
        else:
            overflow.append(replace(entry, review_score=None))
    return kept, overflow


def _cap_key(entry: DigestEntry) -> str | None:
    """Whom a headline counts against for the per-source cap.

    A newsletter, for its own items. A Hacker News story instead counts against the site it links:
    every story in hn-best shares one source ID, but the stories come from unrelated publishers, and
    counting them as one newsletter would hold the whole feed to two headlines. A post with no article
    of its own counts alone.
    """
    if entry.source_type != "hackernews":
        return entry.source_id or entry.source_name
    article = entry.article_url if entry.article_url != entry.discussion_url else None
    hostname = urlsplit(article).hostname if article else None
    return f"site:{hostname.removeprefix('www.')}" if hostname else f"hn:{entry.hn_item_id}"


def _inert(value: str) -> str:
    """Model-written text made safe to print: the promotion record reaches stdout and the journal.

    JSON escapes only U+0000-U+001F, and the CLI writes with ensure_ascii=False, so a C1 control such
    as U+009B - which terminals treat as ESC [ - or a bidirectional override would reach the
    terminal as it is. Same substitution as the progress line, so words do not run together.
    """
    return " ".join("".join(character if is_inert(character, keep="") else " " for character in value).split())


def _is_cve(entry: DigestEntry) -> bool:
    item = entry.item
    return any(CVE_PATTERN.search(text) for text in (item.title, item.summary_zh_tw, item.why_it_matters_zh_tw))


def _with_security_floor(
    headlines: list[DigestEntry],
    mentions: list[DigestEntry],
    headline_limit: int,
    secondary_items: int,
    reviewable: set[int],
) -> tuple[list[DigestEntry], list[DigestEntry], list[DigestEntry], SecurityFloorPromotion | None]:
    """Make sure the digest shows at least one security story.

    The reviewer picks headlines on one scale, and on a day of big AI releases security loses: on
    2026-09-24 thirteen SECURITY candidates reached the reviewer and a Meta Muse 0-day it rated 9 of
    10 was left out. The reserved reviewer slots only put security in front of the reviewer; this
    is the floor on what it hands back.

    A security headline satisfies it, and so does a CVE among the mentions that will be shown: a
    CVE is a one-line fact, and a mention is where it belongs. Otherwise a security story takes the
    last headline slot, and the headline it displaces becomes a mention. The reviewer's own choice
    comes first: with DIGEST_MAX_ITEMS above DIGEST_TOP_ITEMS it can select a security story that
    ranks past the headlines render_digest shows, and promoting a rejected one over it would
    overrule the reviewer to satisfy a rule it already met. Failing that, the best security
    mention in reranker order. Headlines past headline_limit become mentions too, which is where
    render_digest would have put them, so that the promoted story is the last headline shown rather
    than one the renderer cuts. Both are returned apart from the passed-over mentions, so that they
    stay outside DIGEST_SECONDARY_ITEMS rather than using up the quota meant for those. A pool with
    no security story is left alone.

    So is a digest the reviewer chose nothing for: the floor keeps security among the reviewer's
    headlines, and there are none to keep it among. Only a story the reviewer saw may be promoted -
    one of its passed-over candidates, never an entry that gained coverage by merging afterwards.
    """
    if not headlines:
        return headlines, [], mentions, None
    ordered = sorted(headlines, key=_entry_rank, reverse=True)
    visible, hidden = ordered[:headline_limit], ordered[headline_limit:]
    if headline_limit <= 0 or any(entry.item.category == RESERVED_CATEGORY for entry in visible):
        return headlines, [], mentions, None
    if any(entry.item.category == RESERVED_CATEGORY and _is_cve(entry) for entry in mentions[:secondary_items]):
        return headlines, [], mentions, None
    promoted = next(
        (
            entry
            for entry in [*hidden, *mentions]
            if entry.item.category == RESERVED_CATEGORY and entry.candidate_id in reviewable
        ),
        None,
    )
    if promoted is None:
        return headlines, [], mentions, None
    kept = visible[: headline_limit - 1]
    # Only a shown headline counts as displaced: those past the limit were mentions either way.
    promotion = SecurityFloorPromotion(
        promoted=_inert(promoted.item.title),
        source=_inert(promoted.source_name or promoted.source_id or "") or None,
        displaced=_inert(visible[headline_limit - 1].item.title) if len(visible) == headline_limit else None,
    )
    kept_ids = {id(entry) for entry in [*kept, promoted]}
    demoted = [replace(entry, review_score=None) for entry in ordered if id(entry) not in kept_ids]
    return (
        [*kept, replace(promoted, review_score=FLOOR_REVIEW_SCORE)],
        demoted,
        [entry for entry in mentions if entry is not promoted],
        promotion,
    )
