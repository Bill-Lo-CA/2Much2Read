from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Settings, load_sources
from .digest import render_digest
from .discord import deliver
from .gmail import GmailClient, credentials
from .locking import ProcessLock
from .mime import extract_gmail_payload
from .ollama import OllamaClient
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
) -> dict[str, int | str]:
    sources = [source for source in load_sources(settings.sources_config_path).sources if source.enabled]
    if source_id:
        sources = [source for source in sources if source.id == source_id]
    if not sources:
        raise ValueError("no enabled matching source")

    creds = credentials(
        settings.gmail_credentials_path,
        settings.gmail_token_path,
        settings.gmail_oauth_callback_port,
    )
    gmail = GmailClient(creds)
    gmail.ensure_labels([source.name for source in sources])
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
    try:
        with ProcessLock(settings.lock_path):
            for source in sources:
                processed_label = "NewsletterBot/Processed"
                failed_label = "NewsletterBot/Failed"
                query = (
                    f"({source.gmail_query}) newer_than:{settings.gmail_lookback_days}d "
                    f'-label:"{processed_label}" -label:"{failed_label}"'
                )
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
                    )
                    if message_id is None:
                        continue
                    discovered += 1
                    extraction = ollama.extract(source.id, body, truncated)
                    if extraction.source_id != source.id or len(extraction.items) > source.max_items_per_email:
                        raise ValueError("OLLAMA_SCHEMA_INVALID")
                    database.store_extraction(message_id, extraction)
                    processed += 1
                    if not dry_run:
                        gmail.add_labels(
                            gmail_id,
                            [
                                processed_label,
                                f"NewsletterBot/Source/{source.name.replace('/', '-')}",
                            ],
                        )

            now = datetime.now(ZoneInfo(settings.digest_timezone))
            content = render_digest(
                _items(database, settings.digest_max_items)[: settings.digest_max_items],
                now,
                ", ".join(source.name for source in sources),
                settings.digest_top_items,
            )
            delivered = 0
            if content and not dry_run:
                period_start = now - timedelta(days=1)
                digest_key = f"daily:{now.date()}:{settings.digest_timezone}"
                created = database.save_digest(
                    digest_key,
                    period_start.isoformat(),
                    now.isoformat(),
                    settings.digest_timezone,
                    content,
                )
                if created and not no_deliver:
                    retry_delivery(settings, database)
                    delivered = 1
            return {
                "status": "ok" if content else "no_content",
                "discovered": discovered,
                "processed": processed,
                "delivered": delivered,
            }
    finally:
        database.close()


def retry_delivery(settings: Settings, database: Database | None = None) -> int:
    owned = database is None
    database = database or Database(settings.database_path)
    delivered = 0
    try:
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
