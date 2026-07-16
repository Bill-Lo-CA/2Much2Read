from pathlib import Path

import pytest

from two_much_two_read import pipeline
from two_much_two_read.config import Settings
from two_much_two_read.pipeline import deliver_digest, run_pipeline
from two_much_two_read.schemas import EmailExtraction
from two_much_two_read.storage import Database


def write_sources(path: Path, *, enabled: bool = True) -> None:
    path.write_text(
        f"sources:\n  - id: alphasignal\n    name: AlphaSignal\n    enabled: {str(enabled).lower()}\n"
        "    gmail_query: 'from:alphasignal.ai'\n",
        encoding="utf-8",
    )


def test_unknown_source_lists_enabled_ids(tmp_path: Path) -> None:
    sources_path = tmp_path / "sources.yaml"
    write_sources(sources_path)

    with pytest.raises(
        ValueError,
        match="unknown or disabled source_id 'ai-newspaper'; enabled source IDs: alphasignal",
    ):
        run_pipeline(Settings(sources_config_path=sources_path), source_id="ai-newspaper", dry_run=True)


def test_no_enabled_sources_has_distinct_error(tmp_path: Path) -> None:
    sources_path = tmp_path / "sources.yaml"
    write_sources(sources_path, enabled=False)

    with pytest.raises(ValueError, match="no enabled sources configured"):
        run_pipeline(Settings(sources_config_path=sources_path), dry_run=True)


def test_deliver_digest_only_sends_selected_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = Database(tmp_path / "test.sqlite3")
    first_id = database.save_digest("daily:1", "start", "end", "UTC", "old digest")
    current_id = database.save_digest("daily:2", "start", "end", "UTC", "current digest")
    assert first_id is not None and current_id is not None
    delivered: list[str] = []

    def fake_deliver(webhook_url: str, content: str, username: str) -> list[str]:
        delivered.append(content)
        return ["discord-1"]

    monkeypatch.setattr(pipeline, "deliver", fake_deliver)

    deliver_digest(Settings(), database, current_id)

    assert delivered == ["current digest"]
    assert database.pending_digest(first_id) is not None
    assert database.pending_digest(current_id) is None
    database.close()


def test_ollama_failure_marks_one_message_failed_and_continues(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources_path = tmp_path / "sources.yaml"
    write_sources(sources_path)
    settings = Settings(
        sources_config_path=sources_path,
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
    )

    class FakeGmailClient:
        def __init__(self) -> None:
            self.applied_labels: list[tuple[str, list[str]]] = []

        def ensure_labels(self) -> None:
            pass

        def list_messages(self, query: str, limit: int) -> list[str]:
            return ["bad", "good"]

        def get_message(self, message_id: str) -> dict[str, object]:
            return {"internalDate": "0", "threadId": message_id, "payload": {"body": message_id}}

        def add_labels(self, message_id: str, labels: list[str]) -> None:
            self.applied_labels.append((message_id, labels))

    class FakeOllamaClient:
        def __init__(self, *args: object) -> None:
            pass

        def extract(self, source_id: str, content: str, truncated: bool, max_items: int) -> EmailExtraction:
            if content == "bad":
                raise ValueError("OLLAMA_SCHEMA_INVALID")
            return EmailExtraction(
                source_id=source_id,
                newsletter_title="Good news",
                newsletter_date=None,
                overview_zh_tw="摘要",
                items=[
                    {
                        "title": "Good item",
                        "category": "AI_MODEL",
                        "summary_zh_tw": "內容",
                        "why_it_matters_zh_tw": "原因",
                        "importance": 8,
                        "confidence": 0.9,
                    }
                ],
            )

    gmail = FakeGmailClient()
    monkeypatch.setattr(pipeline, "credentials", lambda *args: object())
    monkeypatch.setattr(pipeline, "GmailClient", lambda credentials: gmail)
    monkeypatch.setattr(pipeline, "OllamaClient", FakeOllamaClient)
    monkeypatch.setattr(pipeline, "extract_gmail_payload", lambda payload: str(payload["body"]))

    result = run_pipeline(settings, no_deliver=True)

    assert result == {"status": "partial", "discovered": 2, "processed": 1, "failed": 1, "delivered": 0}
    assert gmail.applied_labels == [
        ("bad", ["NewsletterBot/Failed"]),
        ("good", ["NewsletterBot/Processed"]),
    ]
    database = Database(settings.database_path)
    rows = database.connection.execute("SELECT gmail_message_id, state, last_error_code FROM messages ORDER BY id").fetchall()
    assert [tuple(row) for row in rows] == [
        ("bad", "failed", "OLLAMA_EXTRACTION_FAILED"),
        ("good", "processed", None),
    ]
    database.close()
