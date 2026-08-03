from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal

import httpx

from .endpoint_policy import DISCORD_WEBHOOK_INVALID, EndpointPolicyError, validate_discord_webhook

CORRUPT_MESSAGE_IDS = "DISCORD_MESSAGE_IDS_CORRUPT"
DISCORD_CONFIG_INVALID = "DISCORD_CONFIG_INVALID"
DiscordTransport = Literal["webhook", "bot"]


class DiscordDeliveryError(ValueError):
    def __init__(self, code: str = "DISCORD_DELIVERY_FAILED") -> None:
        super().__init__(code)
        self.code = code


class CorruptMessageIdsError(DiscordDeliveryError):
    def __init__(self) -> None:
        super().__init__(CORRUPT_MESSAGE_IDS)


@dataclass(frozen=True)
class DiscordDestination:
    transport: DiscordTransport
    key: str
    webhook_url: str | None = None
    bot_token: str | None = None
    bot_channel_id: str | None = None


DiscordSender = Callable[[DiscordDestination | str, str, str, list[str] | None, Callable[[list[str]], None] | None], list[str]]


def configured_destinations(mode: str, webhook_url: str, bot_token: str, bot_channel_id: str) -> list[DiscordDestination]:
    if mode not in {"webhook", "bot", "both"}:
        raise DiscordDeliveryError(DISCORD_CONFIG_INVALID)
    destinations: list[DiscordDestination] = []
    if mode in {"webhook", "both"}:
        if not webhook_url:
            raise DiscordDeliveryError(DISCORD_CONFIG_INVALID)
        try:
            webhook_url = validate_discord_webhook(webhook_url)
        except EndpointPolicyError as error:
            raise DiscordDeliveryError(error.code) from None
        destinations.append(
            DiscordDestination("webhook", f"webhook:{hashlib.sha256(webhook_url.encode()).hexdigest()}", webhook_url=webhook_url)
        )
    if mode in {"bot", "both"}:
        if not bot_token or not (bot_channel_id.isascii() and bot_channel_id.isdecimal()):
            raise DiscordDeliveryError(DISCORD_CONFIG_INVALID)
        destinations.append(
            DiscordDestination("bot", f"bot:{bot_channel_id}", bot_token=bot_token, bot_channel_id=bot_channel_id)
        )
    return destinations


def configured_destination(mode: str, webhook_url: str, bot_token: str, bot_channel_id: str) -> DiscordDestination:
    destinations = configured_destinations(mode, webhook_url, bot_token, bot_channel_id)
    if len(destinations) != 1:
        raise DiscordDeliveryError(DISCORD_CONFIG_INVALID)
    return destinations[0]


def legacy_destination(destinations: list[DiscordDestination], destination_key: str | None = None) -> DiscordDestination:
    if destination_key:
        destination = next((item for item in destinations if item.key == destination_key), None)
        if destination is not None:
            return destination
        if destination_key != "webhook":
            raise DiscordDeliveryError(DISCORD_CONFIG_INVALID)
    return next((destination for destination in destinations if destination.transport == "webhook"), destinations[0])


def parse_message_ids(value: object) -> list[str]:
    if value is None:
        return []
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError as error:
        raise CorruptMessageIdsError() from error
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise CorruptMessageIdsError()
    return parsed


def delivery_error_code(error: Exception) -> str:
    known_codes = {
        CORRUPT_MESSAGE_IDS,
        DISCORD_CONFIG_INVALID,
        DISCORD_WEBHOOK_INVALID,
        "DISCORD_BOT_UNAUTHORIZED",
        "DISCORD_BOT_FORBIDDEN",
        "DISCORD_BOT_CHANNEL_NOT_FOUND",
        "DISCORD_RATE_LIMITED",
        "DISCORD_DELIVERY_FAILED",
    }
    return error.code if isinstance(error, DiscordDeliveryError) and error.code in known_codes else "DISCORD_DELIVERY_FAILED"


def deliver_resumable(
    destination: DiscordDestination | str,
    content: str,
    username: str,
    stored_message_ids: object,
    on_progress: Callable[[list[str]], None],
    on_success: Callable[[list[str]], None],
    *,
    sender: DiscordSender,
) -> list[str]:
    message_ids = sender(destination, content, username, parse_message_ids(stored_message_ids), on_progress)
    on_success(message_ids)
    return message_ids


def _split_text(text: str, limit: int) -> list[str]:
    if limit <= 0:
        raise ValueError("chunk limit must be positive")
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


def _numbered_chunks(text: str, limit: int) -> list[str]:
    chunks = _split_text(text, limit - 12)
    total = len(chunks)
    return [f"({index}/{total}) {chunk}" for index, chunk in enumerate(chunks, 1)]


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
        return _numbered_chunks(text, limit)
    opener, body, footer = fenced
    body_limit = limit - len(opener) - len("\n```") - 12
    if body_limit <= 0:
        return _numbered_chunks(text.replace("```", "``\u200b`"), limit)
    chunks = _split_text(body, body_limit)
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


def _destination(value: DiscordDestination | str) -> DiscordDestination:
    if isinstance(value, DiscordDestination):
        if value.transport == "webhook":
            if not value.webhook_url:
                raise DiscordDeliveryError(DISCORD_CONFIG_INVALID)
            try:
                webhook_url = validate_discord_webhook(value.webhook_url)
            except EndpointPolicyError as error:
                raise DiscordDeliveryError(error.code) from None
            return replace(value, webhook_url=webhook_url)
        return value
    if not value:
        raise DiscordDeliveryError("DISCORD_WEBHOOK_URL is required")
    try:
        webhook_url = validate_discord_webhook(value)
    except EndpointPolicyError as error:
        raise DiscordDeliveryError(error.code) from None
    return DiscordDestination("webhook", f"webhook:{hashlib.sha256(webhook_url.encode()).hexdigest()}", webhook_url=webhook_url)


def _request(
    destination: DiscordDestination,
    content: str,
    username: str,
    allowed_mentions: dict[str, list[str]],
    *,
    client: httpx.Client | None = None,
) -> httpx.Response:
    if destination.transport == "webhook":
        assert destination.webhook_url is not None
        url, params, headers = destination.webhook_url, {"wait": "true"}, {}
        payload: dict[str, object] = {"content": content, "username": username, "allowed_mentions": allowed_mentions}
    else:
        assert destination.bot_token is not None and destination.bot_channel_id is not None
        url = f"https://discord.com/api/v10/channels/{destination.bot_channel_id}/messages"
        params = {}
        headers = {"Authorization": f"Bot {destination.bot_token}"}
        payload = {"content": content, "allowed_mentions": allowed_mentions}
    if client is not None:
        return client.post(url, params=params, headers=headers, json=payload)
    with httpx.Client(timeout=30, trust_env=False) as owned_client:
        return owned_client.post(url, params=params, headers=headers, json=payload)


def _error_for_response(destination: DiscordDestination, response: httpx.Response) -> DiscordDeliveryError:
    if destination.transport == "bot":
        codes = {401: "DISCORD_BOT_UNAUTHORIZED", 403: "DISCORD_BOT_FORBIDDEN", 404: "DISCORD_BOT_CHANNEL_NOT_FOUND"}
        if response.status_code in codes:
            return DiscordDeliveryError(codes[response.status_code])
    return DiscordDeliveryError()


def _retry_after(response: httpx.Response, default: float) -> float:
    try:
        return max(0, min(float(response.headers.get("Retry-After", default)), 30))
    except ValueError:
        return default


def deliver(
    destination: DiscordDestination | str,
    content: str,
    username: str,
    message_ids: list[str] | None = None,
    on_progress: Callable[[list[str]], None] | None = None,
    allowed_user_ids: list[str] | None = None,
    mention_user_ids: list[str] | None = None,
) -> list[str]:
    resolved_destination = _destination(destination)
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
    allowed_mentions: dict[str, list[str]] = {"parse": []}
    if allowed_user_ids:
        allowed_mentions["users"] = allowed_user_ids
    with httpx.Client(timeout=30, trust_env=False) as client:
        for chunk in chunks[len(message_ids) :]:
            for attempt in range(4):
                try:
                    response = _request(resolved_destination, chunk, username, allowed_mentions, client=client)
                except httpx.HTTPError as error:
                    raise DiscordDeliveryError() from error
                if response.status_code == 429:
                    if attempt < 3:
                        time.sleep(_retry_after(response, 1))
                        continue
                    raise DiscordDeliveryError("DISCORD_RATE_LIMITED")
                if response.status_code >= 500:
                    if attempt < 3:
                        time.sleep(2**attempt)
                        continue
                    raise DiscordDeliveryError()
                if response.is_error:
                    raise _error_for_response(resolved_destination, response)
                try:
                    message_id = str(response.json()["id"])
                except (KeyError, TypeError, ValueError) as error:
                    raise DiscordDeliveryError() from error
                message_ids.append(message_id)
                if on_progress is not None:
                    on_progress(message_ids)
                break
    return message_ids
