from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from two_read_runtime.discord import DiscordDeliveryError, deliver, deliver_resumable, delivery_error_code, legacy_destination
from two_read_runtime.locking import ProcessLock

from .article_extractor import ArticleExtractionError, extract_article
from .article_fetcher import ArticleFetcher, ArticleFetchError, ResolvedUrl, UrlResolutionError
from .command_models import DeliveryCheckpointResetResult, NewsletterRetryResult, NewsletterRunResult
from .config import GmailSource, HackerNewsSource, Settings, load_sources
from .digest import (
    DigestEntry,
    canonical_url,
    dedupe_entries,
    merge_related_entries,
    render_digest,
)
from .gmail import GmailClient, credentials, message_headers
from .hackernews import HackerNewsClient, HackerNewsError, resolve_hackernews_candidate
from .mime import MAX_ANALYSIS_CHARS, EmailExtractionError, extract_gmail_payload
from .ollama import OllamaClient, OllamaContextError, OllamaSchemaError, close_ollama_client, create_ollama_client
from .reranker import RelevanceReranker
from .schemas import DigestItem, ExtractedEmailContent, ResolvedContent
from .storage import Database
from .url_enrichment import UrlEnricher, resolve_match

StatusReporter = Callable[[str], None]
# The category whose reviewer slots are reserved by DIGEST_SECURITY_CANDIDATE_SLOTS.
RESERVED_CATEGORY = "SECURITY"


def _ignore_status(_: str) -> None:
    pass


def _email_content(value: ExtractedEmailContent | str) -> ExtractedEmailContent:
    return value if isinstance(value, ExtractedEmailContent) else ExtractedEmailContent(analysis_text=value)


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
            }
        )
        result.append(
            DigestEntry(
                item=item,
                candidate_id=int(str(row["id"])),
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


def _ranked_entries(reranker: RelevanceReranker, entries: list[DigestEntry]) -> list[DigestEntry]:
    if not entries:
        return []
    return reranker.rank(dedupe_entries(entries))


def _save_reranker_scores(
    database: Database, ranked: list[DigestEntry], reranker: RelevanceReranker, status: StatusReporter
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


def _reviewed_entries(settings: Settings, ollama: OllamaClient, ranked: list[DigestEntry]) -> list[DigestEntry]:
    if not ranked:
        return []
    ranked = _review_candidates(ranked, settings.digest_review_candidate_limit, settings.digest_security_candidate_slots)
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
                "source": entry.source_name,
            }
        )
    # The context fitter trims from the tail, where the reserved candidates sit by design.
    # The reviewer is released by the caller, once merging and the headline rewrite have also
    # finished with it: both run on this model, and nothing else loads in between.
    review = ollama.review_digest(
        candidates, settings.digest_max_items, RESERVED_CATEGORY, settings.digest_security_candidate_slots
    )
    scores = {selection.candidate_id: selection.score for selection in review.selected}
    selected = [replace(entry, review_score=scores[entry.candidate_id]) for entry in ranked if entry.candidate_id in scores]
    # The reviewer only picks the headline items, but the candidates behind them were already
    # extracted, ranked, and paid for. They carry no review score, so they sort below every
    # selected item and render as the digest's secondary mentions. The secondary limit is applied
    # after merging, so a mention absorbed into a headline frees its slot for the next candidate.
    unselected = [entry for entry in ranked if entry.candidate_id not in scores]
    return selected + unselected


def _story_judge(ollama: OllamaClient, budget: int, status: StatusReporter) -> Callable[[DigestEntry, DigestEntry], bool]:
    """Ask the review model whether two shortlisted entries are the same story.

    Bounded and memoised, because the shortlist is loose by design. The budget caps how many
    generations one digest may spend on merging; past it nothing merges, which loses an attribution
    rather than producing a wrong one. A model or transport failure is answered no for the same
    reason: not merging is the safe direction.
    """
    answers: dict[tuple[int | None, int | None], bool] = {}
    spent = 0

    def judge(left: DigestEntry, right: DigestEntry) -> bool:
        nonlocal spent
        key = (left.candidate_id, right.candidate_id)
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
    entries: list[DigestEntry], secondary_items: int, same_story: Callable[[DigestEntry, DigestEntry], bool]
) -> list[DigestEntry]:
    headlines = [entry for entry in entries if entry.review_score is not None]
    mentions = [entry for entry in entries if entry.review_score is None]
    headlines, mentions = merge_related_entries(headlines, mentions, same_story)
    return headlines + mentions[:secondary_items]


def _article_to_deepen_from(entry: DigestEntry) -> str | None:
    """The story's own article, or nothing - never the page the discussion lives on.

    A Hacker News self-post has no article, so its stored source_url falls back to the discussion
    URL. Fetching that returns the whole thread, and extract_article cannot tell the author's post
    from the replies to it: the rewrite would restate a commenter's claim as the headline's own
    finding. The extractor already read the self-post text when the item was analysed, so the
    newsletter fallback below is that text's summary rather than a loss.
    """
    url = entry.article_url or (str(entry.item.source_url) if entry.item.source_url else None)
    if url and canonical_url(url) == canonical_url(entry.discussion_url):
        return None
    return url


def _headline_source(entry: DigestEntry, fetcher: ArticleFetcher, status: StatusReporter) -> tuple[str, str]:
    """The fullest text available for a headline: the article itself, or the newsletters on it.

    The extractor's own summary is the floor, not the goal - it was written from a few lines of one
    email. Email bodies are never persisted, so the article is the only route to more text, and the
    merged newsletter coverage is the fallback when there is no link or the fetch fails.
    """
    url = _article_to_deepen_from(entry)
    if url:
        try:
            fetched = fetcher.fetch(url)
            return extract_article(fetched.content_type, fetched.body).text, "article"
        except (ArticleFetchError, ArticleExtractionError, UrlResolutionError):
            pass
        except Exception as error:
            # Deliberately broad, and reported rather than swallowed. This parses third-party HTML
            # that nothing in this project controls, and a longer summary is an enhancement: no
            # shape of page should be able to end a run that has already extracted, ranked, and
            # reviewed a full digest. One did - a hidden ancestor with a styled descendant raised a
            # TypeError out of the parser and took the whole digest with it. Anything reaching here
            # is a defect rather than an unreachable page, so it is named: a silent fallback would
            # let the rewrite quietly stop working for a whole class of pages.
            status(f"Warning: could not read {url} ({type(error).__name__}); using the newsletter summaries")
    summaries = entry.merged_summaries or (entry.item.summary_zh_tw,)
    return "\n\n".join((*summaries, entry.item.why_it_matters_zh_tw)), "newsletters"


def _deepened_entries(
    settings: Settings, ollama: OllamaClient, entries: list[DigestEntry], status: StatusReporter
) -> list[DigestEntry]:
    if not settings.digest_deepen_headlines:
        return entries
    fetcher = ArticleFetcher()
    deepened: list[DigestEntry] = []
    for entry in entries:
        if entry.review_score is None:
            deepened.append(entry)
            continue
        content, basis = _headline_source(entry, fetcher, status)
        if basis == "newsletters" and not entry.merged_summaries:
            # The fallback is this item's own summary, so there is nothing fuller to rewrite from.
            # The prompt asks for four to six sentences naming versions and numbers, and a single
            # 60-character summary supports none of that: the model could only pad or invent, and
            # the reader would have no way to tell an expanded headline from an inflated one. A
            # generation is skipped rather than spent turning one sentence into six.
            deepened.append(entry)
            continue
        status(f"Expanding {entry.item.title}")
        sources = ", ".join((entry.source_name or entry.source_id or "Unknown", *entry.also_from))
        try:
            rewrite = ollama.deepen_item(entry.item.title, entry.item.category, sources, basis, content)
        except (OllamaContextError, OllamaSchemaError, httpx.HTTPError) as error:
            # A headline with its original short summary still beats losing the digest.
            status(f"Warning: kept the original summary for {entry.item.title} ({type(error).__name__})")
            deepened.append(entry)
            continue
        if not rewrite.covers_the_item:
            status(f"Warning: {basis} source did not cover {entry.item.title}; kept the original summary")
            deepened.append(entry)
            continue
        item = entry.item.model_copy(
            update={"summary_zh_tw": rewrite.summary_zh_tw, "why_it_matters_zh_tw": rewrite.why_it_matters_zh_tw}
        )
        deepened.append(replace(entry, item=item))
    return deepened


def _unload_model(ollama: OllamaClient, model: str, status: StatusReporter = _ignore_status) -> None:
    if not ollama.unload(model):
        status(f"Warning: {model} did not unload and may still hold memory")


def _enabled_sources(settings: Settings, source_id: str | None) -> list[GmailSource | HackerNewsSource]:
    sources = [source for source in load_sources(settings.sources_config_path).sources if source.enabled]
    if source_id:
        matching_sources = [source for source in sources if source.id == source_id]
        if not matching_sources:
            enabled_ids = ", ".join(source.id for source in sources) or "(none)"
            raise ValueError(f"unknown or disabled source_id {source_id!r}; enabled source IDs: {enabled_ids}")
        sources = matching_sources
    if not sources:
        raise ValueError("no enabled sources configured")
    return sources


def _digest_key(settings: Settings, source_id: str | None, now: datetime, force: bool) -> str:
    key = f"daily:{now.date()}:{settings.digest_timezone}:{source_id or 'all'}"
    return f"{key}:force:{now.astimezone(UTC).isoformat()}" if force else key


def _sync_processing_label(database: Database, gmail: GmailClient, gmail_id: str, document_id: int, state: str) -> bool:
    try:
        gmail.sync_processing_label(gmail_id, state)
    except Exception:
        database.fail_label_sync(document_id)
        return False
    database.mark_label_synced(document_id)
    return True


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
        existing = database.gmail_document(gmail_id)
        if not force and existing is not None and existing["state"] in ("processed", "failed"):
            if not dry_run and not _sync_processing_label(database, gmail, gmail_id, int(existing["id"]), str(existing["state"])):
                failed += 1
            continue
        message = gmail.get_message(gmail_id)
        payload = message.get("payload")
        if not isinstance(payload, dict):
            continue
        headers = message_headers(message)
        received = datetime.fromtimestamp(int(str(message.get("internalDate", "0"))) / 1000, tz=UTC)
        subject = headers.get("subject") or gmail_id
        try:
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
        except OllamaSchemaError as error:
            reason = str(error).split(" response_preview=", 1)[0]
            database.fail_document(document_id, reason)
            failed += 1
            status(f"{source.id}: failed {subject} ({reason})")
            if not dry_run:
                _sync_processing_label(database, gmail, gmail_id, document_id, "failed")
            continue
        items: list[DigestItem] = []
        for match in url_enricher.match(extraction.items, content.link_candidates):
            if match.candidate is None:
                items.append(url_enricher.failed_item(match, "URL_MATCH_UNRESOLVED"))
                continue
            raw_url = str(match.candidate.raw_url)
            cached = database.cached_url_resolution(raw_url)
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
        database.store_items(document_id, items, replace=True, finalize=False)
        processed += 1
        status(f"{source.id}: processed {subject}")
        processed_document_ids.append(document_id)
        processed_documents.append((document_id, gmail_id))
    return discovered, discovered, processed, failed, processed_document_ids, processed_documents


def _process_hackernews_source(
    database: Database,
    hackernews: HackerNewsClient,
    ollama: OllamaClient,
    source: HackerNewsSource,
    remaining: int,
    status: StatusReporter,
    *,
    force: bool,
    now: datetime,
) -> tuple[int, int, int, int, list[int]]:
    limit = min(remaining, source.max_articles_per_run)
    discovered = 0
    processed = 0
    failed = 0
    attempted = 0
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
        candidates = hackernews.discover(source, now, limit=source.max_story_candidates).candidates

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
                    title=candidate.document.title,
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
    return attempted, discovered, processed, failed, processed_document_ids


def run_pipeline(
    settings: Settings,
    source_id: str | None = None,
    max_messages: int | None = None,
    no_deliver: bool = False,
    dry_run: bool = False,
    force: bool = False,
    *,
    now: datetime | None = None,
    status: StatusReporter | None = None,
) -> NewsletterRunResult:
    sources = _enabled_sources(settings, source_id)
    status = status or _ignore_status

    database: Database | None = None
    run_id: int | None = None
    run_status = "failed"
    error_summary: str | None = None
    processed = 0
    processed_documents: list[tuple[int, str]] = []
    processed_document_ids: list[int] = []
    discovered = 0
    failed = 0
    delivered = 0
    delivery_succeeded = 0
    delivery_failed = 0
    delivery_pending = 0
    hackernews: HackerNewsClient | None = None
    ollama: OllamaClient | None = None
    try:
        with ProcessLock(settings.lock_path):
            database = Database(Path(":memory:") if dry_run else settings.database_path)
            timezone = ZoneInfo(settings.digest_timezone)
            now = (now or datetime.now(timezone)).astimezone(timezone)
            digest_key = _digest_key(settings, source_id, now, force)
            if not dry_run:
                run_id = database.start_run("newsletter_digest")
                if not force and database.digest_exists(digest_key):
                    result = NewsletterRunResult(
                        status="skipped", reason="daily_digest_exists", discovered=0, processed=0, failed=0, delivered=0
                    )
                    run_status = result.status
                    return result
            gmail: GmailClient | None = None
            if any(isinstance(source, GmailSource) for source in sources):
                creds = credentials(
                    settings.gmail_credentials_path,
                    settings.gmail_token_path,
                    settings.gmail_oauth_callback_port,
                )
                gmail = GmailClient(creds)
                if not dry_run:
                    gmail.ensure_labels()
            if any(isinstance(source, HackerNewsSource) for source in sources):
                hackernews = HackerNewsClient()
            ollama = create_ollama_client(settings)
            gmail_remaining = settings.gmail_max_messages_per_run
            command_remaining = max_messages
            status(f"Starting {len(sources)} source(s)")
            try:
                for source in sources:
                    if command_remaining is not None and command_remaining <= 0:
                        break
                    if isinstance(source, GmailSource):
                        source_remaining = (
                            min(gmail_remaining, command_remaining) if command_remaining is not None else gmail_remaining
                        )
                        if source_remaining <= 0:
                            continue
                        assert gmail is not None
                        used, source_discovered, source_processed, source_failed, source_ids, source_documents = _process_source(
                            database, gmail, ollama, settings, source, source_remaining, status, force=force, dry_run=dry_run
                        )
                        gmail_remaining -= used
                    else:
                        source_remaining = (
                            min(source.max_articles_per_run, command_remaining)
                            if command_remaining is not None
                            else source.max_articles_per_run
                        )
                        assert hackernews is not None
                        used, source_discovered, source_processed, source_failed, source_ids = _process_hackernews_source(
                            database, hackernews, ollama, source, source_remaining, status, force=force, now=now
                        )
                        source_documents = []
                    if command_remaining is not None:
                        command_remaining -= used
                    discovered += source_discovered
                    processed += source_processed
                    failed += source_failed
                    processed_document_ids.extend(source_ids)
                    processed_documents.extend(source_documents)
                source_names_by_id = {source.id: source.name for source in sources}
                entries = _items(database, processed_document_ids, settings.digest_rerank_candidate_limit, source_names_by_id)
            finally:
                _unload_model(ollama, settings.ollama_model, status)

            reranker = RelevanceReranker(settings.reranker_model, settings.reranker_device)
            try:
                ranked_entries = _ranked_entries(reranker, entries)
                _save_reranker_scores(database, ranked_entries, reranker, status)
            finally:
                reranker.close()

            try:
                reviewed_entries = _reviewed_entries(settings, ollama, ranked_entries)
                reviewed_entries = _merged_entries(
                    reviewed_entries,
                    settings.digest_secondary_items,
                    _story_judge(ollama, settings.digest_merge_judgements, status),
                )
                reviewed_entries = _deepened_entries(settings, ollama, reviewed_entries, status)
            finally:
                _unload_model(ollama, settings.ollama_review_model, status)
            content = render_digest(
                reviewed_entries,
                now,
                ", ".join(dict.fromkeys(source.category for source in sources)),
                ", ".join(dict.fromkeys(entry.source_name or entry.source_id or "Unknown" for entry in reviewed_entries)),
                settings.digest_top_items,
                settings.digest_language,
            )
            digest_id: int | None = None
            destinations = []
            delivery_error: DiscordDeliveryError | None = None
            finalized = False
            if not dry_run:
                if content:
                    period_start = now - timedelta(days=1)
                    if no_deliver:
                        try:
                            destinations = settings.discord_destinations()
                        except DiscordDeliveryError:
                            destinations = []
                    else:
                        try:
                            destinations = settings.discord_destinations()
                        except DiscordDeliveryError as error:
                            delivery_error = error
                    digest_id = database.save_digest(
                        digest_key,
                        period_start.isoformat(),
                        now.isoformat(),
                        settings.digest_timezone,
                        content,
                        processed_document_ids,
                        destinations,
                    )
                    finalized = digest_id is not None
                elif processed_document_ids:
                    database.finalize_documents(processed_document_ids)
                    finalized = True
                if finalized:
                    for document_id, gmail_id in processed_documents:
                        assert gmail is not None
                        if not _sync_processing_label(database, gmail, gmail_id, document_id, "processed"):
                            failed += 1
                if digest_id is not None and not no_deliver:
                    status("Delivering digest")
                    if delivery_error is not None:
                        delivery_failed = 1
                        database.fail_delivery(digest_id, delivery_error_code(delivery_error))
                    else:
                        delivery_succeeded, delivery_failed, delivery_pending = deliver_digest(settings, database, digest_id)
                    delivered = int(delivery_succeeded and not delivery_failed and not delivery_pending)
            result = NewsletterRunResult(
                status="partial" if failed or delivery_failed else "ok" if content else "no_content",
                discovered=discovered,
                processed=processed,
                failed=failed,
                delivered=delivered,
                delivery_succeeded=delivery_succeeded if digest_id is not None and not no_deliver else 0,
                delivery_failed=delivery_failed if digest_id is not None and not no_deliver else 0,
                delivery_pending=delivery_pending if digest_id is not None and not no_deliver else len(destinations),
            )
            run_status = result.status
            return result
    except Exception as error:
        error_summary = type(error).__name__
        raise
    finally:
        if database is not None:
            if run_id is not None:
                database.finish_run(run_id, run_status, discovered, processed, failed, delivered, error_summary)
            database.close()
        if hackernews is not None:
            hackernews.close()
        if ollama is not None:
            close_ollama_client(ollama)


def retry_delivery(settings: Settings, database: Database | None = None) -> NewsletterRetryResult:
    owned = database is None
    active_database = database
    delivered = 0
    failed = 0
    failed_by_error_code: dict[str, int] = {}
    destinations = settings.discord_destinations()
    try:
        with ProcessLock(settings.lock_path):
            active_database = active_database or Database(settings.database_path)
            assert active_database is not None
            for digest in active_database.pending_digests():
                digest_id = int(digest["id"])
                if not hasattr(active_database, "has_digest_deliveries"):
                    try:
                        try:
                            destination_key = digest["discord_destination_key"]
                        except (IndexError, KeyError):
                            destination_key = None

                        def save_legacy_progress(message_ids: list[str], target_id: int = digest_id) -> None:
                            active_database.record_delivery_progress(target_id, message_ids)

                        def finish_legacy_delivery(message_ids: list[str], target_id: int = digest_id) -> None:
                            active_database.finish_delivery(target_id, message_ids)

                        deliver_resumable(
                            legacy_destination(destinations, str(destination_key) if destination_key is not None else None),
                            str(digest["rendered_content"]),
                            settings.discord_username,
                            digest["discord_message_ids_json"],
                            save_legacy_progress,
                            finish_legacy_delivery,
                            sender=deliver,
                        )
                        delivered += 1
                    except DiscordDeliveryError as error:
                        error_code = delivery_error_code(error)
                        active_database.fail_delivery(digest_id, error_code)
                        failed += 1
                        failed_by_error_code[error_code] = failed_by_error_code.get(error_code, 0) + 1
                    continue
                if not active_database.has_digest_deliveries(digest_id) and digest["discord_message_ids_json"] is not None:
                    active_database.migrate_legacy_digest_deliveries(digest_id, destinations)
                active_database.reconcile_digest_deliveries(digest_id, destinations)
            deliveries = (
                active_database.pending_digest_deliveries(destinations)
                if hasattr(active_database, "pending_digest_deliveries")
                else []
            )
            for delivery in deliveries:
                try:
                    delivery_id = int(delivery["id"])
                    destination = next(
                        destination for destination in destinations if destination.key == delivery["destination_key"]
                    )

                    def save_progress(message_ids: list[str], target_id: int = delivery_id) -> None:
                        active_database.record_digest_delivery_progress(target_id, message_ids)

                    def finish_delivery(message_ids: list[str], target_id: int = delivery_id) -> None:
                        active_database.finish_digest_delivery(target_id, message_ids, destinations)

                    deliver_resumable(
                        destination,
                        str(delivery["rendered_content"]),
                        settings.discord_username,
                        delivery["discord_message_ids_json"],
                        save_progress,
                        finish_delivery,
                        sender=deliver,
                    )
                    delivered += 1
                except DiscordDeliveryError as error:
                    error_code = delivery_error_code(error)
                    active_database.fail_digest_delivery(int(delivery["id"]), error_code, destinations)
                    failed += 1
                    failed_by_error_code[error_code] = failed_by_error_code.get(error_code, 0) + 1
    finally:
        if owned and active_database is not None:
            active_database.close()
    return NewsletterRetryResult(
        status="failed" if failed and not delivered else "partial" if failed else "ok",
        delivered=delivered,
        failed=failed,
        failed_by_error_code=failed_by_error_code,
    )


def reset_corrupt_delivery(settings: Settings, delivery_id: int) -> DeliveryCheckpointResetResult:
    with ProcessLock(settings.lock_path):
        database = Database(settings.database_path)
        try:
            if database.has_digest_delivery(delivery_id):
                if not database.reset_corrupt_digest_delivery(delivery_id, settings.discord_destinations()):
                    raise ValueError(f"digest delivery {delivery_id} is not a failed corrupt checkpoint")
            elif not database.reset_corrupt_delivery(delivery_id):
                raise ValueError(f"digest delivery {delivery_id} is not a failed corrupt checkpoint")
        finally:
            database.close()
    return DeliveryCheckpointResetResult(delivery_id=delivery_id)


def deliver_digest(settings: Settings, database: Database, digest_id: int) -> tuple[int, int, int]:
    digest = database.pending_digest(digest_id)
    if digest is None:
        raise ValueError(f"digest {digest_id} is not pending")
    destinations = settings.discord_destinations()
    if not database.has_digest_deliveries(digest_id) and digest["discord_message_ids_json"] is not None:
        database.migrate_legacy_digest_deliveries(digest_id, destinations)
    database.reconcile_digest_deliveries(digest_id, destinations)
    delivered = failed = 0
    for delivery in database.digest_deliveries(digest_id, destinations):
        delivery_id = int(delivery["id"])
        destination = next(destination for destination in destinations if destination.key == delivery["destination_key"])
        try:

            def save_progress(message_ids: list[str], target_id: int = delivery_id) -> None:
                database.record_digest_delivery_progress(target_id, message_ids)

            def finish_delivery(message_ids: list[str], target_id: int = delivery_id) -> None:
                database.finish_digest_delivery(target_id, message_ids, destinations)

            deliver_resumable(
                destination,
                str(delivery["rendered_content"]),
                settings.discord_username,
                delivery["discord_message_ids_json"],
                save_progress,
                finish_delivery,
                sender=deliver,
            )
            delivered += 1
        except DiscordDeliveryError as error:
            failed += 1
            error_code = delivery_error_code(error)
            database.fail_digest_delivery(delivery_id, error_code, destinations)
    return delivered, failed, 0
