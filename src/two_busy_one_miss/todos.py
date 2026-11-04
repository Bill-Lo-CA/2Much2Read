"""Daily to-dos: what a calendar cannot express, such as thirty push-ups every morning.

The writes here take no ProcessLock. Each is one SQLite statement, which the database serialises
itself under its busy timeout, and none of them sends anything or has to agree with a delivery in
progress. The lock would only make a typed command fail whenever the per-minute timer was running.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from two_read_runtime.sqlite_snapshot import reading_connection

from .config import Settings, load_reminders
from .renderer import _agenda_cell
from .rules import parse_weekdays
from .storage import Database, todo_counts_on

MAX_TITLE_CHARACTERS = 200


class TodoView(BaseModel):
    id: int
    title: str
    weekdays: list[str]
    due: bool
    done: bool


class TodoListResult(BaseModel):
    status: Literal["ok"] = "ok"
    day: date
    todos: list[TodoView]


class TodoAddResult(BaseModel):
    status: Literal["ok"] = "ok"
    id: int
    title: str
    weekdays: list[str]


class TodoChangeResult(BaseModel):
    status: Literal["ok"] = "ok"
    id: int
    day: date
    # False when the to-do was already that way: done twice, undone when not done, removed again.
    changed: bool


def _today(settings: Settings, now: datetime | None) -> date:
    config = load_reminders(settings.reminders_config_path)
    timezone = ZoneInfo(config.timezone or settings.reminder_timezone)
    return (now or datetime.now(timezone)).astimezone(timezone).date()


def clean_title(title: str) -> str:
    cleaned = " ".join(title.split())
    # What the agenda would show of it, since a title of only a link or control characters shows nothing.
    if not _agenda_cell(cleaned):
        raise ValueError("a to-do needs a title with something to show")
    if len(cleaned) > MAX_TITLE_CHARACTERS:
        raise ValueError(f"a to-do's title must not exceed {MAX_TITLE_CHARACTERS} characters")
    return cleaned


def add_todo(settings: Settings, title: str, on: str | None, *, now: datetime | None = None) -> TodoAddResult:
    cleaned = clean_title(title)
    weekdays = parse_weekdays(on)
    first_day = _today(settings, now)
    database = Database(settings.database_path)
    try:
        todo_id = database.add_todo(cleaned, weekdays, first_day)
    finally:
        database.close()
    return TodoAddResult(id=todo_id, title=cleaned, weekdays=weekdays.split(","))


def list_todos(settings: Settings, day: date | None = None, *, now: datetime | None = None) -> TodoListResult:
    """The to-dos not removed, read without changing anything on disk, as `run --dry-run` reads."""
    day = day or _today(settings, now)
    with reading_connection(settings.database_path, ("todos", "todo_completions")) as connection:
        if connection is None:
            return TodoListResult(day=day, todos=[])
        rows = Database.reading(connection).todos(day)
        return TodoListResult(
            day=day,
            todos=[
                TodoView(
                    id=int(row["id"]),
                    title=str(row["title"]),
                    weekdays=str(row["weekdays"]).split(","),
                    due=todo_counts_on(row, day),
                    done=bool(row["done"]),
                )
                for row in rows
                if row["removed_on"] is None
            ],
        )


def complete_todo(settings: Settings, todo_id: int, day: date | None = None, *, now: datetime | None = None) -> TodoChangeResult:
    today = _today(settings, now)
    day = day or today
    if day > today:
        raise ValueError(f"{day.isoformat()} has not come yet")
    database = Database(settings.database_path)
    try:
        row = database.todo(todo_id)
        if row is None:
            raise ValueError(f"no to-do with id {todo_id}")
        if not todo_counts_on(row, day):
            raise ValueError(f"to-do {todo_id} is not due on {day.isoformat()}; it falls on {row['weekdays']}")
        return TodoChangeResult(id=todo_id, day=day, changed=database.complete_todo(todo_id, day))
    finally:
        database.close()


def reopen_todo(settings: Settings, todo_id: int, day: date | None = None, *, now: datetime | None = None) -> TodoChangeResult:
    day = day or _today(settings, now)
    database = Database(settings.database_path)
    try:
        if database.todo(todo_id) is None:
            raise ValueError(f"no to-do with id {todo_id}")
        return TodoChangeResult(id=todo_id, day=day, changed=database.reopen_todo(todo_id, day))
    finally:
        database.close()


def remove_todo(settings: Settings, todo_id: int, *, now: datetime | None = None) -> TodoChangeResult:
    day = _today(settings, now)
    database = Database(settings.database_path)
    try:
        if database.todo(todo_id) is None:
            raise ValueError(f"no to-do with id {todo_id}")
        return TodoChangeResult(id=todo_id, day=day, changed=database.remove_todo(todo_id, day))
    finally:
        database.close()
