from pathlib import Path

import pytest

from two_much_two_read import diagnostics
from two_much_two_read.config import Settings
from two_read_runtime import permissions
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


def private_runtime_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path, Path, Path, Path]:
    config_root = tmp_path / "config"
    token_root = config_root / "2much2read"
    data_root = tmp_path / "data" / "2much2read"
    config_root.mkdir()
    token_root.mkdir()
    data_root.mkdir(parents=True)
    for path in (config_root, token_root, data_root):
        path.chmod(0o700)
    env_path = config_root / ".2much2read.env"
    config_path = config_root / "sources.yaml"
    credentials_path = config_root / "gmail-client-secret.json"
    token_path = token_root / "gmail-token.json"
    for path, content in (
        (env_path, "DISCORD_WEBHOOK_URL=ignored\n"),
        (config_path, "sources: []\n"),
        (credentials_path, "{}"),
        (token_path, "{}"),
    ):
        path.write_text(content, encoding="utf-8")
        path.chmod(0o600)
    monkeypatch.setattr(permissions, "config_dir", lambda: config_root)
    monkeypatch.setattr(permissions, "app_config_dir", lambda _: token_root)
    monkeypatch.setattr(permissions, "app_data_dir", lambda _: data_root)
    monkeypatch.setattr(permissions, "env_file", lambda _: env_path)
    return config_root, token_root, data_root, config_path, credentials_path, token_path


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


def test_doctor_reports_safe_runtime_permissions(
    tmp_path: Path, newsletter_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, token_root, data_root, config_path, credentials_path, token_path = private_runtime_paths(tmp_path, monkeypatch)
    mock_ollama(monkeypatch, ["llama3.2:3b", "qwen3:8b"])
    monkeypatch.setattr(diagnostics, "token_status", lambda *args: "ok")
    settings = newsletter_settings.model_copy(
        update={
            "sources_config_path": config_path,
            "gmail_credentials_path": credentials_path,
            "gmail_token_path": token_path,
            "database_path": data_root / "2much2read.sqlite3",
            "lock_path": data_root / "2much2read.lock",
            "ollama_model": "llama3.2:3b",
            "ollama_review_model": "qwen3:8b",
        }
    )

    result = diagnostics.doctor(settings, send_test=False)

    assert result.status == "ok"
    assert result.checks["config_dir"] == "ok"
    assert result.checks["token_dir"] == "ok"
    assert result.checks["data_dir"] == "ok"
    assert result.checks["database"] == "not_created"
    assert result.checks["lock_file"] == "not_created"
    assert result.checks["runtime_paths"] == "ok"


def test_doctor_reports_unsafe_runtime_permissions(
    tmp_path: Path, newsletter_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_root, token_root, data_root, config_path, credentials_path, token_path = private_runtime_paths(tmp_path, monkeypatch)
    config_root.chmod(0o755)
    mock_ollama(monkeypatch, ["llama3.2:3b", "qwen3:8b"])
    monkeypatch.setattr(diagnostics, "token_status", lambda *args: "ok")
    settings = newsletter_settings.model_copy(
        update={
            "sources_config_path": config_path,
            "gmail_credentials_path": credentials_path,
            "gmail_token_path": token_path,
            "database_path": data_root / "2much2read.sqlite3",
            "lock_path": data_root / "2much2read.lock",
            "ollama_model": "llama3.2:3b",
            "ollama_review_model": "qwen3:8b",
        }
    )

    result = diagnostics.doctor(settings, send_test=False)

    assert result.status == "warning"
    assert result.checks["config_dir"] == "unsafe"


def test_doctor_warns_for_custom_runtime_paths(
    tmp_path: Path, newsletter_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, _, config_path, credentials_path, _ = private_runtime_paths(tmp_path, monkeypatch)
    custom_root = tmp_path / "custom"
    custom_root.mkdir()
    mock_ollama(monkeypatch, ["llama3.2:3b", "qwen3:8b"])
    monkeypatch.setattr(diagnostics, "token_status", lambda *args: "ok")
    settings = newsletter_settings.model_copy(
        update={
            "sources_config_path": config_path,
            "gmail_credentials_path": credentials_path,
            "gmail_token_path": custom_root / "gmail-token.json",
            "database_path": custom_root / "digest.sqlite3",
            "lock_path": custom_root / "digest.lock",
        }
    )

    result = diagnostics.doctor(settings, send_test=False)

    assert result.status == "warning"
    assert result.checks["runtime_paths"] == "custom"


def test_doctor_does_not_leak_sensitive_paths(
    tmp_path: Path, newsletter_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, data_root, _, credentials_path, token_path = private_runtime_paths(tmp_path, monkeypatch)
    secret_path = tmp_path / "secret-client-token-must-not-appear.json"
    mock_ollama(monkeypatch, ["llama3.2:3b", "qwen3:8b"])
    monkeypatch.setattr(diagnostics, "token_status", lambda *args: "ok")
    result = diagnostics.doctor(
        newsletter_settings.model_copy(
            update={
                "sources_config_path": secret_path,
                "gmail_credentials_path": credentials_path,
                "gmail_token_path": token_path,
                "database_path": data_root / "2much2read.sqlite3",
                "lock_path": data_root / "2much2read.lock",
                "ollama_model": "llama3.2:3b",
                "ollama_review_model": "qwen3:8b",
            }
        ),
        send_test=False,
    )

    output = result.model_dump_json()
    assert str(secret_path) not in output
