from __future__ import annotations

import time

import httpx


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


def _fenced_block(text: str) -> tuple[str, str] | None:
    opener, separator, body = text.partition("\n")
    if not separator or not opener.startswith("```") or not body.endswith("\n```"):
        return None
    return opener, body.removesuffix("\n```")


def chunk_text(text: str, limit: int = 2000) -> list[str]:
    if len(text) <= limit:
        return [text]
    fenced = _fenced_block(text)
    if fenced is None:
        chunks = _split_text(text, limit - 12)
        total = len(chunks)
        return [f"({index}/{total}) {chunk}" for index, chunk in enumerate(chunks, 1)]
    opener, body = fenced
    chunks = _split_text(body, limit - len(opener) - len("\n```") - 12)
    total = len(chunks)
    return [f"({index}/{total}) {opener}\n{chunk}\n```" for index, chunk in enumerate(chunks, 1)]


def deliver(webhook_url: str, content: str, username: str) -> list[str]:
    if not webhook_url:
        raise ValueError("DISCORD_WEBHOOK_URL is required")
    message_ids: list[str] = []
    for chunk in chunk_text(content):
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
            break
        else:
            raise RuntimeError("DISCORD_DELIVERY_FAILED")
    return message_ids
