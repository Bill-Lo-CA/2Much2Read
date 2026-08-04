from __future__ import annotations

import httpx
import yaml

from two_read_runtime.discord import DiscordDeliveryError, deliver
from two_read_runtime.endpoint_policy import (
    OLLAMA_INSECURE_REMOTE_NOT_ALLOWED,
    OLLAMA_REMOTE_NOT_ALLOWED,
    EndpointPolicyError,
    classify_ollama_endpoint,
    validate_ollama_endpoint,
)
from two_read_runtime.oauth import token_status
from two_read_runtime.paths import directory_is_creatable
from two_read_runtime.permissions import runtime_permission_checks

from .command_models import DoctorResult
from .config import Settings, load_sources

_HEALTHY_CHECKS = {"ok", "not_created", "webhook", "bot", "both", "local", "remote_https"}


def _config_error_status(error: Exception) -> str:
    return "missing" if "not found" in str(error).lower() else "invalid"


def model_name(value: str) -> str:
    value = value.strip()
    return value if ":" in value.rsplit("/", 1)[-1] else f"{value}:latest"


def doctor(settings: Settings, send_test: bool) -> DoctorResult:
    checks: dict[str, str] = {}
    try:
        load_sources(settings.sources_config_path)
        checks["sources"] = "ok"
    except (OSError, ValueError, yaml.YAMLError) as error:
        checks["sources"] = _config_error_status(error)
    checks["gmail_token"] = token_status(
        settings.gmail_token_path,
        ("https://www.googleapis.com/auth/gmail.modify", "https://www.googleapis.com/auth/gmail.settings.basic"),
    )
    checks.update(
        runtime_permission_checks(
            "2much2read",
            config_path=settings.sources_config_path,
            credentials_path=settings.gmail_credentials_path,
            token_path=settings.gmail_token_path,
            database_path=settings.database_path,
            lock_path=settings.lock_path,
        )
    )
    checks["database_directory"] = "ok" if directory_is_creatable(settings.database_path.parent) else "not_writable"
    endpoint_classification = classify_ollama_endpoint(settings.ollama_base_url)
    checks["ollama_endpoint"] = endpoint_classification
    try:
        endpoint = validate_ollama_endpoint(settings.ollama_base_url, allow_remote=settings.ollama_allow_remote)
    except EndpointPolicyError as error:
        checks["ollama"] = (
            "remote_insecure"
            if error.code == OLLAMA_INSECURE_REMOTE_NOT_ALLOWED
            else "remote_not_allowed"
            if error.code == OLLAMA_REMOTE_NOT_ALLOWED
            else "invalid"
        )
    else:
        try:
            with httpx.Client(timeout=5, trust_env=settings.ollama_trust_env) as client:
                response = client.get(f"{endpoint.url}/api/tags")
                response.raise_for_status()
                payload = response.json()
            models = (
                [model.get("name") for model in payload.get("models", []) if isinstance(model, dict)]
                if isinstance(payload, dict)
                else []
            )
            available_models = {model_name(str(model)) for model in models}
            checks["ollama"] = (
                "ok"
                if all(model_name(model) in available_models for model in (settings.ollama_model, settings.ollama_review_model))
                else "model_missing"
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
            for destination in destinations:
                try:
                    deliver(destination, "2much2read connectivity test", settings.discord_username)
                    checks[f"discord_test_{destination.transport}"] = "ok"
                except DiscordDeliveryError:
                    checks[f"discord_test_{destination.transport}"] = "failed"
    status = "ok" if all(value in _HEALTHY_CHECKS for value in checks.values()) else "warning"
    return DoctorResult(status=status, checks=checks)
