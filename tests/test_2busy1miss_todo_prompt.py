from __future__ import annotations

import io
import os
import pty
import select
import signal
import sqlite3
import subprocess
import sys
import termios
import time
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path

import pytest

from two_busy_one_miss import todo_prompt
from two_busy_one_miss.todo_prompt import (
    CANCEL,
    DOWN,
    ENTER,
    EVERY_DAY,
    NONE_PICKED_HINT,
    PICK_HINT,
    SPACE,
    UP,
    ask_title,
    ask_todo,
    ask_weekdays,
    choose,
    choose_many,
    read_key,
)

OPTIONS = ["one", "two", "three"]


def keys(*pressed: str) -> Iterator[str]:
    return iter(pressed)


def run_choose(*pressed: str) -> tuple[int | None, str]:
    out = io.StringIO()
    feed = keys(*pressed)
    return choose("Which?", OPTIONS, lambda: next(feed), out), out.getvalue()


def run_choose_many(*pressed: str) -> tuple[list[int] | None, str]:
    out = io.StringIO()
    feed = keys(*pressed)
    return choose_many("Which?", OPTIONS, lambda: next(feed), out), out.getvalue()


def run_weekdays(*pressed: str) -> tuple[str | None, str]:
    out = io.StringIO()
    feed = keys(*pressed)
    return ask_weekdays(lambda: next(feed), out), out.getvalue()


@pytest.mark.parametrize(
    ("pressed", "chosen"),
    [
        ([ENTER], 0),
        ([DOWN, ENTER], 1),
        ([DOWN, DOWN, ENTER], 2),
        ([DOWN, DOWN, DOWN, ENTER], 0),
        ([UP, ENTER], 2),
        ([DOWN, UP, ENTER], 0),
        (["2"], 1),
        (["3"], 2),
        (["0", "4", "x", "", ENTER], 0),
        ([SPACE, ENTER], 0),
        ([CANCEL], None),
        ([DOWN, CANCEL], None),
    ],
)
def test_choose(pressed: list[str], chosen: int | None) -> None:
    assert run_choose(*pressed)[0] == chosen


def test_choose_draws_the_pointer_where_the_keys_moved_it() -> None:
    _, output = run_choose(DOWN, ENTER)

    first, second = output.split("\x1b[5A\r\x1b[J")[:2]
    assert first == f"Which?\n❯ 1. one\n  2. two\n  3. three\n{todo_prompt.CHOOSE_HINT}\n"
    assert second == f"Which?\n  1. one\n❯ 2. two\n  3. three\n{todo_prompt.CHOOSE_HINT}\n"


def test_choose_leaves_nothing_of_itself_on_the_screen() -> None:
    assert run_choose(ENTER)[1].endswith("\x1b[5A\r\x1b[J")
    assert run_choose(CANCEL)[1].endswith("\x1b[5A\r\x1b[J")


@pytest.mark.parametrize(
    ("pressed", "ticked"),
    [
        ([SPACE, ENTER], [0]),
        ([DOWN, SPACE, ENTER], [1]),
        ([DOWN, DOWN, SPACE, UP, UP, SPACE, ENTER], [0, 2]),
        ([SPACE, SPACE, DOWN, SPACE, ENTER], [1]),
        ([UP, SPACE, ENTER], [2]),
        (["3", "1", ENTER], [0, 2]),
        (["2", "2", SPACE, ENTER], [1]),
        ([SPACE, CANCEL], None),
        ([CANCEL], None),
    ],
)
def test_choose_many(pressed: list[str], ticked: list[int] | None) -> None:
    assert run_choose_many(*pressed)[0] == ticked


def test_choose_many_will_not_confirm_nothing() -> None:
    ticked, output = run_choose_many(ENTER, ENTER, SPACE, ENTER)

    assert ticked == [0]
    assert output.count(NONE_PICKED_HINT) == 2


def test_choose_many_drops_its_hint_at_the_next_key() -> None:
    _, output = run_choose_many(ENTER, SPACE, ENTER)

    assert output.count(NONE_PICKED_HINT) == 1
    assert output.split("\x1b[5A\r\x1b[J")[-2].endswith("Esc to go back\n")


def test_choose_many_shows_what_is_ticked() -> None:
    _, output = run_choose_many(DOWN, SPACE, ENTER)

    frames = output.split("\x1b[5A\r\x1b[J")
    assert frames[0] == f"Which?\n❯ 1. [ ] one\n  2. [ ] two\n  3. [ ] three\n{PICK_HINT}\n"
    assert frames[1] == f"Which?\n  1. [ ] one\n❯ 2. [ ] two\n  3. [ ] three\n{PICK_HINT}\n"
    assert frames[2] == f"Which?\n  1. [ ] one\n❯ 2. [x] two\n  3. [ ] three\n{PICK_HINT}\n"


def test_what_has_been_erased_is_not_erased_again() -> None:
    out = io.StringIO()
    drawing = todo_prompt._Drawing(out)

    drawing.erase()
    assert out.getvalue() == ""

    drawing.show(["a", "b"])
    drawing.erase()
    drawing.erase()
    assert out.getvalue() == "a\nb\n\x1b[2A\r\x1b[J"


@pytest.mark.parametrize(
    ("pressed", "weekdays"),
    [
        ([ENTER], EVERY_DAY),
        ([DOWN, ENTER], "mon,tue,wed,thu,fri"),
        ([DOWN, DOWN, ENTER], "sat,sun"),
        (["3"], "sat,sun"),
        (["4", "5", "1", "3", ENTER], "mon,wed,fri"),
        ([DOWN, DOWN, DOWN, ENTER, UP, SPACE, ENTER], "sun"),
        ([CANCEL], None),
    ],
)
def test_ask_weekdays(pressed: list[str], weekdays: str | None) -> None:
    assert run_weekdays(*pressed)[0] == weekdays


def test_backing_out_of_the_day_list_returns_to_the_presets() -> None:
    assert run_weekdays("4", SPACE, CANCEL, "2")[0] == "mon,tue,wed,thu,fri"
    assert run_weekdays("4", CANCEL, CANCEL)[0] is None


def test_both_lists_ask_which_days() -> None:
    _, output = run_weekdays("4", "1", ENTER)

    frames = output.split("\x1b[6A\r\x1b[J")
    assert frames[0].startswith("Which days?\n❯ 1. Every day\n")
    assert frames[1].startswith("Which days?\n❯ 1. [ ] Mon\n")


def test_the_answer_stays_on_the_screen_as_one_line() -> None:
    assert run_weekdays("2")[1].endswith("Which days? Weekdays (mon-fri)\n")
    assert run_weekdays("4", "1", "3", ENTER)[1].endswith("Which days? mon, wed\n")


@pytest.mark.parametrize(
    ("written", "key"),
    [
        (b"\x1b[A", UP),
        (b"\x1b[B", DOWN),
        (b"\x1bOA", UP),
        (b"\x1bOB", DOWN),
        (b"\x1b[1;2A", UP),
        (b"\x1b[1;2B", DOWN),
        (b"\x1b[12;3A", UP),
        (b"\x1b[@", ""),
        (b"k", UP),
        (b"j", DOWN),
        (b"\r", ENTER),
        (b"\n", ENTER),
        (b" ", SPACE),
        (b"q", CANCEL),
        (b"\x04", CANCEL),
        (b"\x1b", CANCEL),
        (b"\x1b[C", ""),
        (b"\x1b[3~", ""),
        (b"\x1bx", ""),
        (b"\xe4", ""),
        (b"3", "3"),
    ],
)
def test_read_key(written: bytes, key: str) -> None:
    read_end, write_end = os.pipe()
    try:
        os.write(write_end, written)
        assert read_key(read_end) == key
    finally:
        os.close(read_end)
        os.close(write_end)


def test_a_terminal_that_has_gone_cancels_rather_than_waiting_forever() -> None:
    read_end, write_end = os.pipe()
    os.close(write_end)
    try:
        assert read_key(read_end) == CANCEL
    finally:
        os.close(read_end)


def answering(monkeypatch: pytest.MonkeyPatch, *typed: str | type[BaseException]) -> list[str]:
    """What input() hands back in turn, and the prompts it was given."""
    prompts: list[str] = []
    feed = iter(typed)

    def fake_input(prompt: str) -> str:
        prompts.append(prompt)
        answer = next(feed)
        if isinstance(answer, str):
            return answer
        raise answer

    monkeypatch.setattr("builtins.input", fake_input)
    return prompts


def refuse_blank(title: str) -> str:
    if not title.strip():
        raise ValueError("blank")
    return title.strip()


def test_ask_title_returns_what_the_check_makes_of_it(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts = answering(monkeypatch, "  喝水 ")

    assert ask_title(refuse_blank) == "喝水"
    assert prompts == ["To-do: "]


def test_ask_title_says_why_and_asks_again(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    prompts = answering(monkeypatch, " ", "喝水")

    assert ask_title(refuse_blank) == "喝水"
    assert prompts == ["To-do: ", "To-do: "]
    assert capsys.readouterr().out == "blank\n"


def test_ask_title_ends_at_the_end_of_input(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    answering(monkeypatch, EOFError)

    assert ask_title(refuse_blank) is None
    assert capsys.readouterr().out == "\n"


def test_ask_todo_stops_when_the_title_is_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    answering(monkeypatch, EOFError)

    assert ask_todo(refuse_blank, None) is None


def test_ask_todo_needs_no_list_when_the_days_are_given(monkeypatch: pytest.MonkeyPatch) -> None:
    answering(monkeypatch, "喝水")

    assert ask_todo(refuse_blank, "mon") == ("喝水", "mon")


class Terminal:
    """The real command on a real pseudo-terminal, typed at."""

    def __init__(self, tmp_path: Path, *arguments: str) -> None:
        self.database = tmp_path / "reminders.sqlite3"
        reminders = tmp_path / "reminders.yaml"
        reminders.write_text("timezone: America/Toronto\ncalendars:\n  - id: primary\n", encoding="utf-8")
        self.master, self.slave = pty.openpty()
        self.before = termios.tcgetattr(self.slave)
        self.output = b""
        self.process = subprocess.Popen(
            [sys.executable, "-c", "from two_busy_one_miss.cli import app; app()", "todo", "add", *arguments],
            stdin=self.slave,
            stdout=self.slave,
            stderr=self.slave,
            start_new_session=True,
            env={
                "HOME": str(tmp_path),
                "LANG": "C.UTF-8",
                "PATH": os.environ["PATH"],
                "TERM": "xterm-256color",
                "REMINDERS_CONFIG_PATH": str(reminders),
                "DATABASE_PATH": str(self.database),
            },
        )

    def expect(self, text: str, *, times: int = 1, timeout: float = 30) -> None:
        wanted = text.encode()
        deadline = time.monotonic() + timeout
        while self.output.count(wanted) < times:
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"never saw {text!r} in {self.output!r}"
            if select.select([self.master], [], [], remaining)[0]:
                self.output += os.read(self.master, 4096)

    def type(self, text: str | bytes) -> None:
        os.write(self.master, text.encode() if isinstance(text, str) else text)

    def finish(self) -> int:
        code = self.process.wait(timeout=30)
        while select.select([self.master], [], [], 0.1)[0]:
            try:
                data = os.read(self.master, 4096)
            except OSError:
                break
            if not data:
                break
            self.output += data
        return code

    def titles(self) -> list[tuple[str, str]]:
        if not self.database.exists():
            return []
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute("SELECT title, weekdays FROM todos").fetchall()

    def assert_left_as_found(self) -> None:
        assert termios.tcgetattr(self.slave) == self.before
        hidden = self.output.count(b"\x1b[?25l")
        assert self.output.count(b"\x1b[?25h") == hidden
        if hidden:
            assert self.output.rindex(b"\x1b[?25h") > self.output.rindex(b"\x1b[?25l")

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait()
        os.close(self.master)
        os.close(self.slave)


@pytest.fixture
def open_terminal(tmp_path: Path) -> Iterator[Callable[..., Terminal]]:
    opened: list[Terminal] = []

    def start(*arguments: str) -> Terminal:
        terminal = Terminal(tmp_path, *arguments)
        opened.append(terminal)
        return terminal

    yield start
    for terminal in opened:
        terminal.close()


def test_a_to_do_added_by_arrow_keys(open_terminal: Callable[..., Terminal]) -> None:
    terminal = open_terminal()
    terminal.expect("To-do: ")
    terminal.type("三十下伏地挺身\r")
    terminal.expect("Which days?")
    terminal.type(b"\x1b[B\x1b[B\x1b[B\r")
    terminal.expect("[ ] Mon")
    terminal.type(b" \x1b[B\x1b[B ")
    terminal.expect("[x] Wed")
    terminal.type(b"\r")

    assert terminal.finish() == 0
    assert "Added to-do 1: 三十下伏地挺身 (mon, wed)" in terminal.output.decode()
    assert terminal.titles() == [("三十下伏地挺身", "mon,wed")]
    terminal.assert_left_as_found()


def test_a_to_do_added_for_every_day_in_two_keys(open_terminal: Callable[..., Terminal]) -> None:
    terminal = open_terminal()
    terminal.expect("To-do: ")
    terminal.type("喝水\r")
    terminal.expect("Which days?")
    terminal.type(b"\r")

    assert terminal.finish() == 0
    assert "Added to-do 1: 喝水 (every day)" in terminal.output.decode()
    assert terminal.titles() == [("喝水", EVERY_DAY)]


def test_backspace_takes_back_a_whole_wide_character(open_terminal: Callable[..., Terminal]) -> None:
    terminal = open_terminal()
    terminal.expect("To-do: ")
    terminal.type("三十\x7f\r")
    terminal.expect("Which days?")
    terminal.type(b"\r")

    assert terminal.finish() == 0
    assert terminal.titles() == [("三", EVERY_DAY)]


def test_a_title_with_nothing_to_show_is_asked_for_again(open_terminal: Callable[..., Terminal]) -> None:
    terminal = open_terminal()
    terminal.expect("To-do: ")
    terminal.type("   \r")
    terminal.expect("something to show")
    terminal.expect("To-do: ", times=2)
    terminal.type("喝水\r")
    terminal.expect("Which days?")
    terminal.type(b"\r")

    assert terminal.finish() == 0
    assert terminal.titles() == [("喝水", EVERY_DAY)]


def test_on_answers_the_question_about_days(open_terminal: Callable[..., Terminal]) -> None:
    terminal = open_terminal("--on", "fri,mon")
    terminal.expect("To-do: ")
    terminal.type("跑步\r")

    assert terminal.finish() == 0
    assert b"Which days?" not in terminal.output
    assert terminal.titles() == [("跑步", "mon,fri")]


def test_an_unknown_day_is_refused_before_anything_is_asked(open_terminal: Callable[..., Terminal]) -> None:
    terminal = open_terminal("--on", "funday")

    assert terminal.finish() == 2
    assert b"To-do:" not in terminal.output
    assert terminal.titles() == []


def test_escape_cancels_and_leaves_the_terminal_as_found(open_terminal: Callable[..., Terminal]) -> None:
    terminal = open_terminal()
    terminal.expect("To-do: ")
    terminal.type("喝水\r")
    terminal.expect("Which days?")
    terminal.type(b"\x1b")

    assert terminal.finish() == 1
    assert b"nothing was added" in terminal.output
    assert terminal.titles() == []
    terminal.assert_left_as_found()


def test_ctrl_c_in_the_list_cancels_and_leaves_the_terminal_as_found(open_terminal: Callable[..., Terminal]) -> None:
    terminal = open_terminal()
    terminal.expect("To-do: ")
    terminal.type("喝水\r")
    terminal.expect("Which days?")
    os.killpg(terminal.process.pid, signal.SIGINT)

    assert terminal.finish() == 1
    assert b"nothing was added" in terminal.output
    assert terminal.titles() == []
    terminal.assert_left_as_found()


def test_ctrl_c_at_the_title_cancels(open_terminal: Callable[..., Terminal]) -> None:
    terminal = open_terminal()
    terminal.expect("To-do: ")
    os.killpg(terminal.process.pid, signal.SIGINT)

    assert terminal.finish() == 1
    assert b"nothing was added" in terminal.output
    assert terminal.titles() == []
    terminal.assert_left_as_found()


def test_ctrl_d_at_the_title_cancels(open_terminal: Callable[..., Terminal]) -> None:
    terminal = open_terminal()
    terminal.expect("To-do: ")
    terminal.type(b"\x04")

    assert terminal.finish() == 1
    assert b"nothing was added" in terminal.output
    assert terminal.titles() == []
    terminal.assert_left_as_found()
