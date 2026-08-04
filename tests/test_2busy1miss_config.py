from datetime import time
from pathlib import Path

import pytest

from two_busy_one_miss.config import Settings, load_reminders


def test_loads_reminder_config(tmp_path: Path) -> None:
    path = tmp_path / "reminders.yaml"
    path.write_text(
        """
timezone: America/Montreal
calendars:
  - id: primary
default_rules:
  - id: default-5m
    before: 5m
rules: []
""",
        encoding="utf-8",
    )

    config = load_reminders(path)

    assert config.timezone == "America/Montreal"
    assert config.enabled_calendars[0].id == "primary"


def test_omitted_timezone_allows_settings_fallback(tmp_path: Path) -> None:
    path = tmp_path / "reminders.yaml"
    path.write_text(
        """
calendars:
  - id: primary
""",
        encoding="utf-8",
    )

    assert load_reminders(path).timezone is None


def test_rejects_invalid_offset(tmp_path: Path) -> None:
    path = tmp_path / "reminders.yaml"
    path.write_text(
        """
calendars:
  - id: primary
default_rules:
  - before: soon
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="offset"):
        load_reminders(path)


def test_rejects_excessive_offset_and_unknown_timezone(tmp_path: Path) -> None:
    path = tmp_path / "reminders.yaml"
    path.write_text(
        """
timezone: Not/A_Timezone
calendars:
  - id: primary
default_rules:
  - before: 367d
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="timezone|offset"):
        load_reminders(path)


def test_rejects_duplicate_calendar_ids(tmp_path: Path) -> None:
    path = tmp_path / "reminders.yaml"
    path.write_text(
        """
calendars:
  - id: primary
  - id: primary
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unique"):
        load_reminders(path)


def test_settings_ignore_repo_dotenv_and_use_private_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    home = isolated_home
    app_config = home / ".config" / "2much2read-runtime"
    app_config.mkdir(parents=True)
    (tmp_path / ".env").write_text(
        "DISCORD_WEBHOOK_URL=https://newsletter.example/webhook\nDATABASE_PATH=newsletter.sqlite3\n",
        encoding="utf-8",
    )
    (app_config / ".2busy1miss.env").write_text(
        "DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/123456789012345678/test-webhook-token\nDATABASE_PATH=/tmp/2busy1miss.sqlite3\nAGENDA_SCHEDULE_TIME=20:30\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("DATABASE_PATH", raising=False)

    settings = Settings()

    assert settings.discord_webhook_url == "https://discord.com/api/webhooks/123456789012345678/test-webhook-token"
    assert settings.database_path == Path("/tmp/2busy1miss.sqlite3")
    assert settings.agenda_schedule_time == time(20, 30)


def test_default_runtime_paths_are_application_scoped(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_PATH", raising=False)

    settings = Settings()

    assert settings.google_calendar_credentials_path == isolated_home / ".config/2much2read-runtime/calendar-client-secret.json"
    assert settings.google_calendar_token_path == isolated_home / ".config/2much2read-runtime/2busy1miss/calendar-token.json"
    assert settings.reminders_config_path == isolated_home / ".config/2much2read-runtime/reminders.yaml"
    assert settings.database_path == isolated_home / ".local/share/2much2read-runtime/2busy1miss/2busy1miss.sqlite3"
    assert settings.lock_path == isolated_home / ".local/share/2much2read-runtime/2busy1miss/2busy1miss.lock"


def test_default_runtime_paths_keep_legacy_files_until_installer_migration(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_root = isolated_home / ".config/2much2read-runtime"
    data_root = isolated_home / ".local/share/2much2read-runtime"
    config_root.mkdir(parents=True)
    data_root.mkdir(parents=True)
    for path in (
        config_root / "calendar-token.json",
        data_root / "2busy1miss.sqlite3",
        data_root / "2busy1miss.lock",
    ):
        path.touch()
    for variable in ("GOOGLE_CALENDAR_TOKEN_PATH", "DATABASE_PATH", "LOCK_PATH"):
        monkeypatch.delenv(variable, raising=False)

    settings = Settings()

    assert settings.google_calendar_token_path == config_root / "calendar-token.json"
    assert settings.database_path == data_root / "2busy1miss.sqlite3"
    assert settings.lock_path == data_root / "2busy1miss.lock"


def test_agenda_schedule_time_requires_a_minute_precision_time() -> None:
    assert Settings(agenda_schedule_time="20:30").agenda_schedule_time == time(20, 30)

    with pytest.raises(ValueError, match="HH:MM"):
        Settings(agenda_schedule_time="20:30:01")
