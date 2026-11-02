"""Turning one source's new documents into stored items: Gmail newsletters and Hacker News stories."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parseaddr
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from .article_extractor import ArticleExtractionError
from .article_fetcher import ArticleFetcher, ArticleFetchError, ResolvedUrl, UrlResolutionError
from .config import GmailSource, HackerNewsSource, Settings
from .digest import (
    canonical_url,
)
from .gmail import GmailClient, error_name, message_headers
from .hackernews import HackerNewsCandidate, HackerNewsClient, HackerNewsError, resolve_hackernews_candidate
from .mime import MAX_ANALYSIS_CHARS, EmailExtractionError, extract_gmail_payload
from .ollama import (
    OllamaClient,
    OllamaContextError,
    OllamaSchemaError,
)
from .schemas import (
    ArticleAnalysis,
    DigestItem,
    EmailExtraction,
    ExtractedEmailContent,
    ResolvedContent,
)
from .stages import StatusReporter
from .storage import Database
from .url_enrichment import UrlEnricher, resolve_match, shown_url

logger = logging.getLogger(__name__)


def _email_content(value: ExtractedEmailContent | str) -> ExtractedEmailContent:
    return value if isinstance(value, ExtractedEmailContent) else ExtractedEmailContent(analysis_text=value)


class AnalyzesArticles(Protocol):
    def analyze_article(
        self,
        source_id: str,
        hn_item_id: int,
        title: str,
        score: int,
        comments: int,
        published_at: str,
        content_basis: str,
        content: str,
        truncated: bool = False,
    ) -> ArticleAnalysis: ...


def _sync_processing_label(database: Database, gmail: GmailClient, gmail_id: str, document_id: int, state: str) -> bool:
    try:
        gmail.sync_processing_label(gmail_id, state)
    except Exception as error:
        # The row records GMAIL_LABEL_SYNC_FAILED and nothing of why, so the reason is logged: a
        # message deleted since will fail every retry, where an outage passes.
        logger.warning("could not label document %d %s: %s", document_id, state, error_name(error))
        database.fail_label_sync(document_id)
        return False
    database.mark_label_synced(document_id)
    return True


def _sender_domain(from_header: str) -> str | None:
    """The domain of the address a newsletter is sent from, as the header names it."""
    domain = parseaddr(from_header)[1].rpartition("@")[2].strip().lower().removeprefix("www.")
    return domain or None


def _links_front_page_of(item: DigestItem, sender: str | None) -> bool:
    """Whether the item's article is the sending newsletter's own front page.

    That is the newsletter describing itself - Console's "this week's best developer tools" linked
    console.dev and took a headline - not a story it carries. Only the sender's own site counts: a
    tool newsletter's items link tools' front pages, such as droprun.sh, and those are the stories.
    """
    link = item.source_url or item.raw_url
    if sender is None or link is None:
        return False
    parts = urlsplit(canonical_url(str(link)) or "")
    host = (parts.hostname or "").lower().removeprefix("www.")
    same_site = host == sender or host.endswith(f".{sender}") or sender.endswith(f".{host}")
    return bool(host) and same_site and parts.path in {"", "/"} and not parts.query


EXTRACT_TIMEOUT = "OLLAMA_EXTRACT_TIMEOUT"


class ExtractionTimeoutError(ValueError):
    """An email's extraction outran OLLAMA_TIMEOUT_SECONDS for the first time, which ends the run; see _extraction."""


def _extraction(
    ollama: OllamaClient,
    database: Database,
    source: GmailSource,
    document_id: int,
    body: str,
    truncated: bool,
    status: StatusReporter,
    *,
    timed_out_before: bool,
) -> EmailExtraction | str:
    """The email's extraction, or the code it failed with.

    A timeout is not taken for the email's fault at first. Ollama stalling would time out every
    email in turn, and a failed email is labelled Failed and never read again, so the first ends
    the run - as Ollama being unreachable does - and the next run reads everything again. But an
    email that outruns the timeout by itself, with the model repeating itself until the context is
    full, would then end every run for as long as it stays within GMAIL_LOOKBACK_DAYS. So the
    timeout is recorded against the email, and the next run gives it two tries in a row. If both
    time out and the model still answers a one-token probe, the email is what it cannot finish: it
    fails alone and the run goes on. A model that answers nothing has stalled again, or still - a
    stall over two runs would otherwise pass over one sound email a day - so the run ends as at
    first, and the record stands for the run after.

    Only a read timeout counts. Ollama answering nothing at all - refused, or a connect timeout -
    says nothing about the email, and still ends the run every time.
    """
    tries = 2 if timed_out_before else 1
    for attempt in range(1, tries + 1):
        try:
            return ollama.extract(source.id, body, truncated, source.max_items_per_email)
        except (OllamaContextError, OllamaSchemaError) as error:
            return str(error).split(" response_preview=", 1)[0]
        except httpx.ReadTimeout as error:
            if not timed_out_before:
                database.record_document_error(document_id, EXTRACT_TIMEOUT)
                raise ExtractionTimeoutError(
                    f"{EXTRACT_TIMEOUT}: document {document_id} from {source.id} timed out; "
                    "the next run tries it twice, then passes it over"
                ) from error
            status(f"Warning: {source.id}: extraction timed out again ({attempt} of {tries})")
    if not ollama.responds():
        raise ExtractionTimeoutError(
            f"{EXTRACT_TIMEOUT}: document {document_id} from {source.id} timed out twice more, but Ollama "
            "answers nothing at all; it is kept for the next run"
        )
    return EXTRACT_TIMEOUT


def _process_source(
    database: Database,
    gmail: GmailClient,
    ollama: OllamaClient,
    settings: Settings,
    source: GmailSource,
    remaining: int,
    status: StatusReporter,
    *,
    force: bool,
    dry_run: bool,
    seen: set[str],
) -> tuple[int, int, int, int, list[int], list[tuple[int, str]]]:
    processed_label = "NewsletterBot/Processed"
    failed_label = "NewsletterBot/Failed"
    query = f"({source.gmail_query}) newer_than:{settings.gmail_lookback_days}d"
    if not force:
        query += f' -label:"{processed_label}" -label:"{failed_label}"'
    discovered = 0
    processed = 0
    failed = 0
    processed_document_ids: list[int] = []
    processed_documents: list[tuple[int, str]] = []
    url_enricher = UrlEnricher()
    url_fetcher = ArticleFetcher()
    status(f"{source.id}: scanning messages")
    for gmail_id in gmail.iter_messages(query):
        if discovered >= remaining:
            break
        # The leftover pass restarts this query, so it meets the messages the first pass already
        # handled. Nothing else stops them: they are left `discovered` by store_items(finalize=
        # False), their Processed label is not synchronised until the digest exists, and
        # discover_document returns the existing id for a `discovered` row rather than None - so
        # they would reach ollama.extract a second time and be counted against the budget again.
        if gmail_id in seen:
            continue
        seen.add(gmail_id)
        existing = database.gmail_document(gmail_id)
        if not force and existing is not None and existing["state"] in ("processed", "failed"):
            if not dry_run and not _sync_processing_label(database, gmail, gmail_id, int(existing["id"]), str(existing["state"])):
                failed += 1
            continue
        message = gmail.get_message(gmail_id)
        payload = message.get("payload")
        headers = message_headers(message)
        received = datetime.fromtimestamp(int(str(message.get("internalDate", "0"))) / 1000, tz=UTC)
        subject = headers.get("subject") or gmail_id
        try:
            if not isinstance(payload, dict):
                # Recorded as a failure rather than skipped. A bare continue stored nothing, applied
                # no failed label, and cost nothing against the run limit, so the query matched the
                # same message on every run: it reappeared daily and paid for a get_message each
                # time. Raising here reuses the failure path the extractor's own errors already take.
                raise EmailExtractionError("message payload is not a MIME structure", "EMAIL_PAYLOAD_INVALID")
            content = _email_content(extract_gmail_payload(payload))
            body = content.analysis_text
        except EmailExtractionError as error:
            error_code = error.code
            document_id = database.discover_gmail_document(
                gmail_id,
                str(message.get("threadId", "")),
                source.id,
                received,
                headers.get("subject", ""),
                headers.get("from", ""),
                "",
                False,
                force=force,
            )
            if document_id is None:
                continue
            discovered += 1
            database.fail_document(document_id, error_code)
            failed += 1
            status(f"{source.id}: failed {subject} ({error_code})")
            if not dry_run:
                _sync_processing_label(database, gmail, gmail_id, document_id, "failed")
            continue
        original_characters = content.original_characters
        if original_characters is None:
            original_characters = len(body)
        truncated = original_characters > MAX_ANALYSIS_CHARS
        body = body[:MAX_ANALYSIS_CHARS] if truncated else body
        document_id = database.discover_gmail_document(
            gmail_id,
            str(message.get("threadId", "")),
            source.id,
            received,
            headers.get("subject", ""),
            headers.get("from", ""),
            body,
            truncated,
            force=force,
        )
        if document_id is None:
            continue
        discovered += 1
        status(f"{source.id}: extracting {subject}")
        extraction = _extraction(
            ollama,
            database,
            source,
            document_id,
            body,
            truncated,
            status,
            timed_out_before=existing is not None and existing["last_error_code"] == EXTRACT_TIMEOUT,
        )
        if isinstance(extraction, str):
            database.fail_document(document_id, extraction)
            failed += 1
            status(f"{source.id}: failed {subject} ({extraction})")
            if not dry_run:
                _sync_processing_label(database, gmail, gmail_id, document_id, "failed")
            continue
        items: list[DigestItem] = []
        for match in url_enricher.match(extraction.items, content.link_candidates, body):
            if match.candidate is None:
                items.append(url_enricher.failed_item(match, "URL_MATCH_UNRESOLVED"))
                continue
            raw_url = str(match.candidate.raw_url)
            cached = database.cached_url_resolution(raw_url)
            if (
                cached is not None
                and cached["status"] == "resolved"
                and cached["resolved_url"]
                and shown_url(
                    ResolvedUrl(
                        raw_url,
                        str(cached["resolved_url"]),
                        str(cached["canonical_url"]) if cached["canonical_url"] else None,
                    )
                )
                is None
            ):
                # Resolved before the resolver could pass a tracker's page-level hop, so it stopped on
                # the click page - which may name itself as its canonical - and there is no link to
                # show; the link deserves another try rather than 30 days of no article.
                cached = None
            if cached is not None:
                if cached["status"] == "resolved" and cached["resolved_url"]:
                    items.append(
                        url_enricher.resolved_item(
                            match,
                            ResolvedUrl(
                                raw_url,
                                str(cached["resolved_url"]),
                                str(cached["canonical_url"]) if cached["canonical_url"] else None,
                            ),
                        )
                    )
                else:
                    items.append(url_enricher.failed_item(match, str(cached["error_code"] or "URL_RESOLUTION_FAILED")))
                continue
            try:
                resolved = resolve_match(match, url_fetcher)
            except UrlResolutionError as error:
                cache_status = "blocked" if error.code in {"URL_POLICY_BLOCKED", "URL_REDIRECT_BLOCKED"} else "failed"
                database.cache_url_resolution(raw_url, cache_status, error_code=error.code)
                items.append(url_enricher.failed_item(match, error.code))
                continue
            database.cache_url_resolution(
                raw_url,
                "resolved",
                resolved_url=resolved.final_url,
                canonical_url=resolved.canonical_url,
            )
            items.append(url_enricher.resolved_item(match, resolved))
        sender = _sender_domain(headers.get("from", ""))
        own_front_page = [item for item in items if _links_front_page_of(item, sender)]
        items = [item for item in items if not _links_front_page_of(item, sender)]
        database.store_items(document_id, items, replace=True, finalize=False)
        processed += 1
        notes = [
            f"{count} item(s) {reason} dropped"
            for count, reason in (
                (extraction.dropped_for_language, "outside the digest language"),
                (len(own_front_page), "linking the newsletter's own front page"),
            )
            if count
        ]
        status(f"{source.id}: processed {subject}" + (f" ({'; '.join(notes)})" if notes else ""))
        processed_document_ids.append(document_id)
        processed_documents.append((document_id, gmail_id))
    return discovered, discovered, processed, failed, processed_document_ids, processed_documents


# Scores titles, named by their source, for how well each fits the digest; RelevanceReranker.score_titles.
TitleScorer = Callable[[list[str], str], list[float]]


def _by_relevance(
    candidates: list[HackerNewsCandidate], source: HackerNewsSource, score_titles: TitleScorer
) -> list[HackerNewsCandidate]:
    """The candidates that fit the digest best first, ties in feed order.

    The feed ranks by votes across everything Hacker News discusses, and the stories are fetched and
    extracted in this order until the source's budget is spent: on 2026-11-01 four of the feed's top
    ten were a farmer's seeds, Norway's roadside restrooms and the like, while stories on malware and
    an airline breach sat below the cut.
    """
    scores = score_titles([candidate.document.title for candidate in candidates], source.name)
    order = sorted(range(len(candidates)), key=lambda index: scores[index], reverse=True)
    return [candidates[index] for index in order]


def _story_text(candidate: HackerNewsCandidate, fetcher: ArticleFetcher, status: StatusReporter) -> ResolvedContent | str:
    """The story's readable text, or the code it could not be read with."""
    try:
        return resolve_hackernews_candidate(candidate, fetcher).content
    except (ArticleFetchError, ArticleExtractionError) as error:
        return error.code
    except Exception as error:
        # Deliberately broad, for the reason headlines._headline_source gives: this parses
        # third-party HTML, and a hidden ancestor with a styled descendant once raised a TypeError
        # out of the parser. No shape of page may end a run that carries the day's newsletters too.
        # Anything reaching here is a defect rather than an unreadable page, so it is named.
        status(f"Warning: could not read {candidate.document.source_url} ({type(error).__name__}); passed over")
        return "ARTICLE_NO_USABLE_TEXT"


def _process_hackernews_source(
    database: Database,
    hackernews: HackerNewsClient,
    ollama: AnalyzesArticles,
    source: HackerNewsSource,
    remaining: int,
    status: StatusReporter,
    *,
    force: bool,
    now: datetime,
    score_titles: TitleScorer,
) -> tuple[int, int, int, int, list[int], int]:
    """Fetch and extract up to the source's budget of stories; the budget counts stories extracted.

    A story whose article cannot be read is passed over for the next one, and spends nothing: one
    unreadable page is a paywall or a robots rule, so it is skipped, not failed. Every page this run
    tried being unreadable is a fetcher that is broken, and those count as failures, so that it cannot
    pass for a quiet news day.
    """
    limit = min(remaining, source.max_articles_per_run)
    discovered = 0
    processed = 0
    failed = 0
    attempted = 0
    skipped = 0
    processed_document_ids: list[int] = []
    unreadable = 0
    fetcher = ArticleFetcher()
    if force:
        status(f"{source.id}: retrying failed stories")
        candidates = []
        for row in database.failed_hackernews_documents(source.id, limit):
            document_id = int(row["id"])
            try:
                candidate = hackernews.retry_candidate(source, int(row["external_id"]), int(row["feed_rank"]), now)
            except HackerNewsError as error:
                database.record_hackernews_fetch_failure(document_id, str(error))
                failed += 1
                attempted += 1
                status(f"{source.id}: failed {row['external_id']} ({error})")
                continue
            if candidate is None:
                database.record_hackernews_fetch_failure(document_id, "HN_ITEM_INVALID")
                failed += 1
                attempted += 1
                status(f"{source.id}: failed {row['external_id']} (HN_ITEM_INVALID)")
                continue
            candidates.append(candidate)
    else:
        status(f"{source.id}: scanning stories")
        # Keep normal filtering in `skipped`, but count unreadable items as failures so a broken
        # item endpoint cannot make a scheduled run look like a quiet news day.
        try:
            discovery = hackernews.discover(source, now, limit=source.max_story_candidates)
        except HackerNewsError as error:
            # The feed itself could not be read, so there is nothing to choose from. The run goes on
            # without the section rather than losing the newsletters' digest with it, and counts
            # the failure, so that it says so.
            status(f"Warning: {source.id}: could not read the feed ({error})")
            return 0, 0, 0, 1, [], 0
        candidates = discovery.candidates
        skipped = discovery.skipped
        failed += discovery.unreadable
        if discovery.unreadable:
            status(f"{source.id}: failed to read {discovery.unreadable} stor{'y' if discovery.unreadable == 1 else 'ies'}")
        if candidates:
            status(f"{source.id}: ranking {len(candidates)} stories")
            candidates = _by_relevance(candidates, source, score_titles)

    for candidate in candidates:
        if attempted >= limit:
            break
        existing = database.hackernews_document(candidate.document.source_id, candidate.document.external_id)
        if existing is not None and str(existing["state"]) == "processed":
            continue
        if existing is not None and str(existing["state"]) == "failed" and not force:
            continue
        document_id, created = database.store_hackernews_metadata(
            candidate.document,
            candidate.feed,
            candidate.feed_rank,
            candidate.score,
            candidate.comments,
            force=force,
        )
        discovered += int(created)
        status(f"{source.id}: resolving {candidate.document.title}")
        resolved = _story_text(candidate, fetcher, status)
        if isinstance(resolved, str):
            database.record_hackernews_fetch_failure(document_id, resolved)
            if not source.allow_metadata_fallback:
                unreadable += 1
                status(f"{source.id}: passed over {candidate.document.title} ({resolved})")
                continue
            resolved = ResolvedContent(document=candidate.document, text="", basis="metadata", truncated=False)
            status(f"{source.id}: analyzing metadata only for {candidate.document.title}")
        else:
            database.store_hackernews_resolution(document_id, resolved)
        attempted += 1

        status(f"{source.id}: analyzing {candidate.document.title}")
        try:
            analysis = ollama.analyze_article(
                source.id,
                int(candidate.document.external_id),
                candidate.document.title,
                candidate.score,
                candidate.comments,
                candidate.document.published_at.isoformat(),
                resolved.basis,
                resolved.text,
                resolved.truncated,
            )
        except OllamaSchemaError:
            database.fail_document(document_id, "OLLAMA_SCHEMA_INVALID")
            failed += 1
            status(f"{source.id}: failed {candidate.document.title} (OLLAMA_SCHEMA_INVALID)")
            continue
        except httpx.HTTPError:
            database.fail_document(document_id, "OLLAMA_REQUEST_FAILED")
            failed += 1
            status(f"{source.id}: failed {candidate.document.title} (OLLAMA_REQUEST_FAILED)")
            continue
        source_url = resolved.final_url or candidate.document.source_url or candidate.document.discussion_url
        database.store_items(
            document_id,
            [
                DigestItem(
                    # The analysis title is in the digest language, translated from the story's own if
                    # the model left that as it was; the story's title stays as what it is checked against.
                    title=analysis.title,
                    source_title=candidate.document.title,
                    category=analysis.category,
                    summary_zh_tw=analysis.summary_zh_tw,
                    why_it_matters_zh_tw=analysis.why_it_matters_zh_tw,
                    source_url=source_url,
                    importance=analysis.importance,
                    confidence=min(analysis.confidence, 0.6) if resolved.basis == "metadata" else analysis.confidence,
                    tags=analysis.tags,
                )
            ],
            replace=True,
            finalize=False,
        )
        processed += 1
        processed_document_ids.append(document_id)
        status(f"{source.id}: processed {candidate.document.title}")
    if processed:
        skipped += unreadable
    else:
        failed += unreadable
    return attempted, discovered, processed, failed, processed_document_ids, skipped
