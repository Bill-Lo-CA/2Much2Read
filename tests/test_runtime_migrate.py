import sqlite3
from pathlib import Path

import pytest

from two_much_two_read import migrate
from two_much_two_read.migrate import migrate_calendar, migrate_newsletter


def write_database(path: Path, value: str) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE state(value TEXT NOT NULL)")
        connection.execute("INSERT INTO state VALUES(?)", (value,))
        connection.commit()
    finally:
        connection.close()


def database_value(path: Path) -> str:
    connection = sqlite3.connect(path)
    try:
        return str(connection.execute("SELECT value FROM state").fetchone()[0])
    finally:
        connection.close()


def test_migrate_newsletter_copies_runtime_before_removing_legacy_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    legacy_config = home / ".config" / "newsletter-digest"
    legacy_data = home / ".local" / "share" / "newsletter-digest"
    legacy_config.mkdir(parents=True)
    legacy_data.mkdir(parents=True)
    for name in ("google-client-secret.json", "google-token.json", "sources.yaml", "excluded-subscriptions.yaml"):
        (legacy_config / name).write_text(name, encoding="utf-8")
    database = legacy_data / "newsletter-digest.sqlite3"
    write_database(database, "newsletter state")
    lock = legacy_data / "newsletter-digest.lock"
    lock.write_text("stale", encoding="utf-8")
    legacy_env = tmp_path / ".env"
    legacy_env.write_text("OLLAMA_MODEL=custom-model\nSOURCES_CONFIG_PATH=missing.yaml\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    moved = migrate_newsletter([legacy_env])

    target_config = home / ".config" / "2much2read"
    target_data = home / ".local" / "share" / "2much2read"
    assert {path.name for path in moved} == {
        "gmail-client-secret.json",
        "gmail-token.json",
        "sources.yaml",
        "excluded-subscriptions.yaml",
        "2much2read.sqlite3",
        ".2much2read.env",
    }
    assert not legacy_env.exists()
    assert lock.exists()
    assert not (target_data / "2much2read.lock").exists()
    assert database_value(target_data / "2much2read.sqlite3") == "newsletter state"
    assert database_value(target_data / "2much2read.sqlite3.pre-migration-backup") == "newsletter state"
    assert not database.exists()
    assert target_config.stat().st_mode & 0o777 == 0o700
    assert target_data.stat().st_mode & 0o777 == 0o700
    assert (target_config / "gmail-token.json").stat().st_mode & 0o777 == 0o600
    env = (target_config / ".2much2read.env").read_text(encoding="utf-8")
    assert "OLLAMA_MODEL=custom-model" in env
    assert f"DATABASE_PATH={target_data / '2much2read.sqlite3'}" in env


def test_migrate_calendar_keeps_lock_and_is_rerunnable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    legacy_config = home / ".config" / "2busy1miss"
    legacy_data = home / ".local" / "share" / "2busy1miss"
    legacy_config.mkdir(parents=True)
    legacy_data.mkdir(parents=True)
    for name in ("google-calendar-token.json", "reminders.yaml"):
        (legacy_config / name).write_text(name, encoding="utf-8")
    database = legacy_data / "2busy1miss.sqlite3"
    write_database(database, "calendar state")
    lock = legacy_data / "2busy1miss.lock"
    lock.write_text("stale", encoding="utf-8")
    client_secret = tmp_path / "calendar-client.json"
    client_secret.write_text("calendar client", encoding="utf-8")
    legacy_env = legacy_config / "2busy1miss.env"
    legacy_env.write_text("REMINDER_TIMEZONE=America/Toronto\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    moved = migrate_calendar([legacy_env], client_secret)

    target_config = home / ".config" / "2much2read"
    target_data = home / ".local" / "share" / "2much2read"
    assert {path.name for path in moved} == {
        "calendar-client-secret.json",
        "calendar-token.json",
        "reminders.yaml",
        "2busy1miss.sqlite3",
        ".2busy1miss.env",
    }
    assert lock.exists()
    assert not (target_data / "2busy1miss.lock").exists()
    assert database_value(target_data / "2busy1miss.sqlite3") == "calendar state"
    assert "REMINDER_TIMEZONE=America/Toronto" in (target_config / ".2busy1miss.env").read_text(encoding="utf-8")
    assert migrate_calendar([legacy_env], client_secret) == []


def test_preflight_conflict_leaves_every_source_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    legacy_config = home / ".config" / "newsletter-digest"
    target_config = home / ".config" / "2much2read"
    legacy_config.mkdir(parents=True)
    target_config.mkdir(parents=True)
    source_secret = legacy_config / "google-client-secret.json"
    source_token = legacy_config / "google-token.json"
    source_secret.write_text("legacy", encoding="utf-8")
    source_token.write_text("token", encoding="utf-8")
    (target_config / "gmail-client-secret.json").write_text("current", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    with pytest.raises(ValueError, match="already exists"):
        migrate_newsletter([])

    assert source_secret.read_text(encoding="utf-8") == "legacy"
    assert source_token.read_text(encoding="utf-8") == "token"
    assert (target_config / "gmail-client-secret.json").read_text(encoding="utf-8") == "current"


def test_copy_failure_rolls_back_new_targets_without_touching_legacy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    legacy_config = home / ".config" / "newsletter-digest"
    legacy_config.mkdir(parents=True)
    for name in ("google-client-secret.json", "google-token.json"):
        (legacy_config / name).write_text(name, encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    original_copy = migrate._copy_file

    def fail_on_token(source: Path, destination: Path, content: str | None = None) -> None:
        if destination.name == "gmail-token.json":
            raise OSError("simulated failure")
        original_copy(source, destination, content)

    monkeypatch.setattr(migrate, "_copy_file", fail_on_token)

    with pytest.raises(OSError, match="simulated failure"):
        migrate_newsletter([])

    assert (legacy_config / "google-client-secret.json").read_text(encoding="utf-8") == "google-client-secret.json"
    assert (legacy_config / "google-token.json").read_text(encoding="utf-8") == "google-token.json"
    assert not (home / ".config" / "2much2read" / "gmail-client-secret.json").exists()


def test_cleanup_failure_restores_legacy_files_and_removes_new_targets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    legacy_config = home / ".config" / "newsletter-digest"
    legacy_config.mkdir(parents=True)
    for name in ("google-client-secret.json", "google-token.json"):
        (legacy_config / name).write_text(name, encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    original_remove = migrate._remove_source

    def fail_on_token(source: Path) -> None:
        if source.name == "google-token.json":
            raise OSError("simulated cleanup failure")
        original_remove(source)

    monkeypatch.setattr(migrate, "_remove_source", fail_on_token)

    with pytest.raises(OSError, match="simulated cleanup failure"):
        migrate_newsletter([])

    assert (legacy_config / "google-client-secret.json").read_text(encoding="utf-8") == "google-client-secret.json"
    assert (legacy_config / "google-token.json").read_text(encoding="utf-8") == "google-token.json"
    assert not (home / ".config" / "2much2read" / "gmail-client-secret.json").exists()


def test_migration_without_legacy_environment_leaves_new_template_to_installer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    migrate_calendar([])

    assert not (tmp_path / "home" / ".config" / "2much2read" / ".2busy1miss.env").exists()
