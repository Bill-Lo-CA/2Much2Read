"""The rules for what counts as time spent, kept apart from how it is sent, stored and shown."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from two_read_runtime.text import is_inert

# A session with no closing event ends at the next event, or after this long if nothing follows.
MAX_UNCLOSED_SESSION = timedelta(minutes=30)
# Switching apps sends a close and an open within moments, in either order on arrival.
SWITCH_GRACE = timedelta(seconds=5)
# Reading with no input counts as focus for this long, and a window left showing for longer than the
# other. The second is what lets a video on the other screen keep counting without any input.
FOCUS_IDLE_SECONDS = 5 * 60
VISIBLE_IDLE_SECONDS = 30 * 60
# Less of a monitor than this is a sliver of a window behind others, not something being shown.
MIN_VISIBLE_PERCENT = 10.0
MAX_NAME_CHARACTERS = 100
MAX_SAMPLE_SECONDS = 60
SECONDS_PER_MINUTE = 60
# Pending batches are kept this long on the computer, so anything older is not a real resend.
MAX_SAMPLE_AGE = timedelta(days=14)
MAX_CLOCK_AHEAD = timedelta(minutes=5)

BROWSER_PROCESSES = frozenset({"arc", "brave", "chrome", "chromium", "msedge", "opera", "vivaldi"})

DOMAIN = re.compile(r"(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*\Z")


def clean_name(value: str) -> str:
    """An app or process name fit to store, or ValueError when nothing of it is left."""
    cleaned = " ".join("".join(character for character in unicodedata.normalize("NFC", value) if is_inert(character)).split())
    if not cleaned or len(cleaned) > MAX_NAME_CHARACTERS:
        raise ValueError("not a usable name")
    return cleaned


def clean_domain(value: str) -> str:
    """A hostname with its www. dropped, since the two are one site in a summary."""
    domain = value.strip().lower().removesuffix(".").removeprefix("www.")
    if not DOMAIN.match(domain):
        raise ValueError("not a domain")
    return domain


class _Sent(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScreenEntry(_Sent):
    monitor: int = Field(ge=1, le=64)
    process: str = Field(min_length=1, max_length=MAX_NAME_CHARACTERS * 2)
    percent: float = Field(ge=0, le=100)
    foreground: bool


class ComputerSample(_Sent):
    at: AwareDatetime
    seconds: int = Field(ge=1, le=MAX_SAMPLE_SECONDS)
    idle_seconds: int = Field(ge=0)
    locked: bool
    entries: Annotated[list[ScreenEntry], Field(max_length=256)]


class ComputerBatch(_Sent):
    batch_id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    samples: Annotated[list[ComputerSample], Field(min_length=1, max_length=32)]


class BrowserEntry(_Sent):
    domain: str = Field(min_length=1, max_length=253)
    focused: bool
    visible: bool


class BrowserSample(_Sent):
    at: AwareDatetime
    seconds: int = Field(ge=1, le=MAX_SAMPLE_SECONDS)
    entries: Annotated[list[BrowserEntry], Field(max_length=256)]


@dataclass(frozen=True)
class Counted:
    """Seconds one sample adds for one app or site, in one of the minutes it ran through."""

    minute: str
    name: str
    focused_seconds: int
    visible_seconds: int


def minute_of(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M")


def minutes_spanned(end: datetime, seconds: int) -> list[tuple[str, int]]:
    """How many of the seconds up to a moment fell in each minute.

    A sample carries the time since the one before, which can run back across a minute. Putting all
    of it in the minute it was taken would push that minute past its length, and the excess would be
    lost to the cap on a minute while the minute before it stayed short.
    """
    end = end.astimezone(UTC).replace(microsecond=0)
    moment = end - timedelta(seconds=seconds)
    spanned: list[tuple[str, int]] = []
    while moment < end:
        stop = min(moment.replace(second=0) + timedelta(minutes=1), end)
        spanned.append((minute_of(moment), int((stop - moment).total_seconds())))
        moment = stop
    return spanned


def _counted(end: datetime, seconds: int, focused: set[str], visible: set[str]) -> list[Counted]:
    return [
        Counted(minute, name, spent if name in focused else 0, spent)
        for minute, spent in minutes_spanned(end, seconds)
        for name in sorted(visible)
    ]


def recent(moment: datetime, now: datetime) -> bool:
    return now - MAX_SAMPLE_AGE <= moment <= now + MAX_CLOCK_AHEAD


def count_computer_sample(sample: ComputerSample) -> list[Counted]:
    """One sample's time for each process showing. Two monitors of one app are still the same time."""
    if sample.locked or sample.idle_seconds >= VISIBLE_IDLE_SECONDS:
        return []
    attentive = sample.idle_seconds < FOCUS_IDLE_SECONDS
    focused: set[str] = set()
    visible: set[str] = set()
    for entry in sample.entries:
        name = clean_name(entry.process)
        if entry.foreground:
            visible.add(name)
            if attentive:
                focused.add(name)
        elif entry.percent >= MIN_VISIBLE_PERCENT:
            visible.add(name)
    return _counted(sample.at, sample.seconds, focused, visible)


def count_browser_sample(sample: BrowserSample) -> list[Counted]:
    """One sample's time for each site showing. The extension has already applied the idle limits."""
    focused: set[str] = set()
    visible: set[str] = set()
    for entry in sample.entries:
        domain = clean_domain(entry.domain)
        if entry.visible or entry.focused:
            visible.add(domain)
        if entry.focused:
            focused.add(domain)
    return _counted(sample.at, sample.seconds, focused, visible)


@dataclass(frozen=True)
class PhoneEvent:
    app: str
    kind: Literal["open", "close"]
    at: datetime


@dataclass
class PhoneTime:
    seconds: float = 0.0
    estimated_seconds: float = 0.0


def _session_end(events: list[PhoneEvent], index: int) -> tuple[datetime, bool]:
    """When the session opened at this event ended, and whether that is an estimate."""
    opened = events[index]
    latest = opened.at + MAX_UNCLOSED_SESSION
    next_open: datetime | None = None
    for later in events[index + 1 :]:
        if next_open is not None and later.at > next_open + SWITCH_GRACE:
            break
        if later.app == opened.app:
            if later.kind == "close":
                return later.at, False
            return min(later.at, latest), True
        if later.kind == "open" and next_open is None:
            next_open = later.at
    return (latest if next_open is None else min(next_open, latest)), True


def phone_time(events: list[PhoneEvent], start: datetime, end: datetime) -> dict[str, PhoneTime]:
    """Each app's time between two moments, from events sorted oldest first."""
    totals: dict[str, PhoneTime] = {}
    for index, event in enumerate(events):
        if event.kind != "open":
            continue
        finish, estimated = _session_end(events, index)
        seconds = (min(finish, end) - max(event.at, start)).total_seconds()
        if seconds <= 0:
            continue
        total = totals.setdefault(event.app, PhoneTime())
        total.seconds += seconds
        if estimated:
            total.estimated_seconds += seconds
    return totals
