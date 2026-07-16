from pathlib import Path

import pytest

from two_much_two_read.migrate import migrate_calendar, migrate_newsletter


def test_migrate_newsletter_moves_runtime_files_and_preserves_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    legacy_config = home / ".config" / "newsletter-digest"
    legacy_data = home / ".local" / "share" / "newsletter-digest"
    legacy_config.mkdir(parents=True)
    legacy_data.mkdir(parents=True)
    for name in ("google-client-secret.json", "google-token.json", "sources.yaml", "excluded-subscriptions.yaml"):
        (legacy_config / name).write_text(name, encoding="utf-8")
    for name in ("newsletter-digest.sqlite3", "newsletter-digest.lock"):
        (legacy_data / name).write_text(name, encoding="utf-8")
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
        "2much2read.lock",
    }
    assert not legacy_env.exists()
    assert (target_config / "gmail-client-secret.json").is_file()
    assert (target_config / "gmail-token.json").is_file()
    assert (target_config / "sources.yaml").is_file()
    assert (target_config / "excluded-subscriptions.yaml").is_file()
    assert (target_data / "2much2read.sqlite3").is_file()
    assert (target_data / "2much2read.lock").is_file()
    env = (target_config / ".2much2read.env").read_text(encoding="utf-8")
    assert "OLLAMA_MODEL=custom-model" in env
    assert f"DATABASE_PATH={target_data / '2much2read.sqlite3'}" in env


def test_migrate_calendar_moves_distinct_explicit_client_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    legacy_config = home / ".config" / "2busy1miss"
    legacy_data = home / ".local" / "share" / "2busy1miss"
    legacy_config.mkdir(parents=True)
    legacy_data.mkdir(parents=True)
    for name in ("google-calendar-token.json", "reminders.yaml"):
        (legacy_config / name).write_text(name, encoding="utf-8")
    for name in ("2busy1miss.sqlite3", "2busy1miss.lock"):
        (legacy_data / name).write_text(name, encoding="utf-8")
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
        "2busy1miss.lock",
    }
    assert not client_secret.exists()
    assert (target_config / "calendar-client-secret.json").is_file()
    assert (target_config / "calendar-token.json").is_file()
    assert (target_config / "reminders.yaml").is_file()
    assert (target_data / "2busy1miss.sqlite3").is_file()
    assert "REMINDER_TIMEZONE=America/Toronto" in (target_config / ".2busy1miss.env").read_text(encoding="utf-8")


def test_migration_refuses_to_overwrite_existing_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    legacy_config = home / ".config" / "newsletter-digest"
    target_config = home / ".config" / "2much2read"
    legacy_config.mkdir(parents=True)
    target_config.mkdir(parents=True)
    (legacy_config / "google-client-secret.json").write_text("legacy", encoding="utf-8")
    (target_config / "gmail-client-secret.json").write_text("current", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    with pytest.raises(ValueError, match="already exists"):
        migrate_newsletter([])
