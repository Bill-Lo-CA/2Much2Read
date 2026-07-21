from __future__ import annotations

import json
import time
from collections.abc import Callable

import httpx

CORRUPT_MESSAGE_IDS = "DISCORD_MESSAGE_IDS_CORRUPT"
DiscordSender = Callable[[str, str, str, list[str] | None, Callable[[list[str]], None] | None], list[str]]


def parse_message_ids(value: object) -> list[str]:
    if value is None:
        return []
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError as error:
        raise ValueError(f"{CORRUPT_MESSAGE_IDS}: expected a JSON array of strings") from error
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ValueError(f"{CORRUPT_MESSAGE_IDS}: expected a JSON array of strings")
    return parsed


def delivery_error_code(error: Exception) -> str:
    if isinstance(error, ValueError) and str(error).startswith(f"{CORRUPT_MESSAGE_IDS}:"):
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


def deliver(
    webhook_url: str,
    content: str,
    username: str,
    message_ids: list[str] | None = None,
    on_progress: Callable[[list[str]], None] | None = None,
) -> list[str]:
    if not webhook_url:
        raise ValueError("DISCORD_WEBHOOK_URL is required")
    message_ids = list(message_ids or [])
    chunks = chunk_text(content)
    if len(message_ids) > len(chunks):
        raise ValueError("stored Discord delivery progress exceeds message chunks")
    for chunk in chunks[len(message_ids) :]:
        for attempt in range(4):
            response = httpx.post(
                webhook_url,
                params={"wait": "true"},
                json={"content": chunk, "username": username, "allowed_mentions": {"parse": []}},
                timeout=30,
            )
            if response.status_code == 429:
                time.sleep(float(response.headers.get("Retry-After", "1")))
                continue
            if response.status_code >= 500 and attempt < 3:
                time.sleep(2**attempt)
                continue
            response.raise_for_status()
            message_ids.append(str(response.json()["id"]))
            if on_progress is not None:
                on_progress(message_ids)
            break
        else:
            raise RuntimeError("DISCORD_DELIVERY_FAILED")
    return message_ids
