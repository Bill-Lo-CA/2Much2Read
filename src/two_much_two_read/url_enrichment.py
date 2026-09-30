from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, TypeAlias, cast
from urllib.parse import parse_qsl, urlsplit

from pydantic import HttpUrl, ValidationError

from .article_fetcher import ArticleFetcher, ResolvedUrl, UrlResolutionError
from .digest import canonical_url
from .mime import unsubscribe_link
from .schemas import HTTP_URL, DigestItem, ItemAnalysis, LinkCandidate, NewsletterItemAnalysis

MatchedBy: TypeAlias = Literal["model_link", "exact_anchor", "heading_context", "fuzzy_anchor", "url_slug"]
MatchMethod: TypeAlias = Literal[
    "model_link", "exact_anchor", "heading_context", "fuzzy_anchor", "url_slug", "unmatched", "ambiguous"
]
TRACKING_PARAMETERS = {"mc_cid", "mc_eid", "mkt_tok"}


@dataclass(frozen=True)
class UrlMatch:
    item: NewsletterItemAnalysis
    candidate: LinkCandidate | None
    method: MatchMethod
    confidence: float


def _text(value: str) -> str:
    return " ".join(re.findall(r"[\w]+", value.casefold()))


def _tokens(value: str) -> set[str]:
    return set(_text(value).split())


def _similarity(left: set[str], right: set[str]) -> float:
    return len(left & right) / len(left | right)


def _score(title: str, candidate: LinkCandidate) -> tuple[MatchMethod, float]:
    normalized_title = _text(title)
    anchor = _text(candidate.anchor_text)
    nearby = _text(candidate.nearby_text)
    if anchor == normalized_title:
        return "exact_anchor", 1.0
    if normalized_title and normalized_title in nearby:
        return "heading_context", 0.9
    title_tokens = _tokens(title)
    if not title_tokens:
        return "unmatched", 0.0
    anchor_score = _similarity(title_tokens, _tokens(candidate.anchor_text))
    if anchor_score >= 0.7:
        return "fuzzy_anchor", anchor_score
    slug_tokens = _tokens(urlsplit(str(candidate.raw_url)).path.replace("-", " "))
    slug_score = _similarity(title_tokens, slug_tokens)
    return ("url_slug", slug_score) if slug_score >= 0.7 else ("unmatched", 0.0)


def _http_url(value: str | None) -> HttpUrl | None:
    """The value as a URL, or None: stripping parameters can re-encode a URL past HttpUrl's length."""
    if not value:
        return None
    try:
        return HTTP_URL.validate_python(value)
    except ValidationError:
        return None


def _tracking_url(value: str) -> bool:
    parts = urlsplit(value)
    hostname = (parts.hostname or "").casefold()
    query_keys = {key.casefold() for key, _ in parse_qsl(parts.query, keep_blank_values=True)}
    path = parts.path.casefold()
    return (
        "/e3t/" in path
        # HubSpot's second hop, should a resolution ever stop on it.
        or "/events/public/v1/encoded/track/" in path
        or "hubspot" in hostname
        or "mailchimp" in hostname
        or bool(query_keys & TRACKING_PARAMETERS)
    )


def shown_url(resolved: ResolvedUrl) -> str | None:
    """The link a digest shows for a resolution, or None when all it has is a tracker.

    The page's name for itself first, then where the chain ended, each without campaign tags or the
    reader's identity. The tracker test runs on that cleaned link, not the raw one: Mailchimp tags
    the article's own address with mc_cid, which says nothing about the page, while a click page
    that names itself as its canonical is still a click page. Nor is an unsubscribe page ever shown
    as an article: a tracker's link with an unknown label may still lead to one.
    """
    if any(url and unsubscribe_link(url) for url in (resolved.canonical_url, resolved.final_url)):
        return None
    for candidate in (resolved.canonical_url, resolved.final_url):
        shown = canonical_url(candidate)
        if shown is not None and not _tracking_url(shown):
            return shown
    return None


class UrlEnricher:
    """Matches application-supplied links and turns safe resolutions into digest items."""

    def match(
        self, items: Sequence[NewsletterItemAnalysis], candidates: Sequence[LinkCandidate], offered: str | None = None
    ) -> list[UrlMatch]:
        """Pair each item with its link. offered is the text the extractor read, when known.

        A code that text never showed was made up, however well it names a candidate: given a plain
        part with no links in it, the extractor still answered L1 and L2, and those were the HTML's
        first links - advertisements. Such a code falls through to the title like any other.
        """
        available = [candidate for candidate in candidates if candidate.kind != "non_article"]
        shown = None if offered is None else set(re.findall(r"\[(L\d+)\]", offered))
        by_code = {candidate.code: candidate for candidate in available if shown is None or candidate.code in shown}
        used: set[str] = set()
        matches: list[UrlMatch] = []
        for item in items:
            # The extractor's own answer comes first: it read the item beside its links, and so can
            # tell an article link from the comments link next to it, where the title scores both
            # alike and the match is abandoned as ambiguous. A code that names no candidate, or one
            # another item already took, falls through to the title.
            coded = by_code.get(item.link) if item.link else None
            if coded is not None and coded.candidate_id not in used:
                used.add(coded.candidate_id)
                matches.append(UrlMatch(item, coded, "model_link", 1.0))
                continue
            # Score against the newsletter's own wording, not the translated display title.
            scored = [
                (_score(item.source_title, candidate), candidate) for candidate in available if candidate.candidate_id not in used
            ]
            scored = [(score, candidate) for score, candidate in scored if score[1] > 0]
            if not scored:
                matches.append(UrlMatch(item, None, "unmatched", 0.0))
                continue
            scored.sort(key=lambda value: (-value[0][1], value[1].position, value[1].candidate_id))
            (method, confidence), candidate = scored[0]
            if len(scored) > 1 and round(confidence - scored[1][0][1], 6) < 0.1:
                matches.append(UrlMatch(item, None, "ambiguous", confidence))
                continue
            used.add(candidate.candidate_id)
            matches.append(UrlMatch(item, candidate, method, confidence))
        return matches

    def resolved_item(self, match: UrlMatch, resolved: ResolvedUrl) -> DigestItem:
        return self._item(match, resolved=resolved)

    def failed_item(self, match: UrlMatch, error_code: str) -> DigestItem:
        return self._item(match, error_code=error_code)

    def _item(self, match: UrlMatch, resolved: ResolvedUrl | None = None, error_code: str | None = None) -> DigestItem:
        values = {name: getattr(match.item, name) for name in ItemAnalysis.model_fields}
        if match.candidate is None:
            match_status = cast(Literal["unmatched", "ambiguous"], match.method)
            return DigestItem(
                **values,
                url_match_status=match_status,
                url_match_confidence=match.confidence,
                url_resolution_status="not_requested",
            )
        raw_url = str(match.candidate.raw_url)
        if resolved is not None:
            return DigestItem(
                **values,
                source_url=_http_url(shown_url(resolved)),
                raw_url=HTTP_URL.validate_python(raw_url),
                resolved_url=HTTP_URL.validate_python(resolved.final_url),
                canonical_url=HTTP_URL.validate_python(resolved.canonical_url) if resolved.canonical_url else None,
                url_match_status="matched",
                url_match_method=cast(MatchedBy, match.method),
                url_match_confidence=match.confidence,
                url_resolution_status="resolved",
                url_checked_at=datetime.now(UTC),
            )
        blocked = error_code in {"URL_POLICY_BLOCKED", "URL_REDIRECT_BLOCKED"}
        return DigestItem(
            **values,
            raw_url=HTTP_URL.validate_python(raw_url),
            url_match_status="matched",
            url_match_method=cast(MatchedBy, match.method),
            url_match_confidence=match.confidence,
            url_resolution_status="blocked" if blocked else "failed",
            url_error_code=error_code,
            url_checked_at=datetime.now(UTC),
        )


def resolve_match(match: UrlMatch, fetcher: ArticleFetcher) -> ResolvedUrl:
    if match.candidate is None:
        raise UrlResolutionError("URL_MATCH_UNRESOLVED")
    return fetcher.resolve_url(str(match.candidate.raw_url))
