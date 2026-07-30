from __future__ import annotations

import httpx
import yaml

from two_read_runtime.discord import DiscordDeliveryError, deliver
from two_read_runtime.oauth import token_status
from two_read_runtime.paths import directory_is_creatable

from .command_models import DoctorResult
from .config import Settings, load_sources


def model_name(value: str) -> str:
    value = value.strip()
    return value if ":" in value.rsplit("/", 1)[-1] else f"{value}:latest"


def doctor(settings: Settings, send_test: bool) -> DoctorResult:
    checks: dict[str, str] = {}
    try:
        load_sources(settings.sources_config_path)
        checks["sources"] = "ok"
    except (OSError, ValueError, yaml.YAMLError) as error:
        checks["sources"] = str(error)
    checks["gmail_token"] = token_status(
        settings.gmail_token_path,
        ("https://www.googleapis.com/auth/gmail.modify", "https://www.googleapis.com/auth/gmail.settings.basic"),
    )
    checks["database_directory"] = "ok" if directory_is_creatable(settings.database_path.parent) else "not_writable"
    try:
        response = httpx.get(f"{settings.ollama_base_url.rstrip('/')}/api/tags", timeout=5)
        response.raise_for_status()
        payload = response.json()
        models = (
            [model.get("name") for model in payload.get("models", []) if isinstance(model, dict)]
            if isinstance(payload, dict)
            else []
        )
        checks["ollama"] = (
            "ok" if model_name(settings.ollama_model) in {model_name(str(model)) for model in models} else "model_missing"
        )
    except (httpx.HTTPError, ValueError):
        checks["ollama"] = "unreachable"
    try:
        destinations = settings.discord_destinations()
        checks["discord"] = settings.discord_delivery_mode
    except DiscordDeliveryError:
        destinations = []
        checks["discord"] = (
            "missing" if settings.discord_delivery_mode == "webhook" and not settings.discord_webhook_url else "invalid"
        )
    if send_test:
        if not destinations:
            checks["discord_test"] = "missing"
        else:
            try:
                for destination in destinations:
                    deliver(destination, "2much2read connectivity test", settings.discord_username)
                checks["discord_test"] = "ok"
            except DiscordDeliveryError:
                checks["discord_test"] = "failed"
    status = "ok" if all(value in {"ok", "webhook", "bot", "both"} for value in checks.values()) else "warning"
    return DoctorResult(status=status, checks=checks)
