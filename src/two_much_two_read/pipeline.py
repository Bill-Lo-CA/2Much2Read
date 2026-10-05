"""A digest run from start to delivery, and the commands that retry, reset, or prune what runs leave behind."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from two_read_runtime.discord import DiscordDeliveryError, deliver, deliver_resumable, delivery_error_code
from two_read_runtime.locking import ProcessLock
from two_read_runtime.text import is_inert

from .command_models import (
    DeliveryCheckpointResetResult,
    MaintenancePruneResult,
    NewsletterRetryResult,
    NewsletterRunResult,
    SourceItemCounts,
)
from .config import GmailSource, HackerNewsSource, Settings, SourceConfig, load_sources
from .digest import (
    render_digest,
)
from .gmail import GmailClient, credentials
from .hackernews import HackerNewsClient
from .headlines import (
    _checked_titles,
    _deepened_entries,
)
from .ingestion import (
    _process_hackernews_source,
    _process_source,
    _sync_processing_label,
)
from .ollama import (
    OllamaClient,
    close_ollama_client,
    create_ollama_client,
)
from .reranker import RelevanceReranker
from .selection import (
    _items,
    _ranked_entries,
    _repeat_marker,
    _save_reranker_scores,
    _selected_entries,
)
from .stages import StatusReporter, _ignore_status, _unload_model
from .storage import Database


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


MAX_ERROR_SUMMARY = 500


def _error_summary(error: BaseException) -> str:
    """The run row's only record of a failure, so it has to carry the message, not just the type.

    Storing `type(error).__name__` alone cost six days of diagnosis: every scheduled run from
    2026-09-13 to 2026-09-18 failed in under a second and left the single word "ValueError",
    while the exception it came from already read `AUTH_REAUTH_REQUIRED: run '...'` - oauth.py
    had identified an expired refresh token and named the remedy, and the run log threw it away.

    The cause chain is followed because that code is raised `from` the transport or refresh error
    that explains it, and control characters are flattened because the text comes from libraries
    and, further down a chain, can quote message content.
    """
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        message = str(current).strip()
        parts.append(f"{type(current).__name__}: {message}" if message else type(current).__name__)
        current = current.__cause__
    joined = " <- ".join(parts)
    flattened = " ".join("".join(character if is_inert(character, keep="") else " " for character in joined).split())
    if len(flattened) > MAX_ERROR_SUMMARY:
        return flattened[: MAX_ERROR_SUMMARY - 1] + "…"
    return flattened


def _fair_share(budget: int, sources: int) -> int:
    """The slice of a run's Gmail budget each source is guaranteed before any source takes more.

    `budget` is whichever cap binds - the configured GMAIL_MAX_MESSAGES_PER_RUN, or a smaller
    --max-messages when one is given.

    The budget used to be spent in file order: `gmail_remaining` was decremented as the loop went,
    so a source with a backlog - or simply several editions a day - could exhaust it before the
    loop reached the rest, and every source after it was skipped by a bare `continue` that left no
    record anywhere. The starvation was silent and always hit the same tail of the file.

    Never less than one, so that a run with more sources than budget still reaches a source per
    slot rather than spending everything on the first one.
    """
    if sources <= 0:
        return 0
    return max(1, budget // sources)


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
    skipped = 0
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
            gmail_sources = [source for source in sources if isinstance(source, GmailSource)]
            # From whichever budget actually binds. Sharing out the configured one while
            # --max-messages holds a smaller cap hands the whole command budget to the sources at
            # the front of the file and exits before reaching the rest, which is the starvation
            # this is here to remove.
            effective_budget = gmail_remaining if command_remaining is None else min(gmail_remaining, command_remaining)
            allowance = _fair_share(effective_budget, len(gmail_sources))
            rotate_gmail = effective_budget < len(gmail_sources)
            if rotate_gmail:
                last_source = database.last_gmail_source()
                start = next((index + 1 for index, source in enumerate(gmail_sources) if source.id == last_source), 0)
                gmail_sources = gmail_sources[start:] + gmail_sources[:start]
            # Offer each source its first-pass allowance before revisiting Gmail sources with any
            # budget left. Small budgets resume after the previous run's last attempt. Sources that
            # returned less than their allowance are drained and need no second query.
            gmail_order = iter(gmail_sources)
            schedule: list[tuple[SourceConfig, int | None]] = [
                (next(gmail_order), allowance) if isinstance(source, GmailSource) else (source, None) for source in sources
            ]
            schedule.extend((source, None) for source in gmail_sources)
            drained: set[str] = set()
            seen_by_source: dict[str, set[str]] = {source.id: set() for source in gmail_sources}
            status(f"Starting {len(sources)} source(s)")
            try:
                for source, source_allowance in schedule:
                    if command_remaining is not None and command_remaining <= 0:
                        break
                    if isinstance(source, GmailSource):
                        if source.id in drained:
                            continue
                        source_remaining = gmail_remaining if source_allowance is None else min(source_allowance, gmail_remaining)
                        if command_remaining is not None:
                            source_remaining = min(source_remaining, command_remaining)
                        if source_remaining <= 0:
                            continue
                        assert gmail is not None
                        if rotate_gmail and not dry_run:
                            # Save before the attempt so a failing source cannot monopolize later runs.
                            database.record_gmail_source(source.id)
                        used, source_discovered, source_processed, source_failed, source_ids, source_documents = _process_source(
                            database,
                            gmail,
                            ollama,
                            settings,
                            source,
                            source_remaining,
                            status,
                            force=force,
                            dry_run=dry_run,
                            seen=seen_by_source[source.id],
                        )
                        gmail_remaining -= used
                        if used < source_remaining:
                            drained.add(source.id)
                    else:
                        source_remaining = (
                            min(source.max_articles_per_run, command_remaining)
                            if command_remaining is not None
                            else source.max_articles_per_run
                        )
                        assert hackernews is not None
                        (
                            used,
                            source_discovered,
                            source_processed,
                            source_failed,
                            source_ids,
                            source_skipped,
                        ) = _process_hackernews_source(
                            database, hackernews, ollama, source, source_remaining, status, force=force, now=now
                        )
                        skipped += source_skipped
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
                # Both, since titles are translated between emails and the translator stays loaded too.
                _unload_model(ollama, settings.ollama_model, status)
                _unload_model(ollama, settings.ollama_translate_model, status)

            reranker = RelevanceReranker(settings.reranker_model, settings.reranker_device)
            try:
                ranked_entries = _ranked_entries(reranker, entries)
                _save_reranker_scores(database, ranked_entries, reranker, status)
            finally:
                reranker.close()

            try:
                mark_repeats = _repeat_marker(settings, database, ollama, now, processed_document_ids, source_names_by_id, status)
                reviewed_entries, security_floor = _selected_entries(settings, ollama, ranked_entries, mark_repeats, status)
                if security_floor is not None:
                    status(f"Security floor: promoted {security_floor.promoted}")
                reviewed_entries = _checked_titles(settings, ollama, reviewed_entries, status)
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
                reviewed=True,
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
                skipped=skipped or None,
                delivered=delivered,
                delivery_succeeded=delivery_succeeded if digest_id is not None and not no_deliver else 0,
                delivery_failed=delivery_failed if digest_id is not None and not no_deliver else 0,
                delivery_pending=delivery_pending if digest_id is not None and not no_deliver else len(destinations),
                security_floor=security_floor,
                no_article_by_source={
                    source_id: SourceItemCounts(items=items, no_article=no_article)
                    for source_id, (items, no_article) in database.no_article_counts(processed_document_ids).items()
                }
                or None,
            )
            run_status = result.status
            return result
    except Exception as error:
        error_summary = _error_summary(error)
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
                # Migrating the rows a pre-per-destination digest left behind is still reachable and
                # stays; what was removed here is the branch that asked whether this Database object
                # was old enough to lack the method, which it never is.
                if not active_database.has_digest_deliveries(digest_id) and digest["discord_message_ids_json"] is not None:
                    active_database.migrate_legacy_digest_deliveries(digest_id, destinations)
                active_database.reconcile_digest_deliveries(digest_id, destinations)
            for delivery in active_database.pending_digest_deliveries(destinations):
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


def prune_database(settings: Settings, days: int | None = None, *, dry_run: bool = False) -> MaintenancePruneResult:
    """Drop run history and derived rows past the retention window.

    Deletion uses the pipeline's process lock; previews read a snapshot without touching live files.
    """
    retention = settings.retention_days if days is None else days
    cutoff = datetime.now(UTC) - timedelta(days=retention)
    if dry_run:
        deleted = Database.prunable(settings.database_path, cutoff)
        reclaimed = 0
    else:
        with ProcessLock(settings.lock_path):
            database = Database(settings.database_path)
            try:
                deleted = database.prune(cutoff)
                reclaimed = database.vacuum()
            finally:
                database.close()
    return MaintenancePruneResult(
        retention_days=retention,
        cutoff=cutoff.isoformat(),
        dry_run=dry_run,
        deleted=deleted,
        reclaimed_bytes=reclaimed,
    )


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
