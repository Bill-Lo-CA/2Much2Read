from pathlib import Path

import pytest

from two_much_two_read import diagnostics
from two_much_two_read.config import Settings
from two_read_runtime.discord import DiscordDeliveryError
from two_read_runtime.paths import directory_is_creatable


def mock_ollama(monkeypatch: pytest.MonkeyPatch, models: list[str]) -> list[dict[str, object]]:
    options: list[dict[str, object]] = []

    class Response:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, object]:
            return {"models": [{"name": model} for model in models]}

    class Client:
        def __init__(self, **kwargs: object) -> None:
            options.append(kwargs)

        def __enter__(self) -> "Client":
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def get(self, *args: object, **kwargs: object) -> Response:
            return Response()

    monkeypatch.setattr(diagnostics.httpx, "Client", Client)
    return options


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("mistral", "mistral:latest"),
        ("mistral:7b", "mistral:7b"),
        ("registry.example/mistral", "registry.example/mistral:latest"),
    ],
)
def test_normalizes_ollama_default_tags(value: str, expected: str) -> None:
    assert diagnostics.model_name(value) == expected


def test_doctor_accepts_default_model_tag_and_creatable_database_directory(
    tmp_path: Path, newsletter_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    client_options = mock_ollama(monkeypatch, ["mistral:latest"])
    settings = newsletter_settings.model_copy(
        update={
            "database_path": tmp_path / "new-directory" / "digest.sqlite3",
            "ollama_model": "mistral",
            "ollama_review_model": "mistral",
        }
    )

    assert diagnostics.doctor(settings, send_test=False).checks["ollama"] == "ok"
    assert diagnostics.doctor(settings, send_test=False).checks["ollama_endpoint"] == "local"
    assert diagnostics.doctor(settings, send_test=False).checks["database_directory"] == "ok"
    tagged_settings = settings.model_copy(update={"ollama_model": "mistral:7b"})
    assert diagnostics.doctor(tagged_settings, send_test=False).checks["ollama"] == "model_missing"
    missing_reviewer = settings.model_copy(update={"ollama_review_model": "qwen3:8b"})
    assert diagnostics.doctor(missing_reviewer, send_test=False).checks["ollama"] == "model_missing"
    assert all(options == {"timeout": 5, "trust_env": False} for options in client_options)


def test_missing_database_directory_is_creatable_under_writable_parent(tmp_path: Path) -> None:
    assert directory_is_creatable(tmp_path / "missing" / "nested")


def test_doctor_reports_an_unreachable_discord_test(newsletter_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    def offline(*args: object, **kwargs: object) -> None:
        raise DiscordDeliveryError()

    mock_ollama(monkeypatch, [])
    monkeypatch.setattr(diagnostics, "deliver", offline)

    result = diagnostics.doctor(
        newsletter_settings.model_copy(
            update={"discord_webhook_url": "https://discord.com/api/webhooks/123456789012345678/test-webhook-token"}
        ),
        True,
    )

    assert result.status == "warning"
    assert result.checks["discord_test_webhook"] == "failed"


def test_doctor_tests_each_both_destination(newsletter_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []
    mock_ollama(monkeypatch, [])

    def send(destination: object, *args: object) -> list[str]:
        transport = destination.transport  # type: ignore[attr-defined]
        sent.append(transport)
        if transport == "bot":
            raise DiscordDeliveryError("bot unavailable")
        return [transport]

    monkeypatch.setattr(diagnostics, "deliver", send)

    result = diagnostics.doctor(
        newsletter_settings.model_copy(
            update={"discord_delivery_mode": "both", "discord_bot_token": "token", "discord_bot_channel_id": "123"}
        ),
        True,
    )

    assert result.checks["discord"] == "both"
    assert result.checks["discord_test_webhook"] == "ok"
    assert result.checks["discord_test_bot"] == "failed"
    assert sent == ["webhook", "bot"]


def test_doctor_reports_remote_endpoint_policy_without_connecting(newsletter_settings: Settings) -> None:
    result = diagnostics.doctor(
        newsletter_settings.model_copy(update={"ollama_base_url": "https://ollama.example"}), send_test=False
    )

    assert result.checks["ollama_endpoint"] == "remote_https"
    assert result.checks["ollama"] == "remote_not_allowed"
    assert result.status == "warning"
