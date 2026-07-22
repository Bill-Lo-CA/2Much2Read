from collections.abc import Iterator
from pathlib import Path

import pytest

from two_busy_one_miss.config import Settings as CalendarSettings
from two_busy_one_miss.storage import Database as CalendarDatabase
from two_much_two_read.config import Settings as NewsletterSettings
from two_much_two_read.storage import Database as NewsletterDatabase


@pytest.fixture
def newsletter_sources_path(tmp_path: Path) -> Path:
    path = tmp_path / "sources.yaml"
    path.write_text("sources: []\n", encoding="utf-8")
    return path


@pytest.fixture
def newsletter_settings(tmp_path: Path, newsletter_sources_path: Path) -> NewsletterSettings:
    return NewsletterSettings(
        sources_config_path=newsletter_sources_path,
        database_path=tmp_path / "digest.sqlite3",
        lock_path=tmp_path / "digest.lock",
    )


@pytest.fixture
def newsletter_database(newsletter_settings: NewsletterSettings) -> Iterator[NewsletterDatabase]:
    database = NewsletterDatabase(newsletter_settings.database_path)
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def calendar_settings(tmp_path: Path) -> CalendarSettings:
    reminders_path = tmp_path / "reminders.yaml"
    reminders_path.write_text("calendars:\n  - id: primary\n", encoding="utf-8")
    return CalendarSettings(
        reminders_config_path=reminders_path,
        database_path=tmp_path / "reminders.sqlite3",
        lock_path=tmp_path / "reminders.lock",
    )


@pytest.fixture
def calendar_database(calendar_settings: CalendarSettings) -> Iterator[CalendarDatabase]:
    database = CalendarDatabase(calendar_settings.database_path)
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home
