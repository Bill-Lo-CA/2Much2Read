from pathlib import Path

import pytest

from newsletter_digest import pipeline
from newsletter_digest.config import Settings
from newsletter_digest.pipeline import deliver_digest, run_pipeline
from newsletter_digest.storage import Database


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
