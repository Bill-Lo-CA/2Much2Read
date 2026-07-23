from __future__ import annotations

import json
import time
from collections.abc import Callable

import httpx

CORRUPT_MESSAGE_IDS = "DISCORD_MESSAGE_IDS_CORRUPT"
DiscordSender = Callable[[str, str, str, list[str] | None, Callable[[list[str]], None] | None], list[str]]


class DiscordDeliveryError(ValueError):
    pass


class CorruptMessageIdsError(DiscordDeliveryError):
    pass


def parse_message_ids(value: object) -> list[str]:
    if value is None:
        return []
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError as error:
        raise CorruptMessageIdsError(f"{CORRUPT_MESSAGE_IDS}: expected a JSON array of strings") from error
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise CorruptMessageIdsError(f"{CORRUPT_MESSAGE_IDS}: expected a JSON array of strings")
    return parsed


def delivery_error_code(error: Exception) -> str:
    if isinstance(error, CorruptMessageIdsError):
        return CORRUPT_MESSAGE_IDS
    return "DISCORD_DELIVERY_FAILED"


def deliver_resumable(
    webhook_url: str,
    content: str,
    username: str,
    stored_message_ids: object,
    on_progress: Callable[[list[str]], None],
    on_success: Callable[[list[str]], None],
    *,
    sender: DiscordSender,
) -> list[str]:
    message_ids = sender(webhook_url, content, username, parse_message_ids(stored_message_ids), on_progress)
    on_success(message_ids)
    return message_ids


def _split_text(text: str, limit: int) -> list[str]:
    chunks: list[str] = []
    remaining = text
    while remaining:
        cut = min(limit, len(remaining))
        if cut < len(remaining):
            boundary = max(remaining.rfind("\n\n", 0, cut), remaining.rfind("\n", 0, cut))
            cut = boundary if boundary > limit // 2 else cut
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    return chunks


def _fenced_block(text: str) -> tuple[str, str, str] | None:
    opener, separator, body = text.partition("\n")
    closing = body.rfind("\n```")
    if not separator or not opener.startswith("```") or closing < 0:
        return None
    return opener, body[:closing], body[closing + len("\n```") :].lstrip()


def chunk_text(text: str, limit: int = 2000) -> list[str]:
    if len(text) <= limit:
        return [text]
    fenced = _fenced_block(text)
    if fenced is None:
        chunks = _split_text(text, limit - 12)
        total = len(chunks)
        return [f"({index}/{total}) {chunk}" for index, chunk in enumerate(chunks, 1)]
    opener, body, footer = fenced
    chunks = _split_text(body, limit - len(opener) - len("\n```") - 12)
    footer_chunks = _split_text(footer, limit - 12) if footer else []
    total = len(chunks) + len(footer_chunks)
    return [
        *(f"({index}/{total}) {opener}\n{chunk}\n```" for index, chunk in enumerate(chunks, 1)),
        *(f"({index}/{total}) {chunk}" for index, chunk in enumerate(footer_chunks, len(chunks) + 1)),
    ]


def _chunk_with_mentions(text: str, user_ids: list[str], limit: int = 2000) -> list[str]:
    prefix_chunks: list[str] = []
    prefix = ""
    for user_id in user_ids:
        candidate = f"{prefix} <@{user_id}>".strip()
        if prefix and len(candidate) > limit:
            prefix_chunks.append(prefix)
            prefix = f"<@{user_id}>"
        else:
            prefix = candidate
    if not text:
        return [*prefix_chunks, prefix]
    body_limit = limit - len(prefix) - 1
    if body_limit < 13:
        return [*prefix_chunks, prefix, *chunk_text(text, limit)]
    body_chunks = chunk_text(text, body_limit)
    return [*prefix_chunks, f"{prefix} {body_chunks[0]}", *body_chunks[1:]]


def deliver(
    webhook_url: str,
    content: str,
    username: str,
    message_ids: list[str] | None = None,
    on_progress: Callable[[list[str]], None] | None = None,
    allowed_user_ids: list[str] | None = None,
    mention_user_ids: list[str] | None = None,
) -> list[str]:
    if not webhook_url:
        raise DiscordDeliveryError("DISCORD_WEBHOOK_URL is required")
    allowed_user_ids = list(dict.fromkeys(allowed_user_ids or []))
    if not all(user_id.isascii() and user_id.isdecimal() for user_id in allowed_user_ids):
        raise DiscordDeliveryError("Discord allowed user IDs must contain digits only")
    mention_user_ids = list(dict.fromkeys(mention_user_ids or []))
    if not set(mention_user_ids).issubset(allowed_user_ids):
        raise DiscordDeliveryError("Discord mention user IDs must be allowed")
    message_ids = list(message_ids or [])
    chunks = _chunk_with_mentions(content, mention_user_ids) if mention_user_ids else chunk_text(content)
    if len(message_ids) > len(chunks):
        raise DiscordDeliveryError("stored Discord delivery progress exceeds message chunks")
    for chunk in chunks[len(message_ids) :]:
        for attempt in range(4):
            try:
                allowed_mentions: dict[str, list[str]] = {"parse": []}
                if allowed_user_ids:
                    allowed_mentions["users"] = allowed_user_ids
                response = httpx.post(
                    webhook_url,
                    params={"wait": "true"},
                    json={"content": chunk, "username": username, "allowed_mentions": allowed_mentions},
                    timeout=30,
                )
                if response.status_code == 429:
                    time.sleep(float(response.headers.get("Retry-After", "1")))
                    continue
                if response.status_code >= 500 and attempt < 3:
                    time.sleep(2**attempt)
                    continue
                response.raise_for_status()
                message_id = str(response.json()["id"])
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
                raise DiscordDeliveryError("DISCORD_DELIVERY_FAILED") from error
            message_ids.append(message_id)
            if on_progress is not None:
                on_progress(message_ids)
            break
        else:
            raise DiscordDeliveryError("DISCORD_DELIVERY_FAILED")
    return message_ids
