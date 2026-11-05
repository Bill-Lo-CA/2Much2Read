"""The prompts behind `2busy1miss todo add` with no title: a list of options and the arrow keys."""

from __future__ import annotations

import os
import select
import sys
import termios
import tty
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import NamedTuple, TextIO

from .rules import WEEKDAYS

UP = "up"
DOWN = "down"
ENTER = "enter"
SPACE = "space"
CANCEL = "cancel"

# How long a lone Escape byte waits to see whether it starts an arrow key's sequence.
ESCAPE_WAIT_SECONDS = 0.05

QUESTION = "How often?"
EVERY_DAY = ",".join(WEEKDAYS)
PRESETS = (
    ("Every day", EVERY_DAY),
    ("Weekdays (mon-fri)", ",".join(WEEKDAYS[:5])),
    ("Weekends (sat, sun)", ",".join(WEEKDAYS[5:])),
)
PICK_DAYS = "Pick days..."
ONCE_IN_A_PERIOD = (
    ("Once a week (any day)", "week"),
    ("Once a month (any day)", "month"),
)
CHOOSE_HINT = "Up/Down to move, Enter to choose, Esc to cancel"
PICK_HINT = "Up/Down to move, Space to tick, Enter to confirm, Esc to go back"
NONE_PICKED_HINT = "Tick at least one day, or press Esc to go back"

ReadKey = Callable[[], str]


class Schedule(NamedTuple):
    """What was chosen: the weekdays as `--on` takes them, if it has any, and the period."""

    on: str | None
    period: str


def _escape_sequence(descriptor: int) -> str:
    if not select.select([descriptor], [], [], ESCAPE_WAIT_SECONDS)[0]:
        return CANCEL
    if os.read(descriptor, 1) not in {b"[", b"O"}:
        return ""
    final = os.read(descriptor, 1)
    while final and not b"\x40" <= final <= b"\x7e":
        final = os.read(descriptor, 1)
    return {b"A": UP, b"B": DOWN}.get(final, "")


def read_key(descriptor: int) -> str:
    """One keypress by name or as its character, and an empty string for any other, which is ignored."""
    first = os.read(descriptor, 1)
    # No bytes means the terminal has gone, and waiting for another key would never end.
    if first in {b"", b"\x04"}:
        return CANCEL
    if first in {b"\r", b"\n"}:
        return ENTER
    if first == b" ":
        return SPACE
    if first == b"\x1b":
        return _escape_sequence(descriptor)
    key = first.decode("ascii", "ignore")
    return {"k": UP, "j": DOWN, "q": CANCEL}.get(key, key)


class _Drawing:
    """The lines last written, so that the next keypress can replace them where they are."""

    def __init__(self, out: TextIO) -> None:
        self.out = out
        self.height = 0

    def show(self, lines: Sequence[str]) -> None:
        self.erase()
        self.out.write("".join(f"{line}\n" for line in lines))
        self.out.flush()
        self.height = len(lines)

    def erase(self) -> None:
        if self.height:
            self.out.write(f"\x1b[{self.height}A\r\x1b[J")
            self.out.flush()
            self.height = 0


def _moved(cursor: int, key: str, count: int) -> int:
    if key == UP:
        return (cursor - 1) % count
    if key == DOWN:
        return (cursor + 1) % count
    return cursor


def _numbered(key: str, count: int) -> int | None:
    """Which option a digit names, as an index."""
    return int(key) - 1 if key.isdecimal() and 1 <= int(key) <= count else None


def _pointer(index: int, cursor: int) -> str:
    return "❯" if index == cursor else " "


def choose(question: str, options: Sequence[str], read: ReadKey, out: TextIO) -> int | None:
    """The index of the one option chosen, or None when cancelled."""
    drawing = _Drawing(out)
    cursor = 0
    while True:
        rows = [f"{_pointer(index, cursor)} {index + 1}. {option}" for index, option in enumerate(options)]
        drawing.show([question, *rows, CHOOSE_HINT])
        key = read()
        chosen = cursor if key == ENTER else _numbered(key, len(options))
        if key == CANCEL or chosen is not None:
            drawing.erase()
            return chosen
        cursor = _moved(cursor, key, len(options))


def choose_many(question: str, options: Sequence[str], read: ReadKey, out: TextIO) -> list[int] | None:
    """The indexes of the options ticked, in order and never empty, or None when backed out of."""
    drawing = _Drawing(out)
    cursor = 0
    ticked: set[int] = set()
    hint = PICK_HINT
    while True:
        rows = [
            f"{_pointer(index, cursor)} {index + 1}. [{'x' if index in ticked else ' '}] {option}"
            for index, option in enumerate(options)
        ]
        drawing.show([question, *rows, hint])
        key = read()
        hint = PICK_HINT
        if key == CANCEL:
            drawing.erase()
            return None
        if key == ENTER:
            if ticked:
                drawing.erase()
                return sorted(ticked)
            hint = NONE_PICKED_HINT
        elif key == SPACE:
            ticked ^= {cursor}
        elif (numbered := _numbered(key, len(options))) is not None:
            cursor = numbered
            ticked ^= {numbered}
        else:
            cursor = _moved(cursor, key, len(options))


def ask_schedule(read: ReadKey, out: TextIO) -> Schedule | None:
    """How often a to-do is owed, or None when cancelled."""
    options = [label for label, _ in PRESETS]
    pick_days = len(options)
    while True:
        chosen = choose(QUESTION, [*options, PICK_DAYS, *(label for label, _ in ONCE_IN_A_PERIOD)], read, out)
        if chosen is None:
            return None
        if chosen < pick_days:
            out.write(f"{QUESTION} {options[chosen]}\n")
            return Schedule(PRESETS[chosen][1], "day")
        if chosen > pick_days:
            label, period = ONCE_IN_A_PERIOD[chosen - pick_days - 1]
            out.write(f"{QUESTION} {label}\n")
            return Schedule(None, period)
        ticked = choose_many(QUESTION, [weekday.capitalize() for weekday in WEEKDAYS], read, out)
        if ticked is not None:
            weekdays = [WEEKDAYS[index] for index in ticked]
            out.write(f"{QUESTION} {', '.join(weekdays)}\n")
            return Schedule(",".join(weekdays), "day")


def ask_title(check: Callable[[str], str]) -> str | None:
    """A title that `check` accepts, asked again until it is one, or None at the end of input."""
    # Importing it is what gives input() line editing, which wide characters need: the terminal's
    # own editing rubs out half a character on each backspace.
    import readline  # noqa: F401

    while True:
        try:
            typed = input("To-do: ")
        except EOFError:
            print()
            return None
        try:
            return check(typed)
        except ValueError as error:
            print(error)


@contextmanager
def _keypresses(descriptor: int, out: TextIO) -> Iterator[None]:
    """Keys as they are pressed, unechoed, under a hidden cursor; Ctrl-C still interrupts."""
    saved = termios.tcgetattr(descriptor)
    tty.setcbreak(descriptor)
    out.write("\x1b[?25l")
    out.flush()
    try:
        yield
    finally:
        out.write("\x1b[?25h")
        out.flush()
        termios.tcsetattr(descriptor, termios.TCSADRAIN, saved)


def ask_todo(check_title: Callable[[str], str], on: str | None, period: str | None = None) -> tuple[str, Schedule] | None:
    """A to-do's title and schedule from the person at the terminal, or None when they cancel.

    What the command line already said about how often is not asked again.
    """
    title = ask_title(check_title)
    if title is None:
        return None
    if on is not None or period is not None:
        return title, Schedule(on, period or "day")
    descriptor = sys.stdin.fileno()
    with _keypresses(descriptor, sys.stdout):
        schedule = ask_schedule(lambda: read_key(descriptor), sys.stdout)
    return None if schedule is None else (title, schedule)
