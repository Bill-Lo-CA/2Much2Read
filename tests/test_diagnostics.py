from pathlib import Path

import pytest

from two_much_two_read import operations
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
    assert operations._model_name(value) == expected


def test_doctor_accepts_default_model_tag_and_creatable_database_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text("sources: []\n", encoding="utf-8")

    class Response:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, object]:
            return {"models": [{"name": "mistral:latest"}]}

    monkeypatch.setattr(operations.httpx, "get", lambda *args, **kwargs: Response())
    settings = Settings(
        sources_config_path=sources_path,
        database_path=tmp_path / "new-directory" / "digest.sqlite3",
        ollama_model="mistral",
    )

    assert operations.doctor(settings, send_test=False).checks["ollama"] == "ok"
    assert operations.doctor(settings, send_test=False).checks["database_directory"] == "ok"
    tagged_settings = settings.model_copy(update={"ollama_model": "mistral:7b"})
    assert operations.doctor(tagged_settings, send_test=False).checks["ollama"] == "model_missing"


def test_missing_database_directory_is_creatable_under_writable_parent(tmp_path: Path) -> None:
    assert directory_is_creatable(tmp_path / "missing" / "nested")
