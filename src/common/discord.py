from __future__ import annotations

import time

import httpx


def chunk_text(text: str, limit: int = 2000) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        cut = min(limit - 12, len(remaining))
        if cut < len(remaining):
            boundary = max(remaining.rfind("\n\n", 0, cut), remaining.rfind("\n", 0, cut))
            cut = boundary if boundary > limit // 2 else cut
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    total = len(chunks)
    return [f"({index}/{total}) {chunk}" for index, chunk in enumerate(chunks, 1)]


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
