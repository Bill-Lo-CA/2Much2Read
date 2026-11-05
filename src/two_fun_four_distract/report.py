"""One day's time, worked out from what was recorded and written for reading."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from two_read_runtime.text import is_inert

from .storage import Database
from .usage import BROWSER_PROCESSES, SECONDS_PER_MINUTE, Counted, phone_time

SHOWN_PER_LINE = 5
LABEL_WIDTH = 11
OTHER_BROWSING = "其他網頁"
OTHER = "其他"
NO_RECORDS = "沒有紀錄"


@dataclass(frozen=True)
class Line:
    """A total and what it was spent on, largest first."""

    total_seconds: float
    items: list[tuple[str, float]] = field(default_factory=list)


@dataclass(frozen=True)
class DayReport:
    day: date
    phone: Line
    phone_estimated_seconds: float
    focused: Line
    visible: Line


def day_bounds(day: date, timezone: ZoneInfo) -> tuple[datetime, datetime]:
    return (
        datetime.combine(day, time.min, timezone),
        datetime.combine(day + timedelta(days=1), time.min, timezone),
    )


def _line(total: float, seconds_by_name: dict[str, float]) -> Line:
    items = sorted(
        ((name, seconds) for name, seconds in seconds_by_name.items() if seconds > 0), key=lambda item: (-item[1], item[0])
    )
    return Line(total, items)


def _is_browser(name: str) -> bool:
    return name.casefold() in BROWSER_PROCESSES


def _computer_lines(computer: list[Counted], browser: list[Counted], screen_seconds: int) -> tuple[Line, Line]:
    """The focused and the visible line, with a browser's time shown as the sites it was spent on.

    The extension can tell which sites are open but not whether something else covers the window,
    so a site is only counted for as long as the computer saw a browser on screen in that minute.
    What is left of a browser's time after its sites, such as a private window the extension is not
    allowed to see, stays in as browsing with no site named.

    Sites can show at once, in two windows, but only one can have focus. So they share the browser's
    focused time for the minute, the largest claim first, rather than each being held to all of it.
    """
    focused: dict[str, float] = {}
    visible: dict[str, float] = {}
    minute_focused: dict[str, int] = {}
    browser_focused: dict[str, int] = {}
    browser_visible: dict[str, int] = {}
    for row in computer:
        minute_focused[row.minute] = minute_focused.get(row.minute, 0) + row.focused_seconds
        if _is_browser(row.name):
            # Two browsers cannot have focus at once, so theirs add up. They can both show, so theirs may overlap.
            browser_focused[row.minute] = min(browser_focused.get(row.minute, 0) + row.focused_seconds, SECONDS_PER_MINUTE)
            browser_visible[row.minute] = max(browser_visible.get(row.minute, 0), row.visible_seconds)
        else:
            focused[row.name] = focused.get(row.name, 0) + row.focused_seconds
            visible[row.name] = visible.get(row.name, 0) + row.visible_seconds

    focus_left = dict(browser_focused)
    named_visible: dict[str, int] = {}
    for row in sorted(browser, key=lambda row: (row.minute, -row.focused_seconds, row.name)):
        site_focused = min(row.focused_seconds, focus_left.get(row.minute, 0))
        focus_left[row.minute] = focus_left.get(row.minute, 0) - site_focused
        site_visible = min(row.visible_seconds, browser_visible.get(row.minute, 0))
        focused[row.name] = focused.get(row.name, 0) + site_focused
        visible[row.name] = visible.get(row.name, 0) + site_visible
        named_visible[row.minute] = named_visible.get(row.minute, 0) + site_visible

    other_focused = sum(focus_left.values())
    other_visible = sum(max(seconds - named_visible.get(minute, 0), 0) for minute, seconds in browser_visible.items())
    if other_focused:
        focused[OTHER_BROWSING] = other_focused
    if other_visible:
        visible[OTHER_BROWSING] = other_visible
    focused_total = sum(min(seconds, SECONDS_PER_MINUTE) for seconds in minute_focused.values())
    return _line(focused_total, focused), _line(screen_seconds, visible)


def build_report(database: Database | None, day: date, timezone: ZoneInfo, now: datetime) -> DayReport:
    """The day as far as it has gone: a session still open is not counted past this moment."""
    start, end = day_bounds(day, timezone)
    if database is None:
        return DayReport(day, Line(0), 0, Line(0), Line(0))
    # From a day either side, so that a session running across midnight has both of its ends.
    events = database.phone_events(start - timedelta(days=1), end + timedelta(days=1))
    phone = phone_time(events, start, min(end, now))
    focused, visible = _computer_lines(
        database.usage("computer_usage", start, end),
        database.usage("browser_usage", start, end),
        database.screen_seconds(start, end),
    )
    return DayReport(
        day,
        _line(sum(entry.seconds for entry in phone.values()), {app: entry.seconds for app, entry in phone.items()}),
        sum(entry.estimated_seconds for entry in phone.values()),
        focused,
        visible,
    )


def format_duration(seconds: float) -> str:
    minutes = int(seconds // SECONDS_PER_MINUTE)
    if minutes < 1:
        return "<1m"
    hours, minutes = divmod(minutes, SECONDS_PER_MINUTE)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"


def _display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(character) in {"F", "W"} else 1 for character in text)


def _shown_name(name: str) -> str:
    """A name fit for a code block in Discord, where a mention or a closing fence would still act."""
    kept = "".join(character for character in name if is_inert(character))
    return " ".join(kept.replace("@", "@​").replace("`", "ˋ").split())


def _row(label: str, line: Line) -> str:
    padding = " " * max(LABEL_WIDTH - _display_width(label), 0)
    if not line.total_seconds:
        return f"{label}{padding} {'':>6} | {NO_RECORDS}"
    shown = [f"{_shown_name(name)} {format_duration(seconds)}" for name, seconds in line.items[:SHOWN_PER_LINE]]
    rest = sum(seconds for _, seconds in line.items[SHOWN_PER_LINE:])
    if rest:
        shown.append(f"{OTHER} {format_duration(rest)}")
    return f"{label}{padding} {format_duration(line.total_seconds):>6} | {' · '.join(shown)}"


def render(report: DayReport) -> str:
    lines = [
        f"2fun4distract · {report.day.isoformat()}",
        _row("手機", report.phone),
        _row("電腦 焦點", report.focused),
        _row("電腦 看得到", report.visible),
    ]
    if report.phone_estimated_seconds >= SECONDS_PER_MINUTE:
        lines.append(f"手機有 {format_duration(report.phone_estimated_seconds)} 是估計值：沒收到關閉事件")
    return "\n".join(lines)
