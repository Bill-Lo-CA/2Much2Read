from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from two_fun_four_distract.usage import (
    FOCUS_IDLE_SECONDS,
    MAX_NAME_CHARACTERS,
    MAX_UNCLOSED_SESSION,
    MIN_VISIBLE_PERCENT,
    VISIBLE_IDLE_SECONDS,
    BrowserEntry,
    BrowserSample,
    ComputerSample,
    Counted,
    PhoneEvent,
    ScreenEntry,
    clean_domain,
    clean_name,
    count_browser_sample,
    count_computer_sample,
    minute_of,
    phone_time,
    recent,
)

AT = datetime(2026, 11, 4, 21, 8, 30, tzinfo=UTC)
MINUTE = "2026-11-04T21:08"


def entry(process: str, percent: float = 50.0, *, foreground: bool = False, monitor: int = 1) -> ScreenEntry:
    return ScreenEntry(monitor=monitor, process=process, percent=percent, foreground=foreground)


def sample(*entries: ScreenEntry, idle_seconds: int = 0, locked: bool = False, seconds: int = 15) -> ComputerSample:
    return ComputerSample(at=AT, seconds=seconds, idle_seconds=idle_seconds, locked=locked, entries=list(entries))


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    [
        ("Discord", "Discord"),
        ("  漫画人极速版  ", "漫画人极速版"),
        ("YouTube   Music", "YouTube Music"),
        ("H&M", "H&M"),
        ("a‮b", "ab"),
        ("a\x00b", "ab"),
    ],
)
def test_clean_name(raw: str, cleaned: str) -> None:
    assert clean_name(raw) == cleaned


def test_clean_name_composes_what_a_phone_sends_decomposed() -> None:
    assert clean_name("Café") == "Café"


@pytest.mark.parametrize("raw", ["", "   ", "\x00", "‮", "x" * (MAX_NAME_CHARACTERS + 1)])
def test_clean_name_refuses_what_leaves_nothing_or_too_much(raw: str) -> None:
    with pytest.raises(ValueError, match="^not a usable name$"):
        clean_name(raw)


def test_clean_name_takes_a_name_at_the_limit() -> None:
    assert clean_name("x" * MAX_NAME_CHARACTERS) == "x" * MAX_NAME_CHARACTERS


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    [
        ("youtube.com", "youtube.com"),
        ("WWW.YouTube.com", "youtube.com"),
        ("www.youtube.com.", "youtube.com"),
        ("music.youtube.com", "music.youtube.com"),
        ("xn--fiq228c.com", "xn--fiq228c.com"),
        ("localhost", "localhost"),
        ("100.89.225.36", "100.89.225.36"),
    ],
)
def test_clean_domain(raw: str, cleaned: str) -> None:
    assert clean_domain(raw) == cleaned


@pytest.mark.parametrize(
    "raw",
    ["", ".", "a..b", "-a.com", "a-.com", "a b.com", "a.com/path", "user@a.com", "[::1]", "http://a.com", "a" * 254],
)
def test_clean_domain_refuses_anything_more_than_a_hostname(raw: str) -> None:
    with pytest.raises(ValueError, match="^not a domain$"):
        clean_domain(raw)


def test_a_minute_is_named_in_utc() -> None:
    assert minute_of(datetime(2026, 11, 4, 16, 8, 59, tzinfo=ZoneInfo("America/Toronto"))) == MINUTE


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (AT, True),
        (AT - timedelta(days=14), True),
        (AT - timedelta(days=14, seconds=1), False),
        (AT + timedelta(minutes=5), True),
        (AT + timedelta(minutes=5, seconds=1), False),
    ],
)
def test_recent(moment: datetime, expected: bool) -> None:
    assert recent(moment, AT) is expected


def test_the_focused_window_counts_both_ways_and_one_showing_only_as_visible() -> None:
    assert count_computer_sample(sample(entry("Code", 72.7, foreground=True), entry("Discord", 94.4, monitor=2))) == [
        Counted(MINUTE, "Code", 15, 15),
        Counted(MINUTE, "Discord", 0, 15),
    ]


def test_an_app_on_two_monitors_is_still_the_same_time() -> None:
    assert count_computer_sample(sample(entry("chrome", 80, monitor=1), entry("chrome", 80, monitor=2))) == [
        Counted(MINUTE, "chrome", 0, 15),
    ]


def test_focus_on_one_monitor_is_enough_when_the_same_app_shows_on_another() -> None:
    assert count_computer_sample(sample(entry("chrome", 80, monitor=1), entry("chrome", 80, foreground=True, monitor=2))) == [
        Counted(MINUTE, "chrome", 15, 15),
    ]


def test_a_sliver_of_a_window_is_not_showing() -> None:
    assert count_computer_sample(sample(entry("Discord", MIN_VISIBLE_PERCENT - 0.1))) == []


def test_exactly_the_least_share_is_showing() -> None:
    assert count_computer_sample(sample(entry("Discord", MIN_VISIBLE_PERCENT))) == [Counted(MINUTE, "Discord", 0, 15)]


def test_the_focused_window_counts_however_little_of_a_monitor_it_covers() -> None:
    assert count_computer_sample(sample(entry("calc", 1.0, foreground=True))) == [Counted(MINUTE, "calc", 15, 15)]


def test_focus_ends_at_its_idle_limit_while_the_window_still_shows() -> None:
    assert count_computer_sample(sample(entry("Code", foreground=True), idle_seconds=FOCUS_IDLE_SECONDS - 1)) == [
        Counted(MINUTE, "Code", 15, 15),
    ]
    assert count_computer_sample(sample(entry("Code", foreground=True), idle_seconds=FOCUS_IDLE_SECONDS)) == [
        Counted(MINUTE, "Code", 0, 15),
    ]


def test_nothing_counts_past_the_visible_idle_limit() -> None:
    assert count_computer_sample(sample(entry("vlc"), idle_seconds=VISIBLE_IDLE_SECONDS - 1)) == [Counted(MINUTE, "vlc", 0, 15)]
    assert count_computer_sample(sample(entry("vlc"), idle_seconds=VISIBLE_IDLE_SECONDS)) == []


def test_nothing_counts_while_locked() -> None:
    assert count_computer_sample(sample(entry("Code", foreground=True), locked=True)) == []


def test_a_sample_counts_for_its_own_length() -> None:
    assert count_computer_sample(sample(entry("Code", foreground=True), seconds=30)) == [Counted(MINUTE, "Code", 30, 30)]


def test_a_process_name_is_cleaned_before_it_counts() -> None:
    assert count_computer_sample(sample(entry("  Code\x00 "))) == [Counted(MINUTE, "Code", 0, 15)]


def test_browser_sites_count_as_the_extension_reports_them() -> None:
    reported = BrowserSample(
        at=AT,
        seconds=30,
        entries=[
            BrowserEntry(domain="www.youtube.com", focused=False, visible=True),
            BrowserEntry(domain="github.com", focused=True, visible=True),
        ],
    )

    assert count_browser_sample(reported) == [Counted(MINUTE, "github.com", 30, 30), Counted(MINUTE, "youtube.com", 0, 30)]


def test_a_focused_site_is_showing_even_when_not_marked_so() -> None:
    reported = BrowserSample(at=AT, seconds=30, entries=[BrowserEntry(domain="a.com", focused=True, visible=False)])

    assert count_browser_sample(reported) == [Counted(MINUTE, "a.com", 30, 30)]


def test_a_site_neither_focused_nor_visible_does_not_count() -> None:
    reported = BrowserSample(at=AT, seconds=30, entries=[BrowserEntry(domain="a.com", focused=False, visible=False)])

    assert count_browser_sample(reported) == []


def test_one_bad_domain_refuses_the_sample() -> None:
    reported = BrowserSample(at=AT, seconds=30, entries=[BrowserEntry(domain="a b", focused=True, visible=True)])

    with pytest.raises(ValueError, match="not a domain"):
        count_browser_sample(reported)


START = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
FAR = START + timedelta(days=1)


def opened(app: str, seconds: float) -> PhoneEvent:
    return PhoneEvent(app, "open", START + timedelta(seconds=seconds))


def closed(app: str, seconds: float) -> PhoneEvent:
    return PhoneEvent(app, "close", START + timedelta(seconds=seconds))


def times(*events: PhoneEvent, start: datetime = START, end: datetime = FAR) -> dict[str, tuple[float, float]]:
    return {app: (total.seconds, total.estimated_seconds) for app, total in phone_time(list(events), start, end).items()}


def test_a_session_runs_from_open_to_close() -> None:
    assert times(opened("A", 0), closed("A", 60)) == {"A": (60, 0)}


def test_sessions_of_an_app_add_up() -> None:
    assert times(opened("A", 0), closed("A", 60), opened("A", 100), closed("A", 130)) == {"A": (90, 0)}


def test_a_switch_counts_each_app_when_the_close_arrives_first() -> None:
    assert times(opened("A", 0), closed("A", 60), opened("B", 60.5), closed("B", 90)) == {"A": (60, 0), "B": (29.5, 0)}


def test_a_switch_counts_each_app_when_the_close_arrives_second() -> None:
    assert times(opened("A", 0), opened("B", 60), closed("A", 60.5), closed("B", 90)) == {"A": (60.5, 0), "B": (30, 0)}


def test_a_close_later_than_the_grace_after_the_next_open_is_not_its_close() -> None:
    assert times(opened("A", 0), opened("B", 60), closed("B", 70), closed("A", 71)) == {"A": (60, 60), "B": (10, 0)}


def test_exactly_at_the_grace_is_still_its_close() -> None:
    assert times(opened("A", 0), opened("B", 60), closed("A", 65), closed("B", 90))["A"] == (65, 0)


def test_a_session_with_no_close_ends_at_the_next_open_and_is_an_estimate() -> None:
    assert times(opened("A", 0), opened("B", 60), closed("B", 90))["A"] == (60, 60)


def test_a_session_with_nothing_after_it_is_capped_and_an_estimate() -> None:
    limit = MAX_UNCLOSED_SESSION.total_seconds()

    assert times(opened("A", 0)) == {"A": (limit, limit)}


def test_a_later_open_does_not_stretch_an_unclosed_session_past_the_cap() -> None:
    limit = MAX_UNCLOSED_SESSION.total_seconds()

    assert times(opened("A", 0), opened("B", limit * 4), closed("B", limit * 4 + 1))["A"] == (limit, limit)


def test_the_same_app_opened_again_ends_the_first_session_as_an_estimate() -> None:
    assert times(opened("A", 0), opened("A", 60), closed("A", 90)) == {"A": (90, 60)}


def test_the_same_app_opened_again_much_later_is_still_capped() -> None:
    limit = MAX_UNCLOSED_SESSION.total_seconds()

    assert times(opened("A", 0), opened("A", limit * 4), closed("A", limit * 4 + 1)) == {"A": (limit + 1, limit)}


def test_a_properly_closed_session_has_no_cap() -> None:
    assert times(opened("A", 0), closed("A", 7200)) == {"A": (7200, 0)}


def test_a_close_with_no_open_counts_for_nothing() -> None:
    assert times(closed("A", 60)) == {}


def test_a_session_across_the_start_counts_only_from_the_start() -> None:
    assert times(opened("A", -30), closed("A", 30)) == {"A": (30, 0)}


def test_a_session_across_the_end_counts_only_to_the_end() -> None:
    assert times(opened("A", 0), closed("A", 60), end=START + timedelta(seconds=45)) == {"A": (45, 0)}


def test_a_session_before_the_start_does_not_stop_the_count_of_those_after_it() -> None:
    # A report reads a day either side, so every report begins with sessions like the first here.
    assert times(opened("A", -90), closed("A", -30), opened("A", 10), closed("A", 20)) == {"A": (10, 0)}


def test_a_session_of_no_length_leaves_no_entry() -> None:
    assert times(opened("A", 60), closed("A", 60)) == {}


def test_estimates_of_an_app_add_up() -> None:
    assert times(opened("A", 0), opened("A", 60), opened("A", 120), closed("A", 130)) == {"A": (130, 120)}


def test_a_session_entirely_outside_counts_for_nothing() -> None:
    assert times(opened("A", -90), closed("A", -30)) == {}
    assert times(opened("A", 90), closed("A", 120), end=START + timedelta(seconds=60)) == {}
