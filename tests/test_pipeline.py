from __future__ import annotations

import sqlite3
from base64 import urlsafe_b64encode
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from two_much_two_read import mail_operations, pipeline
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
        self.applied_labels: list[tuple[str, str]] = []

    def ensure_labels(self) -> None:
        pass

    def list_messages(self, query: str, limit: int | None = None) -> list[str]:
        return self.message_ids if limit is None else self.message_ids[:limit]

    def get_message(self, message_id: str) -> dict[str, object]:
        return self.messages[message_id]

    def sync_processing_label(self, message_id: str, state: str) -> None:
        self.applied_labels.append((message_id, state))


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
        "reason": None,
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


def test_reset_corrupt_delivery_checkpoint(tmp_path: Path) -> None:
    settings = Settings(database_path=tmp_path / "digest.sqlite3", lock_path=tmp_path / "digest.lock")
    database = Database(settings.database_path)
    digest_id = database.save_digest("daily:corrupt", "start", "end", "UTC", "corrupt")
    assert digest_id is not None
    database.connection.execute(
        """UPDATE digests SET state='failed', discord_message_ids_json='not json',
        last_error_code='DISCORD_MESSAGE_IDS_CORRUPT' WHERE id=?""",
        (digest_id,),
    )
    database.connection.commit()
    database.close()

    assert pipeline.reset_corrupt_delivery(settings, digest_id).model_dump() == {"status": "ok", "digest_id": digest_id}

    database = Database(settings.database_path)
    row = database.connection.execute(
        "SELECT state,discord_message_ids_json,last_error_code FROM digests WHERE id=?", (digest_id,)
    ).fetchone()
    assert tuple(row) == ("pending", None, None)
    assert not database.reset_corrupt_delivery(digest_id)
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
    list_calls: list[int | None] = []

    class FakeGmailClient:
        def ensure_labels(self) -> None:
            pass

        def list_messages(self, query: str, limit: int | None = None) -> list[str]:
            list_calls.append(limit)
            return ["first-1", "first-2"] if "first@example.com" in query else ["second-1"]

        def get_message(self, message_id: str) -> dict[str, object]:
            return {"internalDate": "0", "threadId": message_id, "payload": {"body": message_id}}

        def sync_processing_label(self, message_id: str, state: str) -> None:
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

    assert list_calls == [None, None]
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
            self.applied_labels: list[tuple[str, str]] = []

        def ensure_labels(self) -> None:
            pass

        def list_messages(self, query: str, limit: int | None = None) -> list[str]:
            return ["bad", "good"]

        def get_message(self, message_id: str) -> dict[str, object]:
            return {"internalDate": "0", "threadId": message_id, "payload": {"body": message_id}}

        def sync_processing_label(self, message_id: str, state: str) -> None:
            self.applied_labels.append((message_id, state))

    class FakeOllamaClient:
        def __init__(self, *args: object) -> None:
            pass

        def extract(self, source_id: str, content: str, truncated: bool, max_items: int) -> EmailExtraction:
            if content == "bad":
                raise OllamaSchemaError("OLLAMA_SCHEMA_INVALID error='missing category' response_preview='newsletter body'")
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

    statuses: list[str] = []
    result = run_pipeline(settings, no_deliver=True, status=statuses.append)

    assert result.model_dump() == {
        "status": "partial",
        "discovered": 2,
        "processed": 1,
        "failed": 1,
        "delivered": 0,
        "reason": None,
    }
    assert gmail.applied_labels == [
        ("bad", "failed"),
        ("good", "processed"),
    ]
    assert statuses == [
        "Starting 1 source(s)",
        "alphasignal: 2 message(s)",
        "alphasignal: extracting bad",
        "alphasignal: failed bad (OLLAMA_SCHEMA_INVALID error='missing category')",
        "alphasignal: extracting good",
        "alphasignal: processed good",
    ]
    database = Database(settings.database_path)
    rows = database.connection.execute("SELECT gmail_message_id, state, last_error_code FROM messages ORDER BY id").fetchall()
    assert [tuple(row) for row in rows] == [
        ("bad", "failed", "OLLAMA_SCHEMA_INVALID error='missing category'"),
        ("good", "processed", None),
    ]
    database.close()


def test_mime_failure_marks_one_message_failed_and_continues(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources_path = tmp_path / "sources.yaml"
    write_sources(sources_path)
    settings = Settings(
        sources_config_path=sources_path,
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
    )
    good_body = urlsafe_b64encode(b"good newsletter").decode().rstrip("=")
    gmail = StubGmailClient(
        ["bad", "good"],
        {
            "bad": {
                "internalDate": "0",
                "threadId": "bad",
                "payload": {"mimeType": "text/plain", "headers": [{"name": "Subject", "value": "Bad"}], "body": {}},
            },
            "good": {
                "internalDate": "0",
                "threadId": "good",
                "payload": {
                    "mimeType": "text/plain",
                    "headers": [{"name": "Subject", "value": "Good"}],
                    "body": {"data": good_body},
                },
            },
        },
    )
    extraction = EmailExtraction(
        source_id="alphasignal",
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
    monkeypatch.setattr(pipeline, "credentials", lambda *args: object())
    monkeypatch.setattr(pipeline, "GmailClient", lambda _: gmail)
    monkeypatch.setattr(pipeline, "create_ollama_client", lambda _: StubOllamaClient(extraction))

    assert run_pipeline(settings, no_deliver=True).model_dump() == {
        "status": "partial",
        "discovered": 2,
        "processed": 1,
        "failed": 1,
        "delivered": 0,
        "reason": None,
    }
    assert gmail.applied_labels == [("bad", "failed"), ("good", "processed")]
    database = Database(settings.database_path)
    rows = database.connection.execute("SELECT gmail_message_id,state,last_error_code FROM messages ORDER BY id").fetchall()
    assert [tuple(row) for row in rows] == [("bad", "failed", "EMAIL_NO_USABLE_TEXT"), ("good", "processed", None)]
    database.close()


def test_digest_render_failure_leaves_extractions_retryable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources_path = tmp_path / "sources.yaml"
    write_sources(sources_path)
    settings = Settings(
        sources_config_path=sources_path,
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
    )
    gmail = StubGmailClient(
        ["newsletter"],
        {"newsletter": {"internalDate": "0", "threadId": "thread", "payload": {"body": {"body": "newsletter"}}}},
    )
    extraction = EmailExtraction(
        source_id="alphasignal",
        newsletter_title="News",
        newsletter_date=None,
        overview_zh_tw="摘要",
        items=[
            {
                "title": "Item",
                "category": "AI_MODEL",
                "summary_zh_tw": "內容",
                "why_it_matters_zh_tw": "原因",
                "importance": 8,
                "confidence": 0.9,
            }
        ],
    )
    monkeypatch.setattr(pipeline, "credentials", lambda *args: object())
    monkeypatch.setattr(pipeline, "GmailClient", lambda _: gmail)
    monkeypatch.setattr(pipeline, "create_ollama_client", lambda _: StubOllamaClient(extraction))
    monkeypatch.setattr(pipeline, "extract_gmail_payload", lambda payload: str(payload["body"]))
    monkeypatch.setattr(pipeline, "render_digest", lambda *args: (_ for _ in ()).throw(RuntimeError("render failed")))

    with pytest.raises(RuntimeError, match="render failed"):
        run_pipeline(settings, no_deliver=True)

    database = Database(settings.database_path)
    assert database.connection.execute("SELECT state FROM messages").fetchone()["state"] == "discovered"
    assert len(database.items_for_messages([1], 10)) == 1
    database.close()
    assert gmail.applied_labels == []

    monkeypatch.setattr(pipeline, "render_digest", lambda *args: "digest")
    assert run_pipeline(settings, no_deliver=True).status == "ok"
    assert gmail.applied_labels == [("newsletter", "processed")]
    database = Database(settings.database_path)
    assert database.connection.execute("SELECT state FROM messages").fetchone()["state"] == "processed"
    assert len(database.items_for_messages([1], 10)) == 1
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
        "reason": None,
    }
    assert gmail.applied_labels == [("transient", "processed")]


@pytest.mark.parametrize("state", ["pending", "failed", "delivered"])
def test_existing_daily_digest_skips_before_gmail_access(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    sources_path = tmp_path / "sources.yaml"
    write_sources(sources_path)
    settings = Settings(
        sources_config_path=sources_path,
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
    )
    now = datetime(2026, 7, 22, 8, tzinfo=ZoneInfo("America/Montreal"))
    database = Database(settings.database_path)
    digest_id = database.save_digest("daily:2026-07-22:America/Montreal:all", "start", "end", "America/Montreal", "digest")
    assert digest_id is not None
    if state == "failed":
        database.fail_delivery(digest_id)
    elif state == "delivered":
        database.finish_delivery(digest_id, ["discord-1"])
    database.close()

    monkeypatch.setattr(pipeline, "credentials", lambda *args: pytest.fail("Gmail must not be accessed"))

    assert run_pipeline(settings, now=now).model_dump() == {
        "status": "skipped",
        "discovered": 0,
        "processed": 0,
        "failed": 0,
        "delivered": 0,
        "reason": "daily_digest_exists",
    }
    database = Database(settings.database_path)
    assert database.connection.execute("SELECT status FROM runs ORDER BY id DESC").fetchone()[0] == "skipped"
    database.close()


def test_forced_run_uses_a_separate_digest_key_after_daily_reservation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources_path = tmp_path / "sources.yaml"
    write_sources(sources_path)
    settings = Settings(
        sources_config_path=sources_path,
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
    )
    now = datetime(2026, 7, 22, 8, tzinfo=ZoneInfo("America/Montreal"))
    database = Database(settings.database_path)
    assert database.save_digest("daily:2026-07-22:America/Montreal:all", "start", "end", "America/Montreal", "digest")
    database.close()

    class FakeGmailClient:
        def ensure_labels(self) -> None:
            pass

    monkeypatch.setattr(pipeline, "credentials", lambda *args: object())
    monkeypatch.setattr(pipeline, "GmailClient", lambda _: FakeGmailClient())
    monkeypatch.setattr(pipeline, "_process_source", lambda *args, **kwargs: (0, 0, 0, 0, []))
    monkeypatch.setattr(pipeline, "render_digest", lambda *args: "forced digest")

    assert run_pipeline(settings, force=True, no_deliver=True, now=now).status == "ok"
    database = Database(settings.database_path)
    keys = [row[0] for row in database.connection.execute("SELECT digest_key FROM digests ORDER BY id")]
    assert keys == [
        "daily:2026-07-22:America/Montreal:all",
        "daily:2026-07-22:America/Montreal:all:force:2026-07-22T12:00:00+00:00",
    ]
    database.close()


def test_label_sync_failure_is_repaired_without_reextracting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources_path = tmp_path / "sources.yaml"
    write_sources(sources_path)
    settings = Settings(
        sources_config_path=sources_path,
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
    )
    calls: list[str] = []

    class FakeGmailClient:
        fail_sync = True

        def ensure_labels(self) -> None:
            pass

        def list_messages(self, query: str, limit: int | None = None) -> list[str]:
            return ["gmail-1"]

        def get_message(self, message_id: str) -> dict[str, object]:
            calls.append("get")
            return {"internalDate": "0", "threadId": message_id, "payload": {"body": "body"}}

        def sync_processing_label(self, message_id: str, state: str) -> None:
            calls.append(f"label:{state}")
            if self.fail_sync:
                raise RuntimeError("Gmail unavailable")

    class FakeOllama:
        def extract(self, *args: object) -> EmailExtraction:
            calls.append("extract")
            return EmailExtraction(
                source_id="alphasignal", newsletter_title="News", newsletter_date=None, overview_zh_tw="摘要", items=[]
            )

    gmail = FakeGmailClient()
    monkeypatch.setattr(pipeline, "credentials", lambda *args: object())
    monkeypatch.setattr(pipeline, "GmailClient", lambda _: gmail)
    monkeypatch.setattr(pipeline, "create_ollama_client", lambda _: FakeOllama())
    monkeypatch.setattr(pipeline, "extract_gmail_payload", lambda payload: str(payload["body"]))

    assert (
        run_pipeline(settings, no_deliver=True, now=datetime(2026, 7, 22, tzinfo=ZoneInfo("America/Montreal"))).status
        == "partial"
    )
    database = Database(settings.database_path)
    assert tuple(database.connection.execute("SELECT state,error_code FROM message_label_sync").fetchone()) == (
        "failed",
        "GMAIL_LABEL_SYNC_FAILED",
    )
    database.close()

    gmail.fail_sync = False
    assert run_pipeline(settings, no_deliver=True, now=datetime(2026, 7, 23, tzinfo=ZoneInfo("America/Montreal"))).processed == 0
    assert calls == ["get", "extract", "label:processed", "label:processed"]
    database = Database(settings.database_path)
    assert tuple(database.connection.execute("SELECT state,error_code FROM message_label_sync").fetchone()) == ("synced", None)
    database.close()


def test_stale_label_reconciliation_does_not_use_the_message_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources_path = tmp_path / "sources.yaml"
    write_sources(sources_path)
    settings = Settings(
        sources_config_path=sources_path,
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
    )
    database = Database(settings.database_path)
    stale_id = database.discover("stale", "stale", "alphasignal", "now", "subject", "sender", "body")
    assert stale_id is not None
    database.store_extraction(
        stale_id,
        EmailExtraction(source_id="alphasignal", newsletter_title="Old", newsletter_date=None, overview_zh_tw="摘要", items=[]),
    )
    database.close()
    fetched: list[str] = []

    class FakeGmailClient:
        def ensure_labels(self) -> None:
            pass

        def list_messages(self, query: str, limit: int | None = None) -> list[str]:
            return ["stale", "new"]

        def get_message(self, message_id: str) -> dict[str, object]:
            fetched.append(message_id)
            return {"internalDate": "0", "threadId": message_id, "payload": {"body": message_id}}

        def sync_processing_label(self, message_id: str, state: str) -> None:
            assert message_id == "stale" or state == "processed"

    class FakeOllama:
        def extract(self, *args: object) -> EmailExtraction:
            return EmailExtraction(
                source_id="alphasignal", newsletter_title="New", newsletter_date=None, overview_zh_tw="摘要", items=[]
            )

    monkeypatch.setattr(pipeline, "credentials", lambda *args: object())
    monkeypatch.setattr(pipeline, "GmailClient", lambda _: FakeGmailClient())
    monkeypatch.setattr(pipeline, "create_ollama_client", lambda _: FakeOllama())
    monkeypatch.setattr(pipeline, "extract_gmail_payload", lambda payload: str(payload["body"]))

    assert run_pipeline(settings, max_messages=1, no_deliver=True).processed == 1
    assert fetched == ["new"]


def test_forced_recovery_clears_the_failure_and_remote_failed_label(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources_path = tmp_path / "sources.yaml"
    write_sources(sources_path)
    settings = Settings(
        sources_config_path=sources_path,
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
    )
    database = Database(settings.database_path)
    message_id = database.discover("gmail-1", "thread", "alphasignal", "now", "subject", "sender", "body")
    assert message_id is not None
    database.fail_message(message_id, "OLLAMA_SCHEMA_INVALID")
    database.close()
    synced: list[tuple[str, str]] = []

    class FakeGmailClient:
        def ensure_labels(self) -> None:
            pass

        def list_messages(self, query: str, limit: int | None = None) -> list[str]:
            return ["gmail-1"]

        def get_message(self, message_id: str) -> dict[str, object]:
            return {"internalDate": "0", "threadId": "thread", "payload": {"body": "body"}}

        def sync_processing_label(self, message_id: str, state: str) -> None:
            synced.append((message_id, state))

    class FakeOllama:
        def extract(self, *args: object) -> EmailExtraction:
            return EmailExtraction(
                source_id="alphasignal", newsletter_title="Recovered", newsletter_date=None, overview_zh_tw="摘要", items=[]
            )

    monkeypatch.setattr(pipeline, "credentials", lambda *args: object())
    monkeypatch.setattr(pipeline, "GmailClient", lambda _: FakeGmailClient())
    monkeypatch.setattr(pipeline, "create_ollama_client", lambda _: FakeOllama())
    monkeypatch.setattr(pipeline, "extract_gmail_payload", lambda payload: str(payload["body"]))

    assert run_pipeline(settings, force=True, no_deliver=True).processed == 1
    assert synced == [("gmail-1", "processed")]
    database = Database(settings.database_path)
    assert tuple(database.connection.execute("SELECT state,last_error_code FROM messages").fetchone()) == ("processed", None)
    database.close()


def test_dry_run_skips_gmail_label_writes_and_persistent_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources_path = tmp_path / "sources.yaml"
    write_sources(sources_path)
    settings = Settings(
        sources_config_path=sources_path,
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
    )

    class FakeGmailClient:
        def ensure_labels(self) -> None:
            pytest.fail("dry-run must not create labels")

        def list_messages(self, query: str, limit: int | None = None) -> list[str]:
            return ["gmail-1"]

        def get_message(self, message_id: str) -> dict[str, object]:
            return {"internalDate": "0", "threadId": "thread", "payload": {"body": "body"}}

        def sync_processing_label(self, message_id: str, state: str) -> None:
            pytest.fail("dry-run must not modify labels")

    class FakeOllama:
        def extract(self, *args: object) -> EmailExtraction:
            return EmailExtraction(
                source_id="alphasignal", newsletter_title="Preview", newsletter_date=None, overview_zh_tw="摘要", items=[]
            )

    monkeypatch.setattr(pipeline, "credentials", lambda *args: object())
    monkeypatch.setattr(pipeline, "GmailClient", lambda _: FakeGmailClient())
    monkeypatch.setattr(pipeline, "create_ollama_client", lambda _: FakeOllama())
    monkeypatch.setattr(pipeline, "extract_gmail_payload", lambda payload: str(payload["body"]))

    assert run_pipeline(settings, dry_run=True).processed == 1
    assert not settings.database_path.exists()


def test_labels_reconcile_repairs_terminal_messages_and_records_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(database_path=tmp_path / "digest.sqlite3", lock_path=tmp_path / "digest.lock")
    database = Database(settings.database_path)
    processed_id = database.discover("processed", "thread", "source", "now", "subject", "sender", "body")
    failed_id = database.discover("failed", "thread", "source", "now", "subject", "sender", "body")
    assert processed_id is not None and failed_id is not None
    database.store_extraction(
        processed_id,
        EmailExtraction(source_id="source", newsletter_title="News", newsletter_date=None, overview_zh_tw="摘要", items=[]),
    )
    database.fail_message(failed_id, "OLLAMA_SCHEMA_INVALID")
    database.close()
    calls: list[tuple[str, str]] = []

    class FakeGmailClient:
        def ensure_labels(self) -> None:
            pass

        def sync_processing_label(self, gmail_id: str, state: str) -> None:
            calls.append((gmail_id, state))
            if gmail_id == "failed":
                raise RuntimeError("Gmail unavailable")

    monkeypatch.setattr(mail_operations, "credentials", lambda *args: object())
    monkeypatch.setattr(mail_operations, "GmailClient", lambda _: FakeGmailClient())

    assert mail_operations.reconcile_labels(settings).model_dump() == {"status": "partial", "reconciled": 1, "failed": 1}
    assert calls == [("processed", "processed"), ("failed", "failed")]
    database = Database(settings.database_path)
    rows = database.connection.execute("SELECT state,error_code FROM message_label_sync ORDER BY message_id").fetchall()
    assert [tuple(row) for row in rows] == [("synced", None), ("failed", "GMAIL_LABEL_SYNC_FAILED")]
    database.close()
