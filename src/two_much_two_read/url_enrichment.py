from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, TypeAlias, cast
from urllib.parse import parse_qsl, urlsplit

from .article_fetcher import ArticleFetcher, ResolvedUrl, UrlResolutionError
from .digest import canonical_url
from .schemas import HTTP_URL, DigestItem, ItemAnalysis, LinkCandidate

MatchMethod: TypeAlias = Literal["exact_anchor", "heading_context", "fuzzy_anchor", "url_slug", "unmatched", "ambiguous"]
TRACKING_PARAMETERS = {"mc_cid", "mc_eid", "mkt_tok"}


@dataclass(frozen=True)
class UrlMatch:
    item: ItemAnalysis
    candidate: LinkCandidate | None
    method: MatchMethod
    confidence: float


def _text(value: str) -> str:
    return " ".join(re.findall(r"[\w]+", value.casefold()))


def _tokens(value: str) -> set[str]:
    return set(_text(value).split())


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
    anchor_score = len(title_tokens & _tokens(candidate.anchor_text)) / len(title_tokens)
    if anchor_score >= 0.7:
        return "fuzzy_anchor", anchor_score
    slug_tokens = _tokens(urlsplit(str(candidate.raw_url)).path.replace("-", " "))
    slug_score = len(title_tokens & slug_tokens) / len(title_tokens)
    return ("url_slug", slug_score) if slug_score >= 0.7 else ("unmatched", 0.0)


def _tracking_url(value: str) -> bool:
    parts = urlsplit(value)
    hostname = (parts.hostname or "").casefold()
    query_keys = {key.casefold() for key, _ in parse_qsl(parts.query, keep_blank_values=True)}
    return (
        "/e3t/" in parts.path.casefold()
        or "hubspot" in hostname
        or "mailchimp" in hostname
        or bool(query_keys & TRACKING_PARAMETERS)
    )


class UrlEnricher:
    """Matches application-supplied links and turns safe resolutions into digest items."""

    def match(self, items: Sequence[ItemAnalysis], candidates: Sequence[LinkCandidate]) -> list[UrlMatch]:
        available = [candidate for candidate in candidates if candidate.kind != "non_article"]
        used: set[str] = set()
        matches: list[UrlMatch] = []
        for item in items:
            scored = [(_score(item.title, candidate), candidate) for candidate in available if candidate.candidate_id not in used]
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
            display = canonical_url(resolved.canonical_url or resolved.final_url)
            if resolved.canonical_url is None and _tracking_url(resolved.final_url):
                display = None
            return DigestItem(
                **values,
                source_url=HTTP_URL.validate_python(display) if display else None,
                raw_url=HTTP_URL.validate_python(raw_url),
                resolved_url=HTTP_URL.validate_python(resolved.final_url),
                canonical_url=HTTP_URL.validate_python(resolved.canonical_url) if resolved.canonical_url else None,
                url_match_status="matched",
                url_match_method=cast(Literal["exact_anchor", "heading_context", "fuzzy_anchor", "url_slug"], match.method),
                url_match_confidence=match.confidence,
                url_resolution_status="resolved",
                url_checked_at=datetime.now(UTC),
            )
        blocked = error_code in {"URL_POLICY_BLOCKED", "URL_REDIRECT_BLOCKED"}
        return DigestItem(
            **values,
            raw_url=HTTP_URL.validate_python(raw_url),
            url_match_status="matched",
            url_match_method=cast(Literal["exact_anchor", "heading_context", "fuzzy_anchor", "url_slug"], match.method),
            url_match_confidence=match.confidence,
            url_resolution_status="blocked" if blocked else "failed",
            url_error_code=error_code,
            url_checked_at=datetime.now(UTC),
        )


def resolve_match(match: UrlMatch, fetcher: ArticleFetcher) -> ResolvedUrl:
    if match.candidate is None:
        raise UrlResolutionError("URL_MATCH_UNRESOLVED")
    return fetcher.resolve_url(str(match.candidate.raw_url))
