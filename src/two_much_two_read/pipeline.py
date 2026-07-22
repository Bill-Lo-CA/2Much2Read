from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from two_read_runtime.discord import DiscordDeliveryError, deliver, deliver_resumable, delivery_error_code
from two_read_runtime.locking import ProcessLock

from .command_models import NewsletterRetryResult, NewsletterRunResult
from .config import Settings, Source, load_sources
from .digest import render_digest
from .gmail import GmailClient, credentials, message_headers
from .mime import extract_gmail_payload
from .ollama import OllamaClient, OllamaSchemaError, create_ollama_client
from .schemas import DigestItem
from .storage import Database

StatusReporter = Callable[[str], None]


def _ignore_status(_: str) -> None:
    pass


def _items(database: Database, message_ids: list[int], maximum: int) -> list[DigestItem]:
    result: list[DigestItem] = []
    for row in database.items_for_messages(message_ids, maximum * 5):
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


def _enabled_sources(settings: Settings, source_id: str | None) -> list[Source]:
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


def _process_source(
    database: Database,
    gmail: GmailClient,
    ollama: OllamaClient,
    settings: Settings,
    source: Source,
    remaining: int,
    status: StatusReporter,
    *,
    force: bool,
    dry_run: bool,
) -> tuple[int, int, int, int, list[int]]:
    processed_label = "NewsletterBot/Processed"
    failed_label = "NewsletterBot/Failed"
    query = f"({source.gmail_query}) newer_than:{settings.gmail_lookback_days}d"
    if not force:
        query += f' -label:"{processed_label}" -label:"{failed_label}"'
    discovered = 0
    processed = 0
    failed = 0
    message_ids: list[int] = []
    gmail_ids = gmail.list_messages(query, remaining)
    status(f"{source.id}: {len(gmail_ids)} message(s)")
    for gmail_id in gmail_ids:
        message = gmail.get_message(gmail_id)
        payload = message.get("payload")
        if not isinstance(payload, dict):
            continue
        body = extract_gmail_payload(payload)
        truncated = len(body) > 45_000
        body = body[:45_000] if truncated else body
        headers = message_headers(message)
        received = datetime.fromtimestamp(int(str(message.get("internalDate", "0"))) / 1000, tz=UTC).isoformat()
        message_id = database.discover(
            gmail_id,
            str(message.get("threadId", "")),
            source.id,
            received,
            headers.get("subject", ""),
            headers.get("from", ""),
            body,
            force=force,
        )
        if message_id is None:
            continue
        discovered += 1
        subject = headers.get("subject") or gmail_id
        status(f"{source.id}: extracting {subject}")
        try:
            extraction = ollama.extract(source.id, body, truncated, source.max_items_per_email)
        except OllamaSchemaError as error:
            reason = str(error).split(" response_preview=", 1)[0]
            database.fail_message(message_id, reason)
            failed += 1
            status(f"{source.id}: failed {subject} ({reason})")
            if not dry_run:
                gmail.add_labels(gmail_id, [failed_label])
            continue
        database.store_extraction(message_id, extraction, replace=force)
        processed += 1
        status(f"{source.id}: processed {subject}")
        message_ids.append(message_id)
        if not dry_run:
            gmail.add_labels(gmail_id, [processed_label])
    return len(gmail_ids), discovered, processed, failed, message_ids


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
    processed_message_ids: list[int] = []
    discovered = 0
    failed = 0
    delivered = 0
    try:
        with ProcessLock(settings.lock_path):
            database = Database(Path(":memory:") if dry_run else settings.database_path)
            if not dry_run:
                run_id = database.start_run("newsletter_digest")
            creds = credentials(
                settings.gmail_credentials_path,
                settings.gmail_token_path,
                settings.gmail_oauth_callback_port,
            )
            gmail = GmailClient(creds)
            gmail.ensure_labels()
            ollama = create_ollama_client(settings)
            remaining = max_messages or settings.gmail_max_messages_per_run
            status(f"Starting {len(sources)} source(s)")
            for source in sources:
                if remaining <= 0:
                    break
                used, source_discovered, source_processed, source_failed, source_message_ids = _process_source(
                    database, gmail, ollama, settings, source, remaining, status, force=force, dry_run=dry_run
                )
                remaining -= used
                discovered += source_discovered
                processed += source_processed
                failed += source_failed
                processed_message_ids.extend(source_message_ids)

            timezone = ZoneInfo(settings.digest_timezone)
            now = (now or datetime.now(timezone)).astimezone(timezone)
            content = render_digest(
                _items(database, processed_message_ids, settings.digest_max_items)[: settings.digest_max_items],
                now,
                ", ".join(dict.fromkeys(source.category for source in sources)),
                ", ".join(source.name for source in sources),
                settings.digest_top_items,
            )
            if content and not dry_run:
                period_start = now - timedelta(days=1)
                digest_key = f"daily:{now.date()}:{settings.digest_timezone}:{source_id or 'all'}"
                if force:
                    digest_key += f":force:{now.astimezone(UTC).isoformat()}"
                digest_id = database.save_digest(
                    digest_key,
                    period_start.isoformat(),
                    now.isoformat(),
                    settings.digest_timezone,
                    content,
                )
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
