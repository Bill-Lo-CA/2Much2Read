import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import TypeVar

import pytest

from two_busy_one_miss.config import Settings as CalendarSettings
from two_busy_one_miss.storage import Database as CalendarDatabase
from two_much_two_read.config import Settings as NewsletterSettings
from two_much_two_read.storage import Database as NewsletterDatabase

T = TypeVar("T")


def recorded(log: list[str], entry: str, result: T) -> T:
    """Note that a stub ran, then give back its canned result.

    Stands in for `lambda *args: log.append(entry) or result`, which works only because append
    returns None - and using a None return is exactly what mypy's func-returns-value check exists
    to catch, since elsewhere it is a bug.
    """
    log.append(entry)
    return result


def directory_digest(path: Path) -> dict[str, tuple[int, str]]:
    """Every file in a directory, by content.

    Assertions that a command changed nothing have to read the bytes. A read-only SQLite connection
    updates the reader marks inside a live -shm file in place, leaving its name, its size, and even
    its mtime alone, so a listing built from any of those reports no change where there was one.
    """
    return {
        entry.name: (entry.stat().st_size, hashlib.sha256(entry.read_bytes()).hexdigest())
        for entry in path.iterdir()
        if entry.is_file()
    }


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
        discord_webhook_url="https://discord.com/api/webhooks/123456789012345678/test-webhook-token",
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
        discord_webhook_url="https://discord.com/api/webhooks/123456789012345678/test-webhook-token",
    )


@pytest.fixture
def calendar_database(calendar_settings: CalendarSettings) -> Iterator[CalendarDatabase]:
    database = CalendarDatabase(calendar_settings.database_path)
    try:
        yield database
    finally:
        database.close()


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home
