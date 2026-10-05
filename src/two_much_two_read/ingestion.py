"""Turning one source's new documents into stored items: Gmail newsletters and Hacker News stories."""

from __future__ import annotations

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
from .gmail import GmailClient, message_headers
from .hackernews import HackerNewsClient, HackerNewsError, resolve_hackernews_candidate
from .mime import MAX_ANALYSIS_CHARS, EmailExtractionError, extract_gmail_payload
from .ollama import (
    OllamaClient,
    OllamaContextError,
    OllamaSchemaError,
)
from .schemas import (
    ArticleAnalysis,
    DigestItem,
    ExtractedEmailContent,
    ResolvedContent,
)
from .stages import StatusReporter
from .storage import Database
from .url_enrichment import UrlEnricher, resolve_match, shown_url


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
    except Exception:
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
        try:
            extraction = ollama.extract(source.id, body, truncated, source.max_items_per_email)
        except (OllamaContextError, OllamaSchemaError) as error:
            reason = str(error).split(" response_preview=", 1)[0]
            database.fail_document(document_id, reason)
            failed += 1
            status(f"{source.id}: failed {subject} ({reason})")
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
) -> tuple[int, int, int, int, list[int], int]:
    limit = min(remaining, source.max_articles_per_run)
    discovered = 0
    processed = 0
    failed = 0
    attempted = 0
    skipped = 0
    processed_document_ids: list[int] = []
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
        discovery = hackernews.discover(source, now, limit=source.max_story_candidates)
        candidates = discovery.candidates
        skipped = discovery.skipped
        failed += discovery.unreadable
        if discovery.unreadable:
            status(f"{source.id}: failed to read {discovery.unreadable} stor{'y' if discovery.unreadable == 1 else 'ies'}")

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
        attempted += 1
        discovered += int(created)
        status(f"{source.id}: resolving {candidate.document.title}")
        try:
            resolved = resolve_hackernews_candidate(candidate, fetcher).content
            database.store_hackernews_resolution(document_id, resolved)
        except (ArticleFetchError, ArticleExtractionError) as error:
            database.record_hackernews_fetch_failure(document_id, error.code)
            if not source.allow_metadata_fallback:
                failed += 1
                status(f"{source.id}: failed {candidate.document.title} ({error.code})")
                continue
            resolved = ResolvedContent(document=candidate.document, text="", basis="metadata", truncated=False)
            status(f"{source.id}: analyzing metadata only for {candidate.document.title}")

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
    return attempted, discovered, processed, failed, processed_document_ids, skipped
