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


def test_settings_ignore_repo_dotenv_and_use_private_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    app_config = home / ".config" / "2much2read-runtime"
    app_config.mkdir(parents=True)
    (tmp_path / ".env").write_text(
        "DISCORD_WEBHOOK_URL=https://newsletter.example/webhook\nDATABASE_PATH=newsletter.sqlite3\n",
        encoding="utf-8",
    )
    (app_config / ".2busy1miss.env").write_text(
        "DISCORD_WEBHOOK_URL=https://busy.example/webhook\nDATABASE_PATH=/tmp/2busy1miss.sqlite3\nAGENDA_SCHEDULE_TIME=20:30\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("DATABASE_PATH", raising=False)

    settings = Settings()

    assert settings.discord_webhook_url == "https://busy.example/webhook"
    assert settings.database_path == Path("/tmp/2busy1miss.sqlite3")
    assert settings.agenda_schedule_time == time(20, 30)


def test_agenda_schedule_time_requires_a_minute_precision_time() -> None:
    assert Settings(agenda_schedule_time="20:30").agenda_schedule_time == time(20, 30)

    with pytest.raises(ValueError, match="HH:MM"):
        Settings(agenda_schedule_time="20:30:01")
