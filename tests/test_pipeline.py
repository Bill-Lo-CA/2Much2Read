from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from two_much_two_read import pipeline
from two_much_two_read.command_models import NewsletterRetryResult, NewsletterRunResult
from two_much_two_read.config import Settings
from two_much_two_read.ollama import OllamaSchemaError
from two_much_two_read.pipeline import deliver_digest, run_pipeline
from two_much_two_read.schemas import EmailExtraction
from two_much_two_read.storage import Database
from two_read_runtime.discord import DiscordDeliveryError


def write_sources(path: Path, *, enabled: bool = True) -> None:
    path.write_text(
        f"sources:\n  - id: alphasignal\n    name: AlphaSignal\n    enabled: {str(enabled).lower()}\n"
        "    gmail_query: 'from:alphasignal.ai'\n",
        encoding="utf-8",
    )


class StubGmailClient:
    def __init__(self, message_ids: list[str], messages: dict[str, dict[str, object]]) -> None:
        self.message_ids = message_ids
        self.messages = messages
        self.applied_labels: list[tuple[str, list[str]]] = []

    def ensure_labels(self) -> None:
        pass

    def list_messages(self, query: str, limit: int) -> list[str]:
        return self.message_ids[:limit]

    def get_message(self, message_id: str) -> dict[str, object]:
        return self.messages[message_id]

    def add_labels(self, message_id: str, labels: list[str]) -> None:
        self.applied_labels.append((message_id, labels))


class StubOllamaClient:
    def __init__(self, extraction: EmailExtraction | None = None, error: Exception | None = None) -> None:
        self.extraction = extraction
        self.error = error

    def extract(self, source_id: str, content: str, truncated: bool, max_items: int) -> EmailExtraction:
        if self.error is not None:
            raise self.error
        assert self.extraction is not None
        return self.extraction


class FakeDigestDatabase:
    def __init__(self, pending: list[dict[str, object]], failure_error: Exception | None = None) -> None:
        self.pending = pending
        self.failure_error = failure_error
        self.failed: list[tuple[int, str]] = []
        self.finished: list[tuple[int, list[str]]] = []
        self.progress: list[tuple[int, list[str]]] = []
        self.closed = False

    def pending_digests(self) -> list[dict[str, object]]:
        return self.pending

    def record_delivery_progress(self, digest_id: int, message_ids: list[str]) -> None:
        self.progress.append((digest_id, message_ids))

    def finish_delivery(self, digest_id: int, message_ids: list[str]) -> None:
        self.finished.append((digest_id, message_ids))

    def fail_delivery(self, digest_id: int, error_code: str) -> None:
        if self.failure_error is not None:
            raise self.failure_error
        self.failed.append((digest_id, error_code))

    def close(self) -> None:
        self.closed = True


class RecordingLock:
    def __init__(self) -> None:
        self.entered = False

    def __enter__(self) -> RecordingLock:
        self.entered = True
        return self

    def __exit__(self, *args: object) -> None:
        pass


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


def test_empty_news_day_records_no_content_run(newsletter_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = newsletter_settings
    write_sources(settings.sources_config_path)
    gmail = StubGmailClient([], {})
    monkeypatch.setattr(pipeline, "credentials", lambda *args: object())
    monkeypatch.setattr(pipeline, "GmailClient", lambda credentials: gmail)

    result = run_pipeline(settings, no_deliver=True)

    assert isinstance(result, NewsletterRunResult)
    assert result.model_dump() == {
        "status": "no_content",
        "discovered": 0,
        "processed": 0,
        "failed": 0,
        "delivered": 0,
    }

    database = Database(settings.database_path)
    row = database.connection.execute(
        "SELECT run_type,status,discovered_count,processed_count,failed_count,delivered_digest_count FROM runs"
    ).fetchone()
    assert tuple(row) == ("newsletter_digest", "no_content", 0, 0, 0, 0)
    database.close()


def test_run_pipeline_uses_one_captured_time_for_digest_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources_path = tmp_path / "sources.yaml"
    write_sources(sources_path)
    settings = Settings(
        sources_config_path=sources_path,
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
    )

    class FakeGmailClient:
        def ensure_labels(self) -> None:
            pass

    monkeypatch.setattr(pipeline, "credentials", lambda *args: object())
    monkeypatch.setattr(pipeline, "GmailClient", lambda _: FakeGmailClient())
    monkeypatch.setattr(pipeline, "_process_source", lambda *args, **kwargs: (0, 0, 0, 0, []))
    monkeypatch.setattr(pipeline, "render_digest", lambda *args: "digest")
    now = datetime(2026, 1, 1, 9, 30, tzinfo=ZoneInfo("America/Montreal"))

    assert run_pipeline(settings, no_deliver=True, force=True, now=now).status == "ok"

    database = Database(settings.database_path)
    row = database.connection.execute("SELECT digest_key,period_start,period_end FROM digests").fetchone()
    assert tuple(row) == (
        "daily:2026-01-01:America/Montreal:all:force:2026-01-01T14:30:00+00:00",
        "2025-12-31T09:30:00-05:00",
        "2026-01-01T09:30:00-05:00",
    )
    database.close()


def test_credentials_failure_records_a_failed_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources_path = tmp_path / "sources.yaml"
    write_sources(sources_path)
    settings = Settings(
        sources_config_path=sources_path,
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
    )
    monkeypatch.setattr(
        pipeline,
        "credentials",
        lambda *args: (_ for _ in ()).throw(ValueError("AUTH_REAUTH_REQUIRED: run '2much2read auth gmail'")),
    )

    with pytest.raises(ValueError, match="AUTH_REAUTH_REQUIRED"):
        run_pipeline(settings, no_deliver=True)

    database = Database(settings.database_path)
    row = database.connection.execute(
        "SELECT run_type,status,discovered_count,processed_count,failed_count,delivered_digest_count,error_summary FROM runs"
    ).fetchone()
    assert tuple(row) == ("newsletter_digest", "failed", 0, 0, 0, 0, "ValueError")
    database.close()


def test_deliver_digest_only_sends_selected_digest(
    newsletter_database: Database, newsletter_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = newsletter_database
    first_id = database.save_digest("daily:1", "start", "end", "UTC", "old digest")
    current_id = database.save_digest("daily:2", "start", "end", "UTC", "current digest")
    assert first_id is not None and current_id is not None
    delivered: list[str] = []

    def fake_deliver(webhook_url: str, content: str, username: str, *args: object, **kwargs: object) -> list[str]:
        delivered.append(content)
        return ["discord-1"]

    monkeypatch.setattr(pipeline, "deliver", fake_deliver)

    deliver_digest(newsletter_settings, database, current_id)

    assert delivered == ["current digest"]
    assert database.pending_digest(first_id) is not None
    assert database.pending_digest(current_id) is None


def test_retry_delivery_holds_process_lock(newsletter_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = newsletter_settings
    database = FakeDigestDatabase([{"id": 1, "rendered_content": "content", "discord_message_ids_json": None}])
    lock = RecordingLock()
    monkeypatch.setattr(pipeline, "Database", lambda _: database)
    monkeypatch.setattr(pipeline, "ProcessLock", lambda _: lock)

    def fake_deliver(*args: object) -> list[str]:
        assert lock.entered
        return ["discord-id"]

    monkeypatch.setattr(pipeline, "deliver", fake_deliver)

    result = pipeline.retry_delivery(settings)

    assert isinstance(result, NewsletterRetryResult)
    assert result.model_dump() == {"status": "ok", "delivered": 1, "failed": 0, "failed_by_error_code": {}}
    assert database.finished == [(1, ["discord-id"])]
    assert database.closed


def test_retry_delivery_continues_after_a_failed_digest(newsletter_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = newsletter_settings
    database = FakeDigestDatabase(
        [
            {"id": 1, "rendered_content": "bad", "discord_message_ids_json": None},
            {"id": 2, "rendered_content": "good", "discord_message_ids_json": None},
        ]
    )

    def fake_deliver(*args: object) -> list[str]:
        if args[1] == "bad":
            raise DiscordDeliveryError("delivery failed")
        return ["discord-id"]

    monkeypatch.setattr(pipeline, "deliver", fake_deliver)

    assert pipeline.retry_delivery(settings, database).model_dump() == {
        "status": "ok",
        "delivered": 1,
        "failed": 1,
        "failed_by_error_code": {"DISCORD_DELIVERY_FAILED": 1},
    }
    assert database.failed == [(1, "DISCORD_DELIVERY_FAILED")]
    assert database.finished == [(2, ["discord-id"])]


def test_retry_delivery_stops_when_recording_a_failure_hits_the_database(
    newsletter_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = newsletter_settings
    database = FakeDigestDatabase(
        [
            {"id": 1, "rendered_content": "bad", "discord_message_ids_json": None},
            {"id": 2, "rendered_content": "good", "discord_message_ids_json": None},
        ],
        sqlite3.OperationalError("database unavailable"),
    )
    monkeypatch.setattr(pipeline, "deliver", lambda *args: (_ for _ in ()).throw(DiscordDeliveryError("delivery failed")))

    with pytest.raises(sqlite3.OperationalError, match="database unavailable"):
        pipeline.retry_delivery(settings, database)

    assert database.finished == []


def test_retry_delivery_preserves_corrupt_checkpoint_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(database_path=tmp_path / "digest.sqlite3", lock_path=tmp_path / "digest.lock")
    database = Database(settings.database_path)
    corrupt_id = database.save_digest("daily:corrupt", "start", "end", "UTC", "corrupt")
    good_id = database.save_digest("daily:good", "start", "end", "UTC", "good")
    assert corrupt_id is not None and good_id is not None
    database.connection.execute("UPDATE digests SET discord_message_ids_json='not json' WHERE id=?", (corrupt_id,))
    database.connection.commit()
    monkeypatch.setattr(pipeline, "deliver", lambda *args: ["discord-id"])

    assert pipeline.retry_delivery(settings, database).model_dump() == {
        "status": "ok",
        "delivered": 1,
        "failed": 1,
        "failed_by_error_code": {"DISCORD_MESSAGE_IDS_CORRUPT": 1},
    }
    error_code = database.connection.execute("SELECT last_error_code FROM digests WHERE id=?", (corrupt_id,)).fetchone()[0]
    assert error_code == "DISCORD_MESSAGE_IDS_CORRUPT"
    assert database.pending_digest(good_id) is None
    database.close()


def test_run_pipeline_limits_messages_across_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text(
        "sources:\n"
        "  - id: first\n    name: First\n    gmail_query: from:first@example.com\n"
        "  - id: second\n    name: Second\n    gmail_query: from:second@example.com\n",
        encoding="utf-8",
    )
    settings = Settings(
        sources_config_path=sources_path,
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
    )
    list_calls: list[int] = []

    class FakeGmailClient:
        def ensure_labels(self) -> None:
            pass

        def list_messages(self, query: str, limit: int) -> list[str]:
            list_calls.append(limit)
            return ["first-1", "first-2"] if "first@example.com" in query else ["second-1"]

        def get_message(self, message_id: str) -> dict[str, object]:
            return {"internalDate": "0", "threadId": message_id, "payload": {"body": message_id}}

        def add_labels(self, message_id: str, labels: list[str]) -> None:
            pass

    class FakeOllamaClient:
        def __init__(self, *args: object) -> None:
            pass

        def extract(self, source_id: str, content: str, truncated: bool, max_items: int) -> EmailExtraction:
            return EmailExtraction(
                source_id=source_id, newsletter_title="Test", newsletter_date=None, overview_zh_tw="摘要", items=[]
            )

    monkeypatch.setattr(pipeline, "credentials", lambda *args: object())
    monkeypatch.setattr(pipeline, "GmailClient", lambda credentials: FakeGmailClient())
    monkeypatch.setattr(pipeline, "create_ollama_client", lambda _: FakeOllamaClient())
    monkeypatch.setattr(pipeline, "extract_gmail_payload", lambda payload: str(payload["body"]))

    result = run_pipeline(settings, max_messages=3, no_deliver=True)

    assert list_calls == [3, 1]
    assert result.processed == 3


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
                raise OllamaSchemaError("OLLAMA_SCHEMA_INVALID")
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
    monkeypatch.setattr(pipeline, "create_ollama_client", lambda _: FakeOllamaClient())
    monkeypatch.setattr(pipeline, "extract_gmail_payload", lambda payload: str(payload["body"]))

    result = run_pipeline(settings, no_deliver=True)

    assert result.model_dump() == {"status": "partial", "discovered": 2, "processed": 1, "failed": 1, "delivered": 0}
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


def test_ollama_transport_failure_remains_retryable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources_path = tmp_path / "sources.yaml"
    write_sources(sources_path)
    settings = Settings(
        sources_config_path=sources_path,
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
    )
    gmail = StubGmailClient(
        ["transient"],
        {"transient": {"internalDate": "0", "threadId": "transient", "payload": {"body": "transient"}}},
    )
    ollama = StubOllamaClient(error=httpx.ConnectError("Ollama unavailable"))
    monkeypatch.setattr(pipeline, "credentials", lambda *args: object())
    monkeypatch.setattr(pipeline, "GmailClient", lambda credentials: gmail)
    monkeypatch.setattr(pipeline, "create_ollama_client", lambda _: ollama)
    monkeypatch.setattr(pipeline, "extract_gmail_payload", lambda payload: str(payload["body"]))

    with pytest.raises(httpx.ConnectError, match="Ollama unavailable"):
        run_pipeline(settings, no_deliver=True)

    assert gmail.applied_labels == []
    database = Database(settings.database_path)
    assert database.connection.execute("SELECT state FROM messages").fetchone()["state"] == "discovered"
    assert tuple(database.connection.execute("SELECT status,error_summary FROM runs").fetchone()) == ("failed", "ConnectError")
    database.close()

    ollama.error = None
    ollama.extraction = EmailExtraction(
        source_id="alphasignal",
        newsletter_title="Recovered",
        newsletter_date=None,
        overview_zh_tw="摘要",
        items=[
            {
                "title": "Recovered item",
                "category": "AI_MODEL",
                "summary_zh_tw": "內容",
                "why_it_matters_zh_tw": "原因",
                "importance": 8,
                "confidence": 0.9,
            }
        ],
    )

    assert run_pipeline(settings, no_deliver=True).model_dump() == {
        "status": "ok",
        "discovered": 1,
        "processed": 1,
        "failed": 0,
        "delivered": 0,
    }
    assert gmail.applied_labels == [("transient", ["NewsletterBot/Processed"])]
