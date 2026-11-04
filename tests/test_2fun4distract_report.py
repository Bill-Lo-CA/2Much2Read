from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from conftest import directory_digest
from typer.testing import CliRunner

from two_fun_four_distract import cli, config, pipeline
from two_fun_four_distract.config import Settings
from two_fun_four_distract.report import (
    DayReport,
    Line,
    _display_width,
    _shown_name,
    build_report,
    day_bounds,
    format_duration,
    render,
)
from two_fun_four_distract.storage import Database
from two_fun_four_distract.usage import Counted, minute_of
from two_read_runtime.discord import DiscordDeliveryError

TORONTO = ZoneInfo("America/Toronto")
DAY = date(2026, 11, 4)
NEXT_MORNING = datetime(2026, 11, 5, 8, 0, tzinfo=TORONTO)
WEBHOOK = "https://discord.com/api/webhooks/123456789012345678/test-webhook-token"


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setattr(config, "settings_env_file", lambda: tmp_path / "no-such.env")
    return Settings(database_path=tmp_path / "usage.sqlite3", lock_path=tmp_path / "usage.lock", discord_webhook_url=WEBHOOK)


@pytest.fixture
def database(settings: Settings) -> Database:
    return Database(settings.database_path)


def at(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 11, 4, hour, minute, second, tzinfo=TORONTO)


def computer(database: Database, moment: datetime, name: str, focused: int, visible: int) -> None:
    minute = minute_of(moment)
    database.add_computer_batch(f"{moment}{name}", [Counted(minute, name, focused, visible)], moment, shown=[(minute, visible)])


def browser(database: Database, moment: datetime, name: str, focused: int, visible: int) -> None:
    database.add_browser_usage([Counted(minute_of(moment), name, focused, visible)])


def report(database: Database | None, day: date = DAY, now: datetime = NEXT_MORNING) -> DayReport:
    return build_report(database, day, TORONTO, now)


def test_a_day_is_the_timezones_even_when_it_is_not_24_hours_long() -> None:
    start, end = day_bounds(date(2026, 11, 1), TORONTO)

    assert start.astimezone(UTC) == datetime(2026, 11, 1, 4, 0, tzinfo=UTC)
    assert end.astimezone(UTC) == datetime(2026, 11, 2, 5, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0, "<1m"), (59, "<1m"), (60, "1m"), (119, "1m"), (59 * 60, "59m"), (3600, "1h00m"), (3660, "1h01m"), (36000, "10h00m")],
)
def test_format_duration(seconds: int, text: str) -> None:
    assert format_duration(seconds) == text


def test_no_database_is_a_report_of_nothing() -> None:
    assert report(None) == DayReport(DAY, Line(0), 0, Line(0), Line(0))


def test_nothing_recorded_says_so_on_every_line() -> None:
    assert render(report(None)) == "\n".join(
        [
            "2fun4distract · 2026-11-04",
            "手機               | 沒有紀錄",
            "電腦 焦點          | 沒有紀錄",
            "電腦 看得到        | 沒有紀錄",
        ]
    )


def test_the_whole_report_reads_like_this(database: Database) -> None:
    database.add_phone_event("漫画人极速版", "open", at(10))
    database.add_phone_event("漫画人极速版", "close", at(11, 2))
    database.add_phone_event("世界迷霧", "open", at(12))
    database.add_phone_event("世界迷霧", "close", at(12, 38))
    for minute in range(3):
        computer(database, at(9, minute), "Code", 60, 60)
        computer(database, at(9, minute), "Discord", 0, 60)
    computer(database, at(9, 3), "chrome", 60, 60)
    browser(database, at(9, 3), "youtube.com", 60, 60)

    assert render(report(database)) == "\n".join(
        [
            "2fun4distract · 2026-11-04",
            "手機         1h40m | 漫画人极速版 1h02m · 世界迷霧 38m",
            "電腦 焦點       4m | Code 3m · youtube.com 1m",
            "電腦 看得到     4m | Code 3m · Discord 3m · youtube.com 1m",
        ]
    )


def test_time_on_two_monitors_at_once_adds_up_per_app_but_not_in_the_total(database: Database) -> None:
    computer(database, at(9), "Code", 60, 60)
    computer(database, at(9), "vlc", 0, 60)

    line = report(database).visible

    assert line.total_seconds == 60
    assert line.items == [("Code", 60), ("vlc", 60)]


def test_focused_time_cannot_pass_the_length_of_a_minute(database: Database) -> None:
    computer(database, at(9), "Code", 45, 45)
    computer(database, at(9), "Discord", 45, 45)

    assert report(database).focused.total_seconds == 60


def test_a_browsers_time_is_shown_as_its_sites(database: Database) -> None:
    computer(database, at(9), "chrome", 60, 60)
    browser(database, at(9), "github.com", 60, 60)

    built = report(database)

    assert built.focused.items == [("github.com", 60)]
    assert built.visible.items == [("github.com", 60)]


def test_a_site_counts_only_while_a_browser_was_on_screen(database: Database) -> None:
    # The window sits behind something else: the extension still reports its active tab.
    computer(database, at(9), "Code", 60, 60)
    browser(database, at(9), "youtube.com", 60, 60)

    built = report(database)

    assert built.visible.items == [("Code", 60)]
    assert built.focused.items == [("Code", 60)]


def test_a_sites_minutes_add_up(database: Database) -> None:
    for minute in range(2):
        computer(database, at(9, minute), "chrome", 60, 60)
        browser(database, at(9, minute), "a.com", 60, 60)

    built = report(database)

    assert built.focused.items == [("a.com", 120)]
    assert built.visible.items == [("a.com", 120)]


def test_all_sites_in_a_minute_count_against_the_browsers_time(database: Database) -> None:
    computer(database, at(9), "chrome", 60, 60)
    browser(database, at(9), "a.com", 20, 20)
    browser(database, at(9), "b.com", 20, 20)

    built = report(database)

    assert built.focused.items == [("a.com", 20), ("b.com", 20), ("其他網頁", 20)]
    assert built.visible.items == [("a.com", 20), ("b.com", 20), ("其他網頁", 20)]


def test_sites_share_the_browsers_focus_rather_than_each_having_all_of_it(database: Database) -> None:
    computer(database, at(9), "chrome", 30, 30)
    computer(database, at(9), "Code", 30, 30)
    browser(database, at(9), "a.com", 30, 30)
    browser(database, at(9), "b.com", 30, 30)

    built = report(database)

    assert built.focused.total_seconds == 60
    assert built.focused.items == [("Code", 30), ("a.com", 30)]
    assert built.visible.items == [("Code", 30), ("a.com", 30), ("b.com", 30)]


def test_the_largest_claim_on_a_browsers_focus_goes_first(database: Database) -> None:
    computer(database, at(9), "chrome", 30, 30)
    browser(database, at(9), "a.com", 10, 10)
    browser(database, at(9), "b.com", 50, 50)

    assert report(database).focused.items == [("b.com", 30)]


def test_a_browsers_focus_is_shared_within_its_own_minute_only(database: Database) -> None:
    for minute in range(2):
        computer(database, at(9, minute), "chrome", 30, 30)
        browser(database, at(9, minute), "a.com", 30, 30)

    assert report(database).focused.items == [("a.com", 60)]


def test_a_site_with_no_browser_that_minute_takes_no_focus(database: Database) -> None:
    browser(database, at(9), "a.com", 30, 30)

    built = report(database)

    assert built.focused.items == []
    assert built.focused.total_seconds == 0


def test_equal_times_read_in_name_order(database: Database) -> None:
    computer(database, at(9, 0), "zeta", 60, 60)
    computer(database, at(9, 1), "alpha", 60, 60)

    assert [name for name, _ in report(database).focused.items] == ["alpha", "zeta"]


def test_a_second_of_time_is_still_listed(database: Database) -> None:
    computer(database, at(9), "brief", 1, 1)

    assert report(database).focused.items == [("brief", 1)]


def test_focused_time_in_a_minute_is_exact(database: Database) -> None:
    computer(database, at(9), "a", 20, 40)
    computer(database, at(9), "b", 30, 20)

    assert report(database).focused.total_seconds == 50


def test_time_on_screen_is_what_each_sample_said_not_the_largest_app(database: Database) -> None:
    minute = minute_of(at(9))
    rows = [Counted(minute, "Code", 30, 30), Counted(minute, "Notepad", 30, 30)]
    database.add_computer_batch("batch", rows, at(9), shown=[(minute, 30), (minute, 30)])

    built = report(database)

    assert built.visible.total_seconds == 60
    assert built.visible.items == [("Code", 30), ("Notepad", 30)]


def test_time_on_screen_cannot_pass_the_length_of_a_minute(database: Database) -> None:
    minute = minute_of(at(9))
    database.add_computer_batch("first", [], at(9), shown=[(minute, 45)])
    database.add_computer_batch("second", [], at(9), shown=[(minute, 45)])

    assert report(database).visible.total_seconds == 60


def test_a_single_sample_cannot_pass_the_length_of_a_minute_on_screen(database: Database) -> None:
    database.add_computer_batch("batch", [], at(9), shown=[(minute_of(at(9)), 90)])

    assert database.screen_seconds(at(9), at(10)) == 60


def test_only_the_days_time_on_screen_counts(database: Database) -> None:
    for moment in (at(0, 0) - timedelta(minutes=1), at(0, 0), at(23, 59), at(23, 59) + timedelta(minutes=1)):
        database.add_computer_batch(str(moment), [], moment, shown=[(minute_of(moment), 60)])

    assert report(database).visible.total_seconds == 120


def test_a_batch_already_taken_adds_no_time_on_screen(database: Database) -> None:
    minute = minute_of(at(9))

    database.add_computer_batch("batch", [], at(9), shown=[(minute, 15)])
    database.add_computer_batch("batch", [], at(9), shown=[(minute, 15)])

    assert database.screen_seconds(at(9), at(10)) == 15


def test_a_site_counts_no_longer_than_a_browser_was_on_screen_that_minute(database: Database) -> None:
    computer(database, at(9), "chrome", 15, 30)
    browser(database, at(9), "youtube.com", 60, 60)

    built = report(database)

    assert built.focused.items == [("youtube.com", 15)]
    assert built.visible.items == [("youtube.com", 30)]


def test_browsing_no_site_was_reported_for_stays_in_as_other_browsing(database: Database) -> None:
    # A private window: the agent sees the browser, and the extension is not allowed to.
    computer(database, at(9), "chrome", 60, 60)

    built = report(database)

    assert built.focused.items == [("其他網頁", 60)]
    assert built.visible.items == [("其他網頁", 60)]


def test_part_of_a_minute_unaccounted_for_is_other_browsing(database: Database) -> None:
    computer(database, at(9), "chrome", 60, 60)
    browser(database, at(9), "github.com", 30, 30)

    built = report(database)

    assert built.focused.items == [("github.com", 30), ("其他網頁", 30)]
    assert built.visible.items == [("github.com", 30), ("其他網頁", 30)]


def test_two_sites_showing_at_once_leave_no_negative_remainder(database: Database) -> None:
    computer(database, at(9), "chrome", 0, 60)
    browser(database, at(9), "a.com", 0, 60)
    browser(database, at(9), "b.com", 0, 60)

    built = report(database)

    assert built.visible.items == [("a.com", 60), ("b.com", 60)]
    assert built.focused.items == []


def test_a_browser_process_name_is_matched_whatever_its_case(database: Database) -> None:
    computer(database, at(9), "MSEdge", 60, 60)
    browser(database, at(9), "a.com", 60, 60)

    assert report(database).focused.items == [("a.com", 60)]


def test_a_browser_in_two_processes_is_not_counted_twice(database: Database) -> None:
    computer(database, at(9), "chrome", 45, 50)
    computer(database, at(9), "msedge", 15, 20)
    browser(database, at(9), "a.com", 45, 50)

    built = report(database)

    assert built.visible.items == [("a.com", 50)]
    assert built.focused.items == [("a.com", 45)]


def test_only_the_days_minutes_count(database: Database) -> None:
    computer(database, at(0, 0) - timedelta(minutes=1), "before", 60, 60)
    computer(database, at(0, 0), "first", 60, 60)
    computer(database, at(23, 59), "last", 60, 60)
    computer(database, at(23, 59) + timedelta(minutes=1), "after", 60, 60)

    assert [name for name, _ in report(database).focused.items] == ["first", "last"]


def test_a_phone_session_across_midnight_counts_on_each_side(database: Database) -> None:
    database.add_phone_event("A", "open", at(23, 30))
    database.add_phone_event("A", "close", at(23, 59) + timedelta(minutes=31))

    assert report(database, DAY).phone.total_seconds == 30 * 60
    assert report(database, DAY + timedelta(days=1)).phone.total_seconds == 30 * 60


def test_a_session_still_open_is_not_counted_past_now(database: Database) -> None:
    database.add_phone_event("A", "open", at(10))

    assert report(database, now=at(10, 5)).phone.total_seconds == 5 * 60


def test_an_estimate_is_said_to_be_one(database: Database) -> None:
    database.add_phone_event("A", "open", at(10))

    built = report(database)

    assert built.phone_estimated_seconds == 30 * 60
    assert render(built).endswith("\n手機有 30m 是估計值：沒收到關閉事件")


def test_an_estimate_of_exactly_a_minute_is_said_to_be_one() -> None:
    built = DayReport(DAY, Line(60, [("A", 60)]), 60, Line(0), Line(0))

    assert render(built).endswith("\n手機有 1m 是估計值：沒收到關閉事件")


def test_an_estimate_under_a_minute_is_not_worth_a_line() -> None:
    built = DayReport(DAY, Line(59, [("A", 59)]), 59, Line(0), Line(0))

    assert "估計" not in render(built)


def test_wide_characters_count_as_two_columns() -> None:
    assert _display_width("a") == 1
    assert _display_width("手") == 2
    assert _display_width("\uff21") == 2


def test_no_estimate_means_no_such_line(database: Database) -> None:
    database.add_phone_event("A", "open", at(10))
    database.add_phone_event("A", "close", at(10, 5))

    assert "估計" not in render(report(database))


def test_a_line_shows_the_five_largest_and_the_rest_together() -> None:
    items = [(f"app{index}", 600.0 - index * 60) for index in range(7)]
    built = DayReport(DAY, Line(sum(seconds for _, seconds in items), items), 0, Line(0), Line(0))

    assert render(built).splitlines()[1] == "手機           49m | app0 10m · app1 9m · app2 8m · app3 7m · app4 6m · 其他 9m"


def test_a_name_cannot_mention_anyone_or_close_the_code_block() -> None:
    built = DayReport(DAY, Line(600, [("@everyone```\n@here", 600)]), 0, Line(0), Line(0))

    line = render(built).splitlines()[1]

    assert "@everyone" not in line
    assert "@here" not in line
    assert "`" not in line


def test_a_shown_name_keeps_its_words_and_loses_its_power() -> None:
    assert _shown_name("  @here  `code`\x00 ") == "@\u200bhere ˋcodeˋ"


def test_times_are_stored_in_utc(settings: Settings) -> None:
    database = Database(settings.database_path)
    database.add_phone_event("A", "open", at(10))
    database.add_computer_batch("batch", [], at(10))
    database.record_summary(DAY, "key", ["1"], at(10))
    stored = [
        database.connection.execute("SELECT at FROM phone_events").fetchone()[0],
        database.connection.execute("SELECT received_at FROM computer_batches").fetchone()[0],
        database.connection.execute("SELECT delivered_at FROM summaries").fetchone()[0],
    ]
    database.close()

    assert stored == ["2026-11-04T15:00:00+00:00"] * 3


def test_phone_events_are_read_from_the_start_up_to_the_end(database: Database) -> None:
    second = timedelta(seconds=1)
    for name, moment in (("before", at(10) - second), ("first", at(10)), ("last", at(11) - second), ("after", at(11))):
        database.add_phone_event(name, "open", moment)

    assert [event.app for event in database.phone_events(at(10), at(11))] == ["first", "last"]


def test_a_batch_already_taken_is_refused_and_adds_nothing(database: Database) -> None:
    row = Counted("2026-11-04T15:00", "Code", 15, 15)

    assert database.add_computer_batch("batch", [row], at(10)) is True
    assert database.add_computer_batch("batch", [row], at(10)) is False

    assert database.usage("computer_usage", at(9), at(11)) == [row]


def test_report_reads_without_changing_anything(settings: Settings, tmp_path: Path) -> None:
    database = Database(settings.database_path)
    database.add_phone_event("A", "open", at(10))
    database.close()
    before = directory_digest(tmp_path)

    day, text = pipeline.report_text(settings, DAY, now=NEXT_MORNING)

    assert day == DAY
    assert "A 30m" in text
    assert directory_digest(tmp_path) == before


def test_report_is_of_today_unless_told_otherwise(settings: Settings) -> None:
    assert pipeline.report_text(settings, now=NEXT_MORNING)[0] == date(2026, 11, 5)


# 23:00 on the 4th in Toronto is already 13:00 on the 5th in Tokyo.
LATE_IN_TORONTO = datetime(2026, 11, 4, 23, 0, tzinfo=TORONTO)


def in_tokyo(settings: Settings) -> Settings:
    return settings.model_copy(update={"summary_timezone": "Asia/Tokyo"})


def test_today_is_the_summary_timezones(settings: Settings) -> None:
    assert pipeline.report_text(in_tokyo(settings), now=LATE_IN_TORONTO)[0] == date(2026, 11, 5)


def test_yesterday_is_the_summary_timezones(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    sent_to(monkeypatch)

    assert pipeline.preview_summary(in_tokyo(settings), now=LATE_IN_TORONTO).day == DAY
    assert pipeline.send_summary(in_tokyo(settings), now=LATE_IN_TORONTO).day == DAY


def test_a_day_is_cut_in_the_summary_timezone(settings: Settings) -> None:
    database = Database(settings.database_path)
    # 01:00 on the 5th in Tokyo, which is still the 4th in Toronto.
    computer(database, datetime(2026, 11, 5, 1, 0, tzinfo=ZoneInfo("Asia/Tokyo")), "Code", 60, 60)
    database.close()

    assert "Code" in pipeline.report_text(in_tokyo(settings), date(2026, 11, 5), now=NEXT_MORNING)[1]
    assert "Code" not in pipeline.report_text(in_tokyo(settings), date(2026, 11, 4), now=NEXT_MORNING)[1]


def test_a_preview_of_today_counts_only_up_to_now(settings: Settings) -> None:
    database = Database(settings.database_path)
    database.add_phone_event("A", "open", at(10))
    database.close()

    assert "A 5m" in pipeline.preview_summary(settings, DAY, now=at(10, 5)).content


def test_a_report_with_no_database_creates_none(settings: Settings) -> None:
    assert "沒有紀錄" in pipeline.report_text(settings, DAY, now=NEXT_MORNING)[1]
    assert not settings.database_path.exists()


def test_a_preview_is_of_yesterday_and_sends_nothing(settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def must_not_send(*_: object, **__: object) -> list[str]:
        raise AssertionError("a preview sent something")

    monkeypatch.setattr(pipeline, "deliver", must_not_send)
    before = directory_digest(tmp_path)

    preview = pipeline.preview_summary(settings, now=NEXT_MORNING)

    assert preview.day == DAY
    assert preview.content.startswith("```text\n2fun4distract · 2026-11-04\n")
    assert preview.content.endswith("\n```")
    assert directory_digest(tmp_path) == before


def sent_to(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    sent: list[tuple[str, str, str]] = []

    def deliver(destination: object, content: str, username: str) -> list[str]:
        sent.append((str(getattr(destination, "transport", "")), content, username))
        return ["111"]

    monkeypatch.setattr(pipeline, "deliver", deliver)
    return sent


def summaries(settings: Settings) -> list[tuple[object, ...]]:
    with closing(sqlite3.connect(settings.database_path)) as connection:
        return connection.execute("SELECT day,discord_message_ids_json FROM summaries").fetchall()


def test_the_summary_is_of_yesterday_and_goes_out_once(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    sent = sent_to(monkeypatch)

    first = pipeline.send_summary(settings, now=NEXT_MORNING)
    second = pipeline.send_summary(settings, now=NEXT_MORNING)

    assert (first.status, first.day, first.sent, first.skipped) == ("ok", DAY, 1, 0)
    assert first.discord_message_ids == ["111"]
    assert (second.status, second.sent, second.skipped) == ("ok", 0, 1)
    assert len(sent) == 1
    assert sent[0][0] == "webhook"
    assert sent[0][1].startswith("```text\n2fun4distract · 2026-11-04\n")
    assert sent[0][2] == "2fun4distract"
    assert summaries(settings) == [("2026-11-04", json.dumps(["111"]))]


def test_the_summary_carries_what_was_recorded(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    sent = sent_to(monkeypatch)
    database = Database(settings.database_path)
    database.add_phone_event("世界迷霧", "open", at(10))
    database.add_phone_event("世界迷霧", "close", at(10, 38))
    computer(database, at(9), "Code", 60, 60)
    database.close()

    pipeline.send_summary(settings, now=NEXT_MORNING)

    assert sent[0][1] == "\n".join(
        [
            "```text",
            "2fun4distract · 2026-11-04",
            "手機           38m | 世界迷霧 38m",
            "電腦 焦點       1m | Code 1m",
            "電腦 看得到     1m | Code 1m",
            "```",
        ]
    )


def in_both(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"discord_delivery_mode": "both", "discord_bot_token": "x" * 60, "discord_bot_channel_id": "123456789012345678"}
    )


def test_two_destinations_are_each_counted(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    sent = sent_to(monkeypatch)

    first = pipeline.send_summary(in_both(settings), now=NEXT_MORNING)
    second = pipeline.send_summary(in_both(settings), now=NEXT_MORNING)

    assert (first.sent, first.skipped) == (2, 0)
    assert first.discord_message_ids == ["111", "111"]
    assert (second.sent, second.skipped) == (0, 2)
    assert len(sent) == 2


def test_two_destinations_failing_are_each_counted_by_their_code(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    def fails(*_: object, **__: object) -> list[str]:
        raise DiscordDeliveryError("DISCORD_RATE_LIMITED")

    monkeypatch.setattr(pipeline, "deliver", fails)

    result = pipeline.send_summary(in_both(settings), now=NEXT_MORNING)

    assert result.failed == 2
    assert result.failed_by_error_code == {"DISCORD_RATE_LIMITED": 2}


def test_one_destination_failing_does_not_stop_the_next(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    reached: list[str] = []

    def webhook_fails(destination: object, content: str, username: str) -> list[str]:
        transport = str(getattr(destination, "transport", ""))
        if transport == "webhook":
            raise DiscordDeliveryError("DISCORD_RATE_LIMITED")
        reached.append(transport)
        return ["111"]

    monkeypatch.setattr(pipeline, "deliver", webhook_fails)

    result = pipeline.send_summary(in_both(settings), now=NEXT_MORNING)

    assert reached == ["bot"]
    assert (result.status, result.sent, result.failed) == ("partial", 1, 1)


def test_force_sends_a_day_again(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    sent = sent_to(monkeypatch)

    pipeline.send_summary(settings, now=NEXT_MORNING)
    forced = pipeline.send_summary(settings, force=True, now=NEXT_MORNING)

    assert forced.sent == 1
    assert len(sent) == 2
    assert len(summaries(settings)) == 1


def test_a_named_day_is_sent_as_that_day(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    sent = sent_to(monkeypatch)

    assert pipeline.send_summary(settings, date(2026, 10, 1), now=NEXT_MORNING).day == date(2026, 10, 1)
    assert "2026-10-01" in sent[0][1]


def test_a_failed_send_is_reported_and_tried_again_next_time(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    def fails(*_: object, **__: object) -> list[str]:
        raise DiscordDeliveryError("DISCORD_RATE_LIMITED")

    monkeypatch.setattr(pipeline, "deliver", fails)
    failed = pipeline.send_summary(settings, now=NEXT_MORNING)

    assert (failed.status, failed.failed, failed.sent) == ("failed", 1, 0)
    assert sum(failed.failed_by_error_code.values()) == 1
    assert summaries(settings) == []

    sent = sent_to(monkeypatch)
    assert pipeline.send_summary(settings, now=NEXT_MORNING).sent == 1
    assert len(sent) == 1


def test_a_send_that_reached_one_destination_of_two_is_partial_and_repeats_only_the_other(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    both = settings.model_copy(
        update={"discord_delivery_mode": "both", "discord_bot_token": "x" * 60, "discord_bot_channel_id": "123456789012345678"}
    )
    calls: list[str] = []

    def bot_fails(destination: object, content: str, username: str) -> list[str]:
        transport = str(getattr(destination, "transport", ""))
        calls.append(transport)
        if transport == "bot":
            raise DiscordDeliveryError("DISCORD_BAD_REQUEST")
        return ["111"]

    monkeypatch.setattr(pipeline, "deliver", bot_fails)
    first = pipeline.send_summary(both, now=NEXT_MORNING)

    assert (first.status, first.sent, first.failed) == ("partial", 1, 1)

    calls.clear()
    second = pipeline.send_summary(both, now=NEXT_MORNING)

    assert calls == ["bot"]
    assert second.skipped == 1
    assert second.status == "partial"


def test_a_summary_with_no_discord_settings_is_refused(settings: Settings) -> None:
    empty = settings.model_copy(update={"discord_webhook_url": ""})

    with pytest.raises(ValueError):
        pipeline.send_summary(empty, now=NEXT_MORNING)


def test_the_report_command_prints_the_report(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "Settings", lambda: settings)

    result = CliRunner().invoke(cli.app, ["report", "--day", "2026-11-04"])

    assert result.exit_code == 0
    assert result.output.startswith("2fun4distract · 2026-11-04\n")


@pytest.mark.parametrize("command", ["report", "summary"])
def test_a_day_that_is_not_a_date_is_refused(settings: Settings, monkeypatch: pytest.MonkeyPatch, command: str) -> None:
    monkeypatch.setattr(cli, "Settings", lambda: settings)

    assert CliRunner().invoke(cli.app, [command, "--day", "yesterday"]).exit_code == 2


def test_the_summary_command_dry_run_shows_it(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "Settings", lambda: settings)

    result = CliRunner().invoke(cli.app, ["summary", "--day", "2026-11-04", "--dry-run"])

    assert result.exit_code == 0
    assert json.loads(result.output)["day"] == "2026-11-04"
    assert not settings.database_path.exists()


def test_the_summary_command_sends_and_fails_with_the_send(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    sent_to(monkeypatch)

    result = CliRunner().invoke(cli.app, ["summary", "--day", "2026-11-04"])

    assert result.exit_code == 0
    assert json.loads(result.output)["sent"] == 1

    def fails(*_: object, **__: object) -> list[str]:
        raise DiscordDeliveryError("DISCORD_RATE_LIMITED")

    monkeypatch.setattr(pipeline, "deliver", fails)
    result = CliRunner().invoke(cli.app, ["summary", "--day", "2026-11-03"])

    assert result.exit_code == 1
    assert json.loads(result.output)["status"] == "failed"


def test_doctor_reports_without_showing_the_webhook(settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env = tmp_path / ".2fun4distract.env"
    env.write_text(f"DISCORD_WEBHOOK_URL={WEBHOOK}\n", encoding="utf-8")
    env.chmod(0o600)
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(cli, "settings_env_file", lambda: env)

    result = CliRunner().invoke(cli.app, ["doctor"])

    assert result.exit_code == 0
    assert WEBHOOK not in result.output
    payload = json.loads(result.output)
    assert payload["checks"]["discord"] == "webhook"
    assert payload["checks"]["env_file"] == "ok"
    assert payload["checks"]["env_keys"] == "ok"


def test_doctor_warns_of_missing_discord_settings_and_unknown_keys(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = tmp_path / ".2fun4distract.env"
    env.write_text("NOT_A_SETTING=1\n", encoding="utf-8")
    env.chmod(0o600)
    monkeypatch.setattr(cli, "Settings", lambda: settings.model_copy(update={"discord_webhook_url": ""}))
    monkeypatch.setattr(cli, "settings_env_file", lambda: env)

    payload = json.loads(CliRunner().invoke(cli.app, ["doctor"]).output)

    assert payload["status"] == "warning"
    assert payload["checks"]["discord"] == "missing"
    assert payload["unknown_env_keys"] == ["NOT_A_SETTING"]


def test_the_settings_default_to_toronto_at_eight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "settings_env_file", lambda: tmp_path / "no-such.env")
    built = Settings()

    assert built.summary_timezone == "America/Toronto"
    assert built.summary_schedule_time.isoformat("minutes") == "08:00"


@pytest.mark.parametrize(
    "values",
    [{"summary_timezone": "Mars/Olympus"}, {"summary_schedule_time": "08:00:30"}],
)
def test_the_settings_refuse_a_bad_schedule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, values: dict[str, str]) -> None:
    monkeypatch.setattr(config, "settings_env_file", lambda: tmp_path / "no-such.env")

    with pytest.raises(ValueError):
        Settings(**values)
