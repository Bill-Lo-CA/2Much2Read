from pathlib import Path

import httpx
import pytest

from two_much_two_read import diagnostics
from two_much_two_read.config import Settings
from two_read_runtime.paths import directory_is_creatable


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
    class Response:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, object]:
            return {"models": [{"name": "mistral:latest"}]}

    monkeypatch.setattr(diagnostics.httpx, "get", lambda *args, **kwargs: Response())
    settings = newsletter_settings.model_copy(
        update={"database_path": tmp_path / "new-directory" / "digest.sqlite3", "ollama_model": "mistral"}
    )

    assert diagnostics.doctor(settings, send_test=False).checks["ollama"] == "ok"
    assert diagnostics.doctor(settings, send_test=False).checks["database_directory"] == "ok"
    tagged_settings = settings.model_copy(update={"ollama_model": "mistral:7b"})
    assert diagnostics.doctor(tagged_settings, send_test=False).checks["ollama"] == "model_missing"


def test_missing_database_directory_is_creatable_under_writable_parent(tmp_path: Path) -> None:
    assert directory_is_creatable(tmp_path / "missing" / "nested")


def test_doctor_reports_an_unreachable_discord_test(newsletter_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, object]:
            return {"models": []}

    def offline(*args: object, **kwargs: object) -> None:
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(diagnostics.httpx, "get", lambda *args, **kwargs: Response())
    monkeypatch.setattr(diagnostics.httpx, "post", offline)

    result = diagnostics.doctor(newsletter_settings.model_copy(update={"discord_webhook_url": "https://discord.example"}), True)

    assert result.status == "warning"
    assert result.checks["discord_test"] == "unreachable"
