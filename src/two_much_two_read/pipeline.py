from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from two_read_runtime.discord import deliver
from two_read_runtime.locking import ProcessLock

from .config import Settings, load_sources
from .digest import render_digest
from .gmail import GmailClient, credentials
from .mime import extract_gmail_payload
from .ollama import OllamaClient, OllamaSchemaError
from .schemas import DigestItem
from .storage import Database


def _headers(message: dict[str, object]) -> dict[str, str]:
    payload = message.get("payload", {})
    values = payload.get("headers", []) if isinstance(payload, dict) else []
    return {str(header.get("name", "")).lower(): str(header.get("value", "")) for header in values if isinstance(header, dict)}


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


def _message_ids(row: sqlite3.Row) -> list[str]:
    try:
        value = row["discord_message_ids_json"]
    except KeyError:
        return []
    return [str(item) for item in json.loads(str(value))] if value else []


def run_pipeline(
    settings: Settings,
    source_id: str | None = None,
    max_messages: int | None = None,
    no_deliver: bool = False,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, int | str]:
    sources = [source for source in load_sources(settings.sources_config_path).sources if source.enabled]
    if source_id:
        matching_sources = [source for source in sources if source.id == source_id]
        if not matching_sources:
            enabled_ids = ", ".join(source.id for source in sources) or "(none)"
            raise ValueError(f"unknown or disabled source_id {source_id!r}; enabled source IDs: {enabled_ids}")
        sources = matching_sources
    if not sources:
        raise ValueError("no enabled sources configured")

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
            creds = credentials(
                settings.gmail_credentials_path,
                settings.gmail_token_path,
                settings.gmail_oauth_callback_port,
            )
            gmail = GmailClient(creds)
            gmail.ensure_labels()
            ollama = OllamaClient(
                settings.ollama_base_url,
                settings.ollama_model,
                settings.ollama_timeout_seconds,
                settings.ollama_num_ctx,
                settings.ollama_keep_alive,
            )
            database = Database(Path(":memory:") if dry_run else settings.database_path)
            if not dry_run:
                run_id = database.start_run("newsletter_digest")
            for source in sources:
                processed_label = "NewsletterBot/Processed"
                failed_label = "NewsletterBot/Failed"
                query = f"({source.gmail_query}) newer_than:{settings.gmail_lookback_days}d"
                if not force:
                    query += f' -label:"{processed_label}" -label:"{failed_label}"'
                limit = max_messages or settings.gmail_max_messages_per_run
                for gmail_id in gmail.list_messages(query, limit):
                    message = gmail.get_message(gmail_id)
                    payload = message.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    body = extract_gmail_payload(payload)
                    truncated = len(body) > 45_000
                    body = body[:45_000] if truncated else body
                    headers = _headers(message)
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
                    try:
                        extraction = ollama.extract(source.id, body, truncated, source.max_items_per_email)
                    except OllamaSchemaError:
                        database.fail_message(message_id, "OLLAMA_EXTRACTION_FAILED")
                        failed += 1
                        if not dry_run:
                            gmail.add_labels(gmail_id, [failed_label])
                        continue
                    database.store_extraction(message_id, extraction, replace=force)
                    processed += 1
                    processed_message_ids.append(message_id)
                    if not dry_run:
                        gmail.add_labels(gmail_id, [processed_label])

            now = datetime.now(ZoneInfo(settings.digest_timezone))
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
                    digest_key += f":force:{datetime.now(UTC).isoformat()}"
                digest_id = database.save_digest(
                    digest_key,
                    period_start.isoformat(),
                    now.isoformat(),
                    settings.digest_timezone,
                    content,
                )
                if digest_id is not None and not no_deliver:
                    deliver_digest(settings, database, digest_id)
                    delivered = 1
            result = {
                "status": "partial" if failed else "ok" if content else "no_content",
                "discovered": discovered,
                "processed": processed,
                "failed": failed,
                "delivered": delivered,
            }
            run_status = str(result["status"])
            return result
    except Exception as error:
        error_summary = type(error).__name__
        raise
    finally:
        if database is not None:
            if run_id is not None:
                database.finish_run(run_id, run_status, discovered, processed, failed, delivered, error_summary)
            database.close()


def retry_delivery(settings: Settings, database: Database | None = None) -> int:
    owned = database is None
    active_database = database
    delivered = 0
    try:
        with ProcessLock(settings.lock_path):
            active_database = active_database or Database(settings.database_path)
            assert active_database is not None
            for digest in active_database.pending_digests():
                try:
                    digest_id = int(digest["id"])

                    def save_progress(message_ids: list[str], target_id: int = digest_id) -> None:
                        active_database.record_delivery_progress(target_id, message_ids)

                    ids = deliver(
                        settings.discord_webhook_url,
                        str(digest["rendered_content"]),
                        settings.discord_username,
                        _message_ids(digest),
                        save_progress,
                    )
                    active_database.finish_delivery(digest_id, ids)
                    delivered += 1
                except Exception:
                    active_database.fail_delivery(int(digest["id"]))
                    raise
    finally:
        if owned and active_database is not None:
            active_database.close()
    return delivered


def deliver_digest(settings: Settings, database: Database, digest_id: int) -> None:
    digest = database.pending_digest(digest_id)
    if digest is None:
        raise ValueError(f"digest {digest_id} is not pending")

    def save_progress(message_ids: list[str]) -> None:
        database.record_delivery_progress(digest_id, message_ids)

    try:
        ids = deliver(
            settings.discord_webhook_url,
            str(digest["rendered_content"]),
            settings.discord_username,
            _message_ids(digest),
            save_progress,
        )
        database.finish_delivery(digest_id, ids)
    except Exception:
        database.fail_delivery(digest_id)
        raise
