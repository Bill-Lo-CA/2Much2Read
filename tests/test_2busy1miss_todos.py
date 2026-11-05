from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from conftest import directory_digest
from typer.testing import CliRunner

from two_busy_one_miss import cli, pipeline, todos
from two_busy_one_miss.config import Settings
from two_busy_one_miss.google_calendar import CalendarEvent
from two_busy_one_miss.renderer import RULE, render_agenda
from two_busy_one_miss.rules import falls_on, parse_period, parse_weekdays, period_start
from two_busy_one_miss.storage import Database
from two_read_runtime.locking import ProcessLock

TORONTO = ZoneInfo("America/Toronto")
MONDAY = date(2026, 11, 2)


def noon(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 12, tzinfo=TORONTO)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    reminders_path = tmp_path / "reminders.yaml"
    reminders_path.write_text("timezone: America/Toronto\ncalendars:\n  - id: primary\n", encoding="utf-8")
    return Settings(
        reminders_config_path=reminders_path,
        database_path=tmp_path / "reminders.sqlite3",
        lock_path=tmp_path / "reminders.lock",
        discord_webhook_url="https://discord.com/api/webhooks/123456789012345678/test-webhook-token",
    )


def rows(settings: Settings, query: str) -> list[tuple[object, ...]]:
    with closing(sqlite3.connect(settings.database_path)) as connection:
        return connection.execute(query).fetchall()


def agenda_todos(settings: Settings, day: date) -> tuple[list[str], list[str]]:
    database = Database(settings.database_path)
    try:
        return database.agenda_todos(day)
    finally:
        database.close()


def test_weekdays_are_kept_in_calendar_order_and_default_to_every_day() -> None:
    assert parse_weekdays(None) == "mon,tue,wed,thu,fri,sat,sun"
    assert parse_weekdays("fri, MON,fri") == "mon,fri"


@pytest.mark.parametrize("value", ["", "mon,", "monday", "mon,funday"])
def test_a_weekday_nothing_could_fall_on_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match="unknown weekday"):
        parse_weekdays(value)


def test_a_day_falls_on_its_own_weekday_only() -> None:
    assert falls_on("mon,wed", MONDAY)
    assert not falls_on("mon,wed", MONDAY + timedelta(days=1))
    assert falls_on("mon,wed", MONDAY + timedelta(days=2))
    assert falls_on("sun", MONDAY - timedelta(days=1))


def test_a_new_todo_is_cleaned_and_listed(settings: Settings) -> None:
    added = todos.add_todo(settings, "  三十下   伏地挺身 ", "wed,mon", now=noon(MONDAY))

    assert (added.title, added.weekdays) == ("三十下 伏地挺身", ["mon", "wed"])
    assert todos.list_todos(settings, now=noon(MONDAY)).model_dump(mode="json") == {
        "status": "ok",
        "day": "2026-11-02",
        "todos": [
            {"id": added.id, "title": "三十下 伏地挺身", "weekdays": ["mon", "wed"], "period": "day", "due": True, "done": False}
        ],
    }


@pytest.mark.parametrize("title", ["", "   ", "https://example.com/only-a-link", "‮‮"])
def test_a_title_the_agenda_would_show_nothing_of_is_refused(settings: Settings, title: str) -> None:
    with pytest.raises(ValueError, match="something to show"):
        todos.add_todo(settings, title, None, now=noon(MONDAY))


def test_a_title_past_the_limit_is_refused(settings: Settings) -> None:
    assert todos.add_todo(settings, "x" * todos.MAX_TITLE_CHARACTERS, None, now=noon(MONDAY)).id == 1
    with pytest.raises(ValueError, match="must not exceed"):
        todos.add_todo(settings, "x" * (todos.MAX_TITLE_CHARACTERS + 1), None, now=noon(MONDAY))


def test_a_todo_not_due_today_is_still_listed_as_not_due(settings: Settings) -> None:
    added = todos.add_todo(settings, "跑步", "tue", now=noon(MONDAY))

    listed = todos.list_todos(settings, now=noon(MONDAY)).todos

    assert [(view.id, view.due, view.done) for view in listed] == [(added.id, False, False)]


def test_done_and_undo_mark_one_day_and_say_whether_anything_changed(settings: Settings) -> None:
    added = todos.add_todo(settings, "跑步", None, now=noon(MONDAY))
    tuesday = MONDAY + timedelta(days=1)

    assert todos.complete_todo(settings, added.id, now=noon(MONDAY)).changed
    assert not todos.complete_todo(settings, added.id, now=noon(MONDAY)).changed
    assert todos.list_todos(settings, MONDAY, now=noon(tuesday)).todos[0].done
    assert not todos.list_todos(settings, tuesday, now=noon(tuesday)).todos[0].done

    assert todos.reopen_todo(settings, added.id, MONDAY, now=noon(tuesday)).changed
    assert not todos.reopen_todo(settings, added.id, MONDAY, now=noon(tuesday)).changed
    assert not todos.list_todos(settings, MONDAY, now=noon(tuesday)).todos[0].done


def test_done_is_refused_for_a_day_it_could_not_have_been_done(settings: Settings) -> None:
    added = todos.add_todo(settings, "跑步", "mon", now=noon(MONDAY))
    next_week = MONDAY + timedelta(days=7)

    with pytest.raises(ValueError, match="has not come yet"):
        todos.complete_todo(settings, added.id, next_week, now=noon(MONDAY))
    with pytest.raises(ValueError, match="is not due on 2026-11-03; it falls on mon"):
        todos.complete_todo(settings, added.id, MONDAY + timedelta(days=1), now=noon(next_week))
    with pytest.raises(ValueError, match="is not due on 2026-10-26"):
        todos.complete_todo(settings, added.id, MONDAY - timedelta(days=7), now=noon(next_week))


@pytest.mark.parametrize("operation", [todos.complete_todo, todos.reopen_todo, todos.remove_todo])
def test_an_id_that_was_never_added_is_refused(settings: Settings, operation: Callable[..., todos.TodoChangeResult]) -> None:
    with pytest.raises(ValueError, match="no to-do with id 9"):
        operation(settings, 9, now=noon(MONDAY))


def test_a_removed_todo_stops_counting_from_that_day_and_keeps_its_record(settings: Settings) -> None:
    added = todos.add_todo(settings, "跑步", None, now=noon(MONDAY))
    tuesday = MONDAY + timedelta(days=1)
    todos.complete_todo(settings, added.id, now=noon(MONDAY))

    assert todos.remove_todo(settings, added.id, now=noon(tuesday)).changed
    assert not todos.remove_todo(settings, added.id, now=noon(tuesday)).changed

    assert todos.list_todos(settings, now=noon(tuesday)).todos == []
    assert agenda_todos(settings, tuesday) == ([], [])
    with pytest.raises(ValueError, match="is not due on 2026-11-03"):
        todos.complete_todo(settings, added.id, now=noon(tuesday))
    # The day before it was removed it still counted, so it can still be put right.
    assert not todos.complete_todo(settings, added.id, MONDAY, now=noon(tuesday)).changed
    assert todos.reopen_todo(settings, added.id, MONDAY, now=noon(tuesday)).changed
    assert rows(settings, "SELECT title FROM todos") == [("跑步",)]


def test_today_is_the_configured_zones_day(settings: Settings) -> None:
    # 03:30 UTC on the 3rd is still the evening of the 2nd in Toronto, and past noon on the 3rd in Tokyo.
    instant = datetime(2026, 11, 3, 3, 30, tzinfo=UTC)
    assert todos.list_todos(settings, now=instant).day == MONDAY
    assert todos.add_todo(settings, "跑步", None, now=instant).id == 1
    assert agenda_todos(settings, MONDAY) == (["跑步"], [])

    settings.reminders_config_path.write_text("timezone: Asia/Tokyo\ncalendars:\n  - id: primary\n", encoding="utf-8")
    assert todos.list_todos(settings, now=instant).day == MONDAY + timedelta(days=1)


def test_an_agenda_lists_its_days_open_todos_and_only_yesterdays_undone(settings: Settings) -> None:
    daily = todos.add_todo(settings, "伏地挺身", None, now=noon(MONDAY)).id
    done_yesterday = todos.add_todo(settings, "讀書", None, now=noon(MONDAY)).id
    todos.add_todo(settings, "週一才做", "mon", now=noon(MONDAY))
    todos.add_todo(settings, "週三才做", "wed", now=noon(MONDAY))
    tuesday, wednesday = MONDAY + timedelta(days=1), MONDAY + timedelta(days=2)
    todos.complete_todo(settings, done_yesterday, tuesday, now=noon(tuesday))

    # Monday's undone are two days back from Wednesday's agenda, so only Tuesday's are named.
    assert agenda_todos(settings, wednesday) == (["伏地挺身", "讀書", "週三才做"], ["伏地挺身"])
    # A to-do done on the agenda's own day is not left to do.
    todos.complete_todo(settings, daily, wednesday, now=noon(wednesday))
    assert agenda_todos(settings, wednesday)[0] == ["讀書", "週三才做"]


def test_a_todo_owes_nothing_from_before_it_was_added(settings: Settings) -> None:
    todos.add_todo(settings, "伏地挺身", None, now=noon(MONDAY))

    assert agenda_todos(settings, MONDAY) == (["伏地挺身"], [])
    assert agenda_todos(settings, MONDAY - timedelta(days=1)) == ([], [])


def test_writes_work_while_the_runtime_lock_is_held(settings: Settings) -> None:
    with ProcessLock(settings.lock_path):
        added = todos.add_todo(settings, "跑步", None, now=noon(MONDAY))
        assert todos.complete_todo(settings, added.id, now=noon(MONDAY)).changed
        assert todos.reopen_todo(settings, added.id, now=noon(MONDAY)).changed
        assert todos.remove_todo(settings, added.id, now=noon(MONDAY)).changed


def test_listing_changes_nothing_on_disk(settings: Settings, tmp_path: Path) -> None:
    todos.add_todo(settings, "跑步", None, now=noon(MONDAY))
    before = directory_digest(tmp_path)

    assert len(todos.list_todos(settings, now=noon(MONDAY)).todos) == 1

    assert directory_digest(tmp_path) == before


def test_listing_a_database_from_before_todos_finds_none_and_creates_nothing(settings: Settings, tmp_path: Path) -> None:
    with closing(sqlite3.connect(settings.database_path)) as connection:
        connection.execute("CREATE TABLE events(id INTEGER PRIMARY KEY)")
        connection.commit()
    before = directory_digest(tmp_path)

    assert todos.list_todos(settings, now=noon(MONDAY)).todos == []

    assert directory_digest(tmp_path) == before


def test_opening_a_database_from_before_todos_adds_their_tables_and_keeps_its_rows(settings: Settings) -> None:
    Database(settings.database_path).close()
    with closing(sqlite3.connect(settings.database_path)) as connection:
        connection.executescript("DROP TABLE todo_completions; DROP TABLE todos;")
        connection.execute(
            """INSERT INTO agenda_deliveries(agenda_day,timezone,destination_hash,content,state,created_at,updated_at)
            VALUES('2026-11-02','America/Montreal','hash','old agenda','delivered','now','now')"""
        )
        connection.commit()

    assert todos.add_todo(settings, "跑步", None, now=noon(MONDAY)).id == 1

    assert rows(settings, "SELECT content FROM agenda_deliveries") == [("old agenda",)]


def test_the_agenda_shows_todos_and_yesterdays_undone_under_their_own_rules() -> None:
    rendered = render_agenda(date(2026, 11, 5), [], TORONTO, ["三十下伏地挺身", "讀書"], ["跑步", "伸展"])

    assert rendered.split("\n") == [
        "```text",
        "2busy1miss agenda · 2026-11-05",
        "TIME        | EVENT",
        RULE,
        "            | No events",
        RULE,
        "To do       | 三十下伏地挺身",
        "            | 讀書",
        RULE,
        "Undone      | 11-04: 跑步",
        "            | 11-04: 伸展",
        "```",
    ]


def test_todos_follow_the_events_and_an_events_links_stay_after_the_table() -> None:
    start = datetime(2026, 11, 5, 7, 30, tzinfo=TORONTO)
    event = CalendarEvent(
        calendar_id="primary",
        calendar_name="Main",
        event_id="event-1",
        instance_id="event-1",
        title="起床時間",
        location="https://maps.app.goo.gl/example",
        start=start,
        end=start + timedelta(minutes=30),
        all_day=False,
    )

    rendered = render_agenda(date(2026, 11, 5), [event], TORONTO, ["讀書"], [])

    assert rendered.split("\n")[4:] == [
        "07:30-08:00 | 起床時間 (Main)",
        RULE,
        "To do       | 讀書",
        "```",
        "Location link - maps.app.goo.gl: <https://maps.app.goo.gl/example>",
    ]


def test_each_todo_section_appears_only_when_it_has_something() -> None:
    day = date(2026, 11, 5)
    assert "Undone" not in render_agenda(day, [], TORONTO, ["讀書"], [])
    only_undone = render_agenda(day, [], TORONTO, [], ["跑步"])
    assert "To do" not in only_undone
    assert "Undone      | 11-04: 跑步" in only_undone


def test_an_agenda_without_todos_is_what_it_was() -> None:
    day = date(2026, 11, 5)
    assert render_agenda(day, [], TORONTO, [], []) == render_agenda(day, [], TORONTO)
    assert RULE not in render_agenda(day, [], TORONTO).split("\n")[4:]


def test_a_todos_title_cannot_break_out_of_the_agenda() -> None:
    rendered = render_agenda(date(2026, 11, 5), [], TORONTO, ["@everyone ```run``` https://example.com/x"], ["`@here`\nforged"])

    assert "@everyone" not in rendered
    assert "@here" not in rendered
    assert rendered.count("```") == 2
    assert rendered.endswith("\n```")
    assert "example.com" not in rendered
    assert "\nforged" not in rendered


def test_the_next_day_agenda_carries_the_todos_and_a_dry_run_changes_nothing(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []

    def deliver(*args: object, **kwargs: object) -> list[str]:
        sent.append(str(args[1]))
        return ["discord-id"]

    monkeypatch.setattr(pipeline, "list_events_between", lambda *args: [])
    monkeypatch.setattr(pipeline, "deliver", deliver)
    todos.add_todo(settings, "伏地挺身", None, now=noon(MONDAY))
    evening = datetime(2026, 11, 2, 21, tzinfo=TORONTO)
    tuesday = date(2026, 11, 3)
    expected = render_agenda(tuesday, [], TORONTO, ["伏地挺身"], ["伏地挺身"])

    before = directory_digest(tmp_path)
    preview = pipeline.next_day_agenda(settings, dry_run=True, force=False, now=evening)
    assert isinstance(preview, pipeline.AgendaPreviewResult)
    assert preview.content == expected
    assert directory_digest(tmp_path) == before

    assert pipeline.next_day_agenda(settings, dry_run=False, force=False, now=evening).sent == 1
    assert sent == [expected]
    # Done after it was sent: the stored agenda is the one that went out, and is not sent again.
    todos.complete_todo(settings, 1, MONDAY, now=evening)
    assert pipeline.next_day_agenda(settings, dry_run=False, force=False, now=evening).skipped == 1
    assert sent == [expected]


def test_a_manual_agenda_carries_the_todos_of_the_day_it_names(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []

    def deliver(*args: object, **kwargs: object) -> list[str]:
        sent.append(str(args[1]))
        return ["discord-id"]

    monkeypatch.setattr(pipeline, "list_events_between", lambda *args: [])
    monkeypatch.setattr(pipeline, "deliver", deliver)
    todos.add_todo(settings, "週三才做", "wed", now=noon(MONDAY))
    wednesday = MONDAY + timedelta(days=2)
    expected = render_agenda(wednesday, [], TORONTO, ["週三才做"], [])

    preview = pipeline.agenda(settings, wednesday, dry_run=True)
    assert isinstance(preview, pipeline.AgendaPreviewResult)
    assert preview.content == expected
    assert pipeline.agenda(settings, wednesday, dry_run=False).sent == 1
    assert sent == [expected]


def test_a_dry_run_agenda_before_any_database_exists_has_no_todos(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline, "list_events_between", lambda *args: [])

    preview = pipeline.agenda(settings, MONDAY, dry_run=True)

    assert isinstance(preview, pipeline.AgendaPreviewResult)
    assert preview.content == render_agenda(MONDAY, [], TORONTO)
    assert not settings.database_path.exists()


def test_the_commands_write_one_json_result(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(todos, "_today", lambda *args: MONDAY)
    runner = CliRunner()

    added = runner.invoke(cli.app, ["todo", "add", "三十下伏地挺身", "--on", "mon,wed,fri"])
    assert added.exit_code == 0, added.output
    assert json.loads(added.stdout) == {
        "status": "ok",
        "id": 1,
        "title": "三十下伏地挺身",
        "weekdays": ["mon", "wed", "fri"],
        "period": "day",
    }

    done = runner.invoke(cli.app, ["todo", "done", "1", "--day", "2026-11-02"])
    assert done.exit_code == 0, done.output
    assert json.loads(done.stdout) == {"status": "ok", "id": 1, "day": "2026-11-02", "changed": True}

    listed = runner.invoke(cli.app, ["todo", "list", "--day", "2026-11-02"])
    assert listed.exit_code == 0, listed.output
    assert json.loads(listed.stdout)["todos"] == [
        {"id": 1, "title": "三十下伏地挺身", "weekdays": ["mon", "wed", "fri"], "period": "day", "due": True, "done": True}
    ]

    undone = runner.invoke(cli.app, ["todo", "undo", "1", "--day", "2026-11-02"])
    assert undone.exit_code == 0, undone.output
    assert json.loads(undone.stdout)["changed"] is True

    removed = runner.invoke(cli.app, ["todo", "remove", "1"])
    assert removed.exit_code == 0, removed.output
    assert json.loads(removed.stdout)["changed"] is True


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["todo", "add", "跑步", "--on", "funday"], "unknown weekday"),
        (["todo", "add", "   "], "something to show"),
        (["todo", "done", "9"], "no to-do with id 9"),
        (["todo", "undo", "9"], "no to-do with id 9"),
        (["todo", "remove", "9"], "no to-do with id 9"),
        (["todo", "done", "1", "--day", "tomorrow"], "date must use YYYY-MM-DD"),
        (["todo", "undo", "1", "--day", "tomorrow"], "date must use YYYY-MM-DD"),
        (["todo", "list", "--day", "tomorrow"], "date must use YYYY-MM-DD"),
    ],
)
def test_a_refused_command_ends_on_its_message(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, arguments: list[str], message: str
) -> None:
    monkeypatch.setattr(cli, "Settings", lambda: settings)

    result = CliRunner().invoke(cli.app, arguments)

    assert result.exit_code == 2
    assert message in " ".join(result.output.replace("│", " ").split())


TODO_PAGE = "http://100.89.225.36:8766/"


def test_an_agenda_listing_to_dos_links_to_the_page() -> None:
    content = render_agenda(MONDAY, [], TORONTO, ["伏地挺身"], [], todo_page=TODO_PAGE)

    assert content.splitlines()[-2:] == ["```", f"To-do page: <{TODO_PAGE}>"]


def test_an_agenda_listing_only_undone_to_dos_links_to_the_page() -> None:
    assert f"To-do page: <{TODO_PAGE}>" in render_agenda(MONDAY, [], TORONTO, [], ["跑步"], todo_page=TODO_PAGE)


def test_an_agenda_with_no_to_dos_has_no_link_to_the_page() -> None:
    assert render_agenda(MONDAY, [], TORONTO, todo_page=TODO_PAGE) == render_agenda(MONDAY, [], TORONTO)


@pytest.mark.parametrize("url", ["", "javascript:alert(1)", "http://user:password@100.89.225.36:8766/", "100.89.225.36:8766"])
def test_a_page_address_that_is_not_a_plain_link_is_left_out(url: str) -> None:
    assert "To-do page" not in render_agenda(MONDAY, [], TORONTO, ["伏地挺身"], [], todo_page=url)


def test_the_cap_on_links_cannot_push_out_the_page() -> None:
    events = [
        CalendarEvent(
            calendar_id="primary",
            calendar_name=None,
            event_id=f"event-{index}",
            instance_id=f"event-{index}",
            title="Meeting",
            location=f"https://example.com/{index}",
            start=noon(MONDAY),
            end=noon(MONDAY) + timedelta(hours=1),
            all_day=False,
        )
        for index in range(30)
    ]

    lines = render_agenda(MONDAY, events, TORONTO, ["伏地挺身"], [], todo_page=TODO_PAGE).splitlines()

    assert lines[lines.index("```", 1) + 1] == f"To-do page: <{TODO_PAGE}>"
    assert sum("link - " in line for line in lines) == 19


def test_both_agenda_previews_link_to_the_page(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline, "list_events_between", lambda *_: [])
    settings = settings.model_copy(update={"todo_web_url": TODO_PAGE})
    todos.add_todo(settings, "伏地挺身", None, now=noon(MONDAY))

    assert TODO_PAGE in pipeline.agenda(settings, MONDAY, True).content
    preview = pipeline.next_day_agenda(settings, dry_run=True, force=False, now=noon(MONDAY) - timedelta(days=1))
    assert isinstance(preview, pipeline.AgendaPreviewResult)
    assert TODO_PAGE in preview.content


def test_an_id_no_to_do_could_have_is_refused_like_any_unknown_one(settings: Settings) -> None:
    for change in (todos.complete_todo, todos.reopen_todo):
        with pytest.raises(ValueError, match="no to-do with id"):
            change(settings, 2**63, MONDAY, now=noon(MONDAY))


def test_add_outside_a_terminal_needs_a_title(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "Settings", lambda: settings)

    result = CliRunner().invoke(cli.app, ["todo", "add"])

    assert result.exit_code == 2
    assert "title is needed" in result.output
    assert rows_or_empty(settings) == []


def test_todo_on_its_own_outside_a_terminal_says_so_and_adds_nothing(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "Settings", lambda: settings)

    result = CliRunner().invoke(cli.app, ["todo"])

    assert result.exit_code == 2
    assert "title is needed" in result.output
    assert rows_or_empty(settings) == []


def test_todo_still_lists_its_commands_in_its_help() -> None:
    result = CliRunner().invoke(cli.app, ["todo", "--help"])

    assert result.exit_code == 0
    for command in ("add", "list", "done", "undo", "remove", "serve"):
        assert command in result.output


def rows_or_empty(settings: Settings) -> list[tuple[object, ...]]:
    return rows(settings, "SELECT * FROM todos") if settings.database_path.exists() else []


@pytest.mark.parametrize("host", ["0.0.0.0", "127.0.0.1", "192.168.30.180", "::1", "example.com"])
def test_serve_refuses_any_address_outside_the_tailnet(settings: Settings, monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    monkeypatch.setattr(cli, "Settings", lambda: settings)

    result = CliRunner().invoke(cli.app, ["todo", "serve", "--host", host, "--port", "8766"])

    assert result.exit_code == 2


def test_serve_reports_an_address_it_cannot_listen_on_and_fails(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "Settings", lambda: settings)

    # An address inside the tailnet's range that no interface on the test machine holds.
    result = CliRunner().invoke(cli.app, ["todo", "serve", "--host", "100.127.255.254", "--port", "8766"])

    assert result.exit_code == 1
    assert "cannot listen on 100.127.255.254:8766" in result.output


@pytest.mark.parametrize(
    ("period", "day", "start"),
    [
        ("day", date(2026, 11, 4), date(2026, 11, 4)),
        ("week", date(2026, 11, 2), date(2026, 11, 2)),
        ("week", date(2026, 11, 4), date(2026, 11, 2)),
        ("week", date(2026, 11, 8), date(2026, 11, 2)),
        ("week", date(2026, 11, 9), date(2026, 11, 9)),
        ("week", date(2027, 1, 1), date(2026, 12, 28)),
        ("month", date(2026, 11, 1), date(2026, 11, 1)),
        ("month", date(2026, 11, 30), date(2026, 11, 1)),
        ("month", date(2028, 2, 29), date(2028, 2, 1)),
    ],
)
def test_period_start(period: str, day: date, start: date) -> None:
    assert period_start(period, day) == start


@pytest.mark.parametrize("value", ["", "year", "WEEK", "weekly"])
def test_a_period_there_is_no_such_thing_as_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match="unknown period"):
        parse_period(value)


def weekly(settings: Settings, first_day: date = MONDAY, title: str = "寫週報") -> int:
    return todos.add_todo(settings, title, None, period="week", now=noon(first_day)).id


def monthly(settings: Settings, first_day: date = MONDAY, title: str = "繳房租") -> int:
    return todos.add_todo(settings, title, None, period="month", now=noon(first_day)).id


def views(settings: Settings, day: date) -> list[tuple[str, bool, bool]]:
    return [(view.title, view.due, view.done) for view in todos.list_todos(settings, day).todos]


def test_a_daily_to_do_is_the_default_period(settings: Settings) -> None:
    added = todos.add_todo(settings, "喝水", None, now=noon(MONDAY))

    assert added.period == "day"
    assert todos.list_todos(settings, MONDAY).todos[0].period == "day"


def test_a_weekly_to_do_falls_on_every_day_and_says_what_it_is(settings: Settings) -> None:
    added = todos.add_todo(settings, "寫週報", None, period="week", now=noon(MONDAY))

    assert added.period == "week"
    assert added.weekdays == ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    assert todos.list_todos(settings, MONDAY).todos[0].period == "week"


@pytest.mark.parametrize("period", ["week", "month"])
def test_days_cannot_be_named_for_a_weekly_or_monthly_to_do(settings: Settings, period: str) -> None:
    with pytest.raises(ValueError, match="--on is for daily to-dos"):
        todos.add_todo(settings, "寫週報", "mon", period=period, now=noon(MONDAY))


def test_days_can_be_named_for_a_daily_to_do_when_said_outright(settings: Settings) -> None:
    assert todos.add_todo(settings, "跑步", "mon", period="day", now=noon(MONDAY)).weekdays == ["mon"]


def test_a_period_there_is_no_such_thing_as_adds_nothing(settings: Settings) -> None:
    with pytest.raises(ValueError, match="unknown period"):
        todos.add_todo(settings, "寫週報", None, period="year", now=noon(MONDAY))

    assert not settings.database_path.exists()


def test_a_weekly_to_do_is_done_for_the_week_by_any_one_day(settings: Settings) -> None:
    todo_id = weekly(settings)
    wednesday = MONDAY + timedelta(days=2)

    assert todos.complete_todo(settings, todo_id, wednesday, now=noon(wednesday)).changed

    for offset in range(7):
        assert views(settings, MONDAY + timedelta(days=offset)) == [("寫週報", True, True)]


def test_a_weekly_to_do_starts_over_on_monday(settings: Settings) -> None:
    todo_id = weekly(settings)
    todos.complete_todo(settings, todo_id, MONDAY, now=noon(MONDAY))

    assert views(settings, MONDAY + timedelta(days=6)) == [("寫週報", True, True)]
    assert views(settings, MONDAY + timedelta(days=7)) == [("寫週報", True, False)]


def test_a_monthly_to_do_is_done_for_the_month_and_starts_over_on_the_first(settings: Settings) -> None:
    todo_id = monthly(settings)
    late = date(2026, 11, 20)
    todos.complete_todo(settings, todo_id, late, now=noon(late))

    assert views(settings, date(2026, 11, 2)) == [("繳房租", True, True)]
    assert views(settings, date(2026, 11, 30)) == [("繳房租", True, True)]
    assert views(settings, date(2026, 12, 1)) == [("繳房租", True, False)]


def test_doing_a_weekly_to_do_twice_in_a_week_changes_nothing_the_second_time(settings: Settings) -> None:
    todo_id = weekly(settings)
    tuesday = MONDAY + timedelta(days=1)

    assert todos.complete_todo(settings, todo_id, MONDAY, now=noon(tuesday)).changed
    assert not todos.complete_todo(settings, todo_id, tuesday, now=noon(tuesday)).changed
    assert rows(settings, "SELECT day FROM todo_completions") == [("2026-11-02",)]


def test_a_weekly_to_do_can_be_undone_from_any_day_of_the_week(settings: Settings) -> None:
    todo_id = weekly(settings)
    friday = MONDAY + timedelta(days=4)
    todos.complete_todo(settings, todo_id, MONDAY, now=noon(friday))

    assert todos.reopen_todo(settings, todo_id, friday, now=noon(friday)).changed
    assert views(settings, MONDAY) == [("寫週報", True, False)]


def test_undoing_a_weekly_to_do_leaves_other_weeks_alone(settings: Settings) -> None:
    todo_id = weekly(settings)
    next_week = MONDAY + timedelta(days=7)
    todos.complete_todo(settings, todo_id, MONDAY, now=noon(next_week))
    todos.complete_todo(settings, todo_id, next_week, now=noon(next_week))

    todos.reopen_todo(settings, todo_id, next_week, now=noon(next_week))

    assert views(settings, MONDAY) == [("寫週報", True, True)]
    assert views(settings, next_week) == [("寫週報", True, False)]


def test_a_weekly_to_do_is_not_owed_before_it_was_added(settings: Settings) -> None:
    wednesday = MONDAY + timedelta(days=2)
    todo_id = weekly(settings, wednesday)

    assert views(settings, MONDAY) == [("寫週報", False, False)]
    assert views(settings, wednesday) == [("寫週報", True, False)]
    with pytest.raises(ValueError, match="is not due"):
        todos.complete_todo(settings, todo_id, MONDAY, now=noon(wednesday))


def test_a_weekly_to_do_is_listed_with_its_period_until_it_is_done(settings: Settings) -> None:
    todo_id = weekly(settings)

    assert agenda_todos(settings, MONDAY)[0] == ["寫週報 (week)"]
    assert agenda_todos(settings, MONDAY + timedelta(days=3))[0] == ["寫週報 (week)"]

    tuesday = MONDAY + timedelta(days=1)
    todos.complete_todo(settings, todo_id, tuesday, now=noon(tuesday))

    assert agenda_todos(settings, MONDAY)[0] == []
    assert agenda_todos(settings, MONDAY + timedelta(days=6))[0] == []
    assert agenda_todos(settings, MONDAY + timedelta(days=7))[0] == ["寫週報 (week)"]


def test_a_monthly_to_do_is_listed_with_its_period(settings: Settings) -> None:
    monthly(settings)

    assert agenda_todos(settings, MONDAY)[0] == ["繳房租 (month)"]


def test_a_daily_to_do_has_no_period_in_the_agenda(settings: Settings) -> None:
    todos.add_todo(settings, "喝水", None, now=noon(MONDAY))

    assert agenda_todos(settings, MONDAY)[0] == ["喝水"]


def test_a_weekly_to_do_is_undone_only_the_day_after_its_week_ran_out(settings: Settings) -> None:
    weekly(settings)

    for offset in range(1, 7):
        assert agenda_todos(settings, MONDAY + timedelta(days=offset))[1] == []
    assert agenda_todos(settings, MONDAY + timedelta(days=7)) == (["寫週報 (week)"], ["寫週報 (week)"])
    assert agenda_todos(settings, MONDAY + timedelta(days=8))[1] == []


def test_a_monthly_to_do_is_undone_only_the_day_after_its_month_ran_out(settings: Settings) -> None:
    monthly(settings)

    assert agenda_todos(settings, date(2026, 11, 30))[1] == []
    assert agenda_todos(settings, date(2026, 12, 1))[1] == ["繳房租 (month)"]
    assert agenda_todos(settings, date(2026, 12, 2))[1] == []


def test_a_weekly_to_do_done_in_its_week_is_not_undone(settings: Settings) -> None:
    todo_id = weekly(settings)
    todos.complete_todo(settings, todo_id, MONDAY, now=noon(MONDAY))

    assert agenda_todos(settings, MONDAY + timedelta(days=7))[1] == []


def test_a_weekly_to_do_added_on_the_last_day_is_still_owed_that_week(settings: Settings) -> None:
    sunday = MONDAY + timedelta(days=6)
    weekly(settings, sunday)

    assert agenda_todos(settings, sunday)[0] == ["寫週報 (week)"]
    assert agenda_todos(settings, sunday + timedelta(days=1))[1] == ["寫週報 (week)"]


def test_a_weekly_to_do_added_on_the_first_day_of_the_next_week_is_not_undone_yet(settings: Settings) -> None:
    next_week = MONDAY + timedelta(days=7)
    weekly(settings, next_week)

    assert agenda_todos(settings, next_week) == (["寫週報 (week)"], [])


def test_a_weekly_to_do_removed_before_its_week_ended_is_not_undone(settings: Settings) -> None:
    todo_id = weekly(settings)
    friday = MONDAY + timedelta(days=4)
    todos.remove_todo(settings, todo_id, now=noon(friday))

    assert agenda_todos(settings, friday)[0] == []
    assert agenda_todos(settings, MONDAY + timedelta(days=7)) == ([], [])


def test_a_daily_to_do_is_still_undone_every_day(settings: Settings) -> None:
    todos.add_todo(settings, "喝水", None, now=noon(MONDAY))

    assert agenda_todos(settings, MONDAY + timedelta(days=1))[1] == ["喝水"]
    assert agenda_todos(settings, MONDAY + timedelta(days=2))[1] == ["喝水"]


def test_the_agenda_shows_a_period_on_both_lines() -> None:
    content = render_agenda(MONDAY, [], TORONTO, ["寫週報 (week)"], ["繳房租 (month)"])

    assert "To do       | 寫週報 (week)" in content
    assert "Undone      | 11-01: 繳房租 (month)" in content


OLD_TODOS = """CREATE TABLE todos(
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, weekdays TEXT NOT NULL, first_day TEXT NOT NULL,
  removed_on TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE todo_completions(
  todo_id INTEGER NOT NULL, day TEXT NOT NULL, completed_at TEXT NOT NULL, PRIMARY KEY(todo_id, day));
INSERT INTO todos VALUES(1,'喝水','mon,tue,wed,thu,fri,sat,sun','2026-11-02',NULL,'x','x');
INSERT INTO todo_completions VALUES(1,'2026-11-02','x');
"""


def test_a_database_from_before_periods_gets_the_column_and_keeps_its_to_dos(settings: Settings) -> None:
    with closing(sqlite3.connect(settings.database_path)) as connection:
        connection.executescript(OLD_TODOS)

    assert views(settings, MONDAY) == [("喝水", True, True)]
    assert agenda_todos(settings, MONDAY)[0] == []
    assert agenda_todos(settings, MONDAY + timedelta(days=1))[0] == ["喝水"]
    assert rows(settings, "SELECT period FROM todos") == [("day",)]


def test_opening_a_database_for_writing_twice_adds_the_column_once(settings: Settings) -> None:
    with closing(sqlite3.connect(settings.database_path)) as connection:
        connection.executescript(OLD_TODOS)

    Database(settings.database_path).close()
    Database(settings.database_path).close()

    assert rows(settings, "SELECT COUNT(*) FROM pragma_table_info('todos') WHERE name='period'") == [(1,)]


def test_a_database_from_before_periods_can_be_read_without_changing_it(settings: Settings, tmp_path: Path) -> None:
    with closing(sqlite3.connect(settings.database_path)) as connection:
        connection.executescript(OLD_TODOS)
    before = directory_digest(tmp_path)

    listed = todos.list_todos(settings, MONDAY).todos

    assert [(view.title, view.period, view.done) for view in listed] == [("喝水", "day", True)]
    assert directory_digest(tmp_path) == before


def test_the_database_refuses_a_period_there_is_no_such_thing_as(settings: Settings) -> None:
    database = Database(settings.database_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            database.add_todo("x", "mon", MONDAY, "year")
    finally:
        database.close()


def test_add_takes_every_on_the_command_line(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "Settings", lambda: settings)

    result = CliRunner().invoke(cli.app, ["todo", "add", "寫週報", "--every", "week"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["period"] == "week"
    assert rows(settings, "SELECT period FROM todos") == [("week",)]


@pytest.mark.parametrize("arguments", [["--every", "year"], ["--every", "week", "--on", "mon"]])
def test_add_refuses_a_schedule_that_cannot_be(settings: Settings, monkeypatch: pytest.MonkeyPatch, arguments: list[str]) -> None:
    monkeypatch.setattr(cli, "Settings", lambda: settings)

    result = CliRunner().invoke(cli.app, ["todo", "add", "寫週報", *arguments])

    assert result.exit_code == 2
    assert rows_or_empty(settings) == []
