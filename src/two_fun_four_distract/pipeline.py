from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from two_read_runtime.discord import DiscordDeliveryError, deliver, delivery_error_code
from two_read_runtime.locking import ProcessLock

from .config import Settings
from .report import build_report, render
from .storage import Database, snapshot


class SummaryPreview(BaseModel):
    status: str = "ok"
    day: date
    content: str


class SummaryResult(BaseModel):
    status: str = "ok"
    day: date
    sent: int = 0
    skipped: int = 0
    failed: int = 0
    discord_message_ids: list[str] = []
    failed_by_error_code: dict[str, int] = {}


def report_text(settings: Settings, day: date | None = None, *, now: datetime | None = None) -> tuple[date, str]:
    """A day's report, read without changing anything on disk. Today's when no day is named."""
    timezone = ZoneInfo(settings.summary_timezone)
    now = (now or datetime.now(timezone)).astimezone(timezone)
    day = day or now.date()
    with snapshot(settings.database_path) as database:
        return day, render(build_report(database, day, timezone, now))


def _fenced(text: str) -> str:
    return f"```text\n{text}\n```"


def preview_summary(settings: Settings, day: date | None = None, *, now: datetime | None = None) -> SummaryPreview:
    timezone = ZoneInfo(settings.summary_timezone)
    now = (now or datetime.now(timezone)).astimezone(timezone)
    day, text = report_text(settings, day or now.date() - timedelta(days=1), now=now)
    return SummaryPreview(day=day, content=_fenced(text))


def send_summary(
    settings: Settings, day: date | None = None, *, force: bool = False, now: datetime | None = None
) -> SummaryResult:
    """Yesterday's summary unless a day is named, to each destination that has not had it.

    Once per destination, so that a retry after a partial failure or a run caught up after a reboot
    repeats nothing; `force` is there for sending a day again on purpose.
    """
    timezone = ZoneInfo(settings.summary_timezone)
    now = (now or datetime.now(timezone)).astimezone(timezone)
    day = day or now.date() - timedelta(days=1)
    destinations = settings.discord_destinations()
    with ProcessLock(settings.lock_path):
        database = Database(settings.database_path)
        try:
            content = _fenced(render(build_report(database, day, timezone, now)))
            result = SummaryResult(day=day)
            for destination in destinations:
                if not force and database.summary_sent(day, destination.key):
                    result.skipped += 1
                    continue
                try:
                    message_ids = deliver(destination, content, settings.discord_username)
                except DiscordDeliveryError as error:
                    code = delivery_error_code(error)
                    result.failed += 1
                    result.failed_by_error_code[code] = result.failed_by_error_code.get(code, 0) + 1
                    continue
                database.record_summary(day, destination.key, message_ids, now)
                result.sent += 1
                result.discord_message_ids.extend(message_ids)
        finally:
            database.close()
    if result.failed:
        result.status = "partial" if result.sent or result.skipped else "failed"
    return result
