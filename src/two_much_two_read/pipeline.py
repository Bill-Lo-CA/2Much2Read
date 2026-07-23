from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from two_read_runtime.discord import DiscordDeliveryError, deliver, deliver_resumable, delivery_error_code
from two_read_runtime.locking import ProcessLock

from .command_models import DeliveryCheckpointResetResult, NewsletterRetryResult, NewsletterRunResult
from .config import GmailSource, Settings, load_sources
from .digest import render_digest
from .gmail import GmailClient, credentials, message_headers
from .mime import EmptyEmailError, extract_gmail_payload
from .ollama import OllamaClient, OllamaSchemaError, create_ollama_client
from .schemas import DigestItem
from .storage import Database

StatusReporter = Callable[[str], None]


def _ignore_status(_: str) -> None:
    pass


def _items(database: Database, document_ids: list[int], maximum: int) -> list[DigestItem]:
    result: list[DigestItem] = []
    for row in database.items_for_documents(document_ids, maximum * 5):
        result.append(
            DigestItem.model_validate(
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
        )
    return result


def _enabled_sources(settings: Settings, source_id: str | None) -> list[GmailSource]:
    sources = [source for source in load_sources(settings.sources_config_path).sources if source.enabled]
    if source_id:
        matching_sources = [source for source in sources if source.id == source_id]
        if not matching_sources:
            enabled_ids = ", ".join(source.id for source in sources) or "(none)"
            raise ValueError(f"unknown or disabled source_id {source_id!r}; enabled source IDs: {enabled_ids}")
        sources = matching_sources
    if not sources:
        raise ValueError("no enabled sources configured")
    unsupported = [source.id for source in sources if not isinstance(source, GmailSource)]
    if unsupported:
        raise ValueError(f"Hacker News sources are not available yet: {', '.join(unsupported)}")
    return [source for source in sources if isinstance(source, GmailSource)]


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
) -> tuple[int, int, int, int, list[tuple[int, str]]]:
    processed_label = "NewsletterBot/Processed"
    failed_label = "NewsletterBot/Failed"
    query = f"({source.gmail_query}) newer_than:{settings.gmail_lookback_days}d"
    if not force:
        query += f' -label:"{processed_label}" -label:"{failed_label}"'
    discovered = 0
    processed = 0
    failed = 0
    processed_documents: list[tuple[int, str]] = []
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
            body = extract_gmail_payload(payload)
        except EmptyEmailError:
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
            database.fail_document(document_id, "EMAIL_NO_USABLE_TEXT")
            failed += 1
            status(f"{source.id}: failed {subject} (EMAIL_NO_USABLE_TEXT)")
            if not dry_run:
                _sync_processing_label(database, gmail, gmail_id, document_id, "failed")
            continue
        truncated = len(body) > 45_000
        body = body[:45_000] if truncated else body
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
        database.store_extraction(document_id, extraction, replace=True, finalize=False)
        processed += 1
        status(f"{source.id}: processed {subject}")
        processed_documents.append((document_id, gmail_id))
    return discovered, discovered, processed, failed, processed_documents


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
    discovered = 0
    failed = 0
    delivered = 0
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
            creds = credentials(
                settings.gmail_credentials_path,
                settings.gmail_token_path,
                settings.gmail_oauth_callback_port,
            )
            gmail = GmailClient(creds)
            if not dry_run:
                gmail.ensure_labels()
            ollama = create_ollama_client(settings)
            remaining = max_messages or settings.gmail_max_messages_per_run
            status(f"Starting {len(sources)} source(s)")
            for source in sources:
                if remaining <= 0:
                    break
                used, source_discovered, source_processed, source_failed, source_documents = _process_source(
                    database, gmail, ollama, settings, source, remaining, status, force=force, dry_run=dry_run
                )
                remaining -= used
                discovered += source_discovered
                processed += source_processed
                failed += source_failed
                processed_documents.extend(source_documents)

            processed_document_ids = [document_id for document_id, _ in processed_documents]

            content = render_digest(
                _items(database, processed_document_ids, settings.digest_max_items)[: settings.digest_max_items],
                now,
                ", ".join(dict.fromkeys(source.category for source in sources)),
                ", ".join(source.name for source in sources),
                settings.digest_top_items,
            )
            digest_id: int | None = None
            finalized = False
            if not dry_run:
                if content:
                    period_start = now - timedelta(days=1)
                    digest_id = database.save_digest(
                        digest_key,
                        period_start.isoformat(),
                        now.isoformat(),
                        settings.digest_timezone,
                        content,
                        processed_document_ids,
                    )
                    finalized = digest_id is not None
                elif processed_document_ids:
                    database.finalize_documents(processed_document_ids)
                    finalized = True
                if finalized:
                    for document_id, gmail_id in processed_documents:
                        if not _sync_processing_label(database, gmail, gmail_id, document_id, "processed"):
                            failed += 1
                if digest_id is not None and not no_deliver:
                    status("Delivering digest")
                    deliver_digest(settings, database, digest_id)
                    delivered = 1
            result = NewsletterRunResult(
                status="partial" if failed else "ok" if content else "no_content",
                discovered=discovered,
                processed=processed,
                failed=failed,
                delivered=delivered,
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


def retry_delivery(settings: Settings, database: Database | None = None) -> NewsletterRetryResult:
    owned = database is None
    active_database = database
    delivered = 0
    failed = 0
    failed_by_error_code: dict[str, int] = {}
    try:
        with ProcessLock(settings.lock_path):
            active_database = active_database or Database(settings.database_path)
            assert active_database is not None
            for digest in active_database.pending_digests():
                try:
                    digest_id = int(digest["id"])

                    def save_progress(message_ids: list[str], target_id: int = digest_id) -> None:
                        active_database.record_delivery_progress(target_id, message_ids)

                    def finish_delivery(message_ids: list[str], target_id: int = digest_id) -> None:
                        active_database.finish_delivery(target_id, message_ids)

                    deliver_resumable(
                        settings.discord_webhook_url,
                        str(digest["rendered_content"]),
                        settings.discord_username,
                        digest["discord_message_ids_json"],
                        save_progress,
                        finish_delivery,
                        sender=deliver,
                    )
                    delivered += 1
                except DiscordDeliveryError as error:
                    error_code = delivery_error_code(error)
                    active_database.fail_delivery(int(digest["id"]), error_code)
                    failed += 1
                    failed_by_error_code[error_code] = failed_by_error_code.get(error_code, 0) + 1
    finally:
        if owned and active_database is not None:
            active_database.close()
    return NewsletterRetryResult(delivered=delivered, failed=failed, failed_by_error_code=failed_by_error_code)


def reset_corrupt_delivery(settings: Settings, digest_id: int) -> DeliveryCheckpointResetResult:
    with ProcessLock(settings.lock_path):
        database = Database(settings.database_path)
        try:
            if not database.reset_corrupt_delivery(digest_id):
                raise ValueError(f"digest {digest_id} is not a failed corrupt checkpoint")
        finally:
            database.close()
    return DeliveryCheckpointResetResult(digest_id=digest_id)


def deliver_digest(settings: Settings, database: Database, digest_id: int) -> None:
    digest = database.pending_digest(digest_id)
    if digest is None:
        raise ValueError(f"digest {digest_id} is not pending")

    def save_progress(message_ids: list[str]) -> None:
        database.record_delivery_progress(digest_id, message_ids)

    try:
        deliver_resumable(
            settings.discord_webhook_url,
            str(digest["rendered_content"]),
            settings.discord_username,
            digest["discord_message_ids_json"],
            save_progress,
            lambda message_ids: database.finish_delivery(digest_id, message_ids),
            sender=deliver,
        )
    except DiscordDeliveryError as error:
        database.fail_delivery(digest_id, delivery_error_code(error))
        raise
