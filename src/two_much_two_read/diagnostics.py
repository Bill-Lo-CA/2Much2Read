from __future__ import annotations

import httpx
import yaml

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
    checks["discord"] = "configured" if settings.discord_webhook_url else "missing"
    if send_test:
        if not settings.discord_webhook_url:
            checks["discord_test"] = "missing"
        else:
            try:
                response = httpx.post(
                    settings.discord_webhook_url,
                    params={"wait": "true"},
                    json={"content": "2much2read connectivity test", "allowed_mentions": {"parse": []}},
                    timeout=30,
                )
                checks["discord_test"] = "ok" if response.is_success else "failed"
            except httpx.HTTPError:
                checks["discord_test"] = "unreachable"
    status = "ok" if all(value in {"ok", "configured"} for value in checks.values()) else "warning"
    return DoctorResult(status=status, checks=checks)
