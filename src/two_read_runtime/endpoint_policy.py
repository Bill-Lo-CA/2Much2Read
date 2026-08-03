from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address
from typing import Literal

import httpx

OLLAMA_REMOTE_NOT_ALLOWED = "OLLAMA_REMOTE_NOT_ALLOWED"
OLLAMA_INSECURE_REMOTE_NOT_ALLOWED = "OLLAMA_INSECURE_REMOTE_NOT_ALLOWED"
OLLAMA_ENDPOINT_INVALID = "OLLAMA_ENDPOINT_INVALID"
DISCORD_WEBHOOK_INVALID = "DISCORD_WEBHOOK_INVALID"

OllamaEndpointClassification = Literal["local", "remote_https", "remote_insecure", "invalid"]

_DISCORD_WEBHOOK_HOSTS = frozenset({"canary.discord.com", "discord.com", "discordapp.com", "ptb.discord.com"})


class EndpointPolicyError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class OllamaEndpoint:
    url: str
    classification: OllamaEndpointClassification


def _parse_ollama_url(value: str) -> httpx.URL | None:
    if not isinstance(value, str) or not value or value != value.strip() or "#" in value:
        return None
    try:
        url = httpx.URL(value)
        port = url.port
    except (TypeError, ValueError, httpx.InvalidURL):
        return None
    if (
        url.scheme not in {"http", "https"}
        or not url.host
        or (port is not None and not 1 <= port <= 65535)
        or bool(url.username)
        or bool(url.password)
    ):
        return None
    return url


def classify_ollama_endpoint(value: str) -> OllamaEndpointClassification:
    url = _parse_ollama_url(value)
    if url is None:
        return "invalid"
    try:
        local = url.host == "localhost" or ip_address(url.host).is_loopback
    except ValueError:
        local = False
    if local:
        return "local"
    return "remote_https" if url.scheme == "https" else "remote_insecure"


def validate_ollama_endpoint(value: str, *, allow_remote: bool = False) -> OllamaEndpoint:
    classification = classify_ollama_endpoint(value)
    if classification == "invalid":
        raise EndpointPolicyError(OLLAMA_ENDPOINT_INVALID)
    if classification == "remote_insecure":
        raise EndpointPolicyError(OLLAMA_INSECURE_REMOTE_NOT_ALLOWED)
    if classification == "remote_https" and not allow_remote:
        raise EndpointPolicyError(OLLAMA_REMOTE_NOT_ALLOWED)
    url = _parse_ollama_url(value)
    assert url is not None
    return OllamaEndpoint(str(url).rstrip("/"), classification)


def validate_discord_webhook(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "#" in value:
        raise EndpointPolicyError(DISCORD_WEBHOOK_INVALID)
    try:
        url = httpx.URL(value)
        port = url.port
    except (TypeError, ValueError, httpx.InvalidURL):
        raise EndpointPolicyError(DISCORD_WEBHOOK_INVALID) from None
    if (
        url.scheme != "https"
        or url.host not in _DISCORD_WEBHOOK_HOSTS
        or port not in {None, 443}
        or bool(url.username)
        or bool(url.password)
    ):
        raise EndpointPolicyError(DISCORD_WEBHOOK_INVALID)
    parts = url.path.split("/")
    if len(parts) != 5 or parts[1:3] != ["api", "webhooks"]:
        raise EndpointPolicyError(DISCORD_WEBHOOK_INVALID)
    webhook_id, token = parts[3:]
    if not webhook_id.isascii() or not webhook_id.isdecimal() or not token.isascii() or not token:
        raise EndpointPolicyError(DISCORD_WEBHOOK_INVALID)
    if any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._~-" for character in token):
        raise EndpointPolicyError(DISCORD_WEBHOOK_INVALID)
    return str(url)
