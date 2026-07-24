import importlib.util
import sqlite3
import sys
from contextlib import nullcontext
from pathlib import Path

import pytest

from two_much_two_read.config import Settings
from two_much_two_read.storage import SCHEMA_VERSION, Database

script_path = Path(__file__).parents[1] / "scripts" / "cleanup_test_environment.py"
spec = importlib.util.spec_from_file_location("cleanup_test_environment", script_path)
assert spec is not None and spec.loader is not None
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


def test_reset_database_replaces_a_legacy_database_with_v2(tmp_path: Path) -> None:
    path = tmp_path / "digest.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE schema_version(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);"
        "INSERT INTO schema_version VALUES(1, 'now');"
        "CREATE TABLE messages(id INTEGER PRIMARY KEY);"
    )
    connection.close()
    Path(f"{path}-wal").write_text("stale", encoding="utf-8")
    Path(f"{path}-journal").write_text("stale", encoding="utf-8")

    assert cleanup.database_counts(path) == {"documents": 0, "items": 0, "digests": 0, "runs": 0}
    cleanup.reset_database(path)

    database = Database(path)
    assert database.connection.execute("SELECT version FROM schema_version").fetchone()[0] == SCHEMA_VERSION
    assert database.connection.execute("SELECT 1 FROM sqlite_master WHERE name='messages'").fetchone() is None
    assert database.counts() == {
        "documents": 0,
        "gmail_document_state": 0,
        "hackernews_document_state": 0,
        "items": 0,
        "digests": 0,
        "runs": 0,
    }
    database.close()


def test_cleanup_keeps_database_when_label_removal_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text(
        "sources:\n  - id: source\n    name: Source\n    gmail_query: from:source@example.com\n",
        encoding="utf-8",
    )
    settings = Settings(
        sources_config_path=sources_path,
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
    )
    removals: list[tuple[str, list[str]]] = []
    resets: list[Path] = []

    class FakeGmailClient:
        labels = {"NewsletterBot/Processed": "processed"}

        def list_messages(self, query: str) -> list[str]:
            return ["gmail-1"]

        def remove_labels(self, message_id: str, labels: list[str]) -> None:
            removals.append((message_id, labels))
            raise RuntimeError("Gmail unavailable")

    monkeypatch.setattr(cleanup, "Settings", lambda: settings)
    monkeypatch.setattr(cleanup, "credentials", lambda *args: object())
    monkeypatch.setattr(cleanup, "GmailClient", lambda _: FakeGmailClient())
    monkeypatch.setattr(cleanup, "ProcessLock", lambda _: nullcontext())
    monkeypatch.setattr(cleanup, "reset_database", lambda path: resets.append(path))
    monkeypatch.setattr(sys, "argv", ["cleanup_test_environment.py", "--apply"])

    with pytest.raises(RuntimeError, match="Gmail unavailable"):
        cleanup.main()

    assert removals == [("gmail-1", cleanup.PROCESSING_LABELS)]
    assert resets == []
