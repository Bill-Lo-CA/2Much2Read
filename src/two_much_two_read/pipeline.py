from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from common.discord import deliver
from common.locking import ProcessLock

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


def _items(database: Database, maximum: int) -> list[DigestItem]:
    result: list[DigestItem] = []
    for row in database.recent_items(maximum * 5):
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
    processed = 0
    discovered = 0
    failed = 0
    try:
        with ProcessLock(settings.lock_path):
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
                    if not dry_run:
                        gmail.add_labels(gmail_id, [processed_label])

            now = datetime.now(ZoneInfo(settings.digest_timezone))
            content = render_digest(
                _items(database, settings.digest_max_items)[: settings.digest_max_items],
                now,
                ", ".join(dict.fromkeys(source.category for source in sources)),
                ", ".join(source.name for source in sources),
                settings.digest_top_items,
            )
            delivered = 0
            if content and not dry_run:
                period_start = now - timedelta(days=1)
                digest_key = f"daily:{now.date()}:{settings.digest_timezone}"
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
            return {
                "status": "partial" if failed else "ok" if content else "no_content",
                "discovered": discovered,
                "processed": processed,
                "failed": failed,
                "delivered": delivered,
            }
    finally:
        database.close()


def retry_delivery(settings: Settings, database: Database | None = None) -> int:
    owned = database is None
    database = database or Database(settings.database_path)
    delivered = 0
    try:
        with ProcessLock(settings.lock_path):
            for digest in database.pending_digests():
                try:
                    ids = deliver(
                        settings.discord_webhook_url,
                        str(digest["rendered_content"]),
                        settings.discord_username,
                    )
                    database.finish_delivery(int(digest["id"]), ids)
                    delivered += 1
                except Exception:
                    database.fail_delivery(int(digest["id"]))
                    raise
    finally:
        if owned:
            database.close()
    return delivered


def deliver_digest(settings: Settings, database: Database, digest_id: int) -> None:
    digest = database.pending_digest(digest_id)
    if digest is None:
        raise ValueError(f"digest {digest_id} is not pending")
    try:
        ids = deliver(
            settings.discord_webhook_url,
            str(digest["rendered_content"]),
            settings.discord_username,
        )
        database.finish_delivery(digest_id, ids)
    except Exception:
        database.fail_delivery(digest_id)
        raise
