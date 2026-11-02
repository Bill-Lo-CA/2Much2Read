from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from shutil import get_terminal_size
from threading import Event, Lock, Thread
from time import monotonic
from typing import TypeVar

from .text import is_inert

Result = TypeVar("Result")
StatusReporter = Callable[[str], None]
logger = logging.getLogger(__name__)


def _one_line(message: str) -> str:
    # Status lines carry Gmail subjects and Hacker News titles, so this is untrusted text on its way
    # to a terminal or a log. keep="" because a status line is one line: a newline here would break
    # out of the scroll region the live display sets up, or forge a line of its own in the journal.
    return "".join(character if is_inert(character, keep="") else " " for character in message)


def _log_status(message: str) -> None:
    """A status line where no terminal shows it: a scheduled run's journal, at LOG_LEVEL=INFO.

    The lines reporting something wrong all start "Warning:" and are logged as warnings, so the
    default level still shows them - an article the headline rewrite could not parse, say. They
    were dropped outright before, along with every other status line, whenever stderr was no TTY.
    """
    logger.log(logging.WARNING if message.startswith("Warning:") else logging.INFO, "%s", _one_line(message))


def _format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, remaining = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {remaining:02d}s"
    if minutes:
        return f"{minutes}m {remaining:02d}s"
    return f"{remaining}s"


def run_with_elapsed(label: str, operation: Callable[[], Result], interval: float = 1.0) -> Result:
    if not sys.stderr.isatty():
        return operation()
    start = monotonic()
    stop = Event()

    def elapsed() -> str:
        return _format_elapsed(monotonic() - start)

    def show(message: str, final: bool = False) -> None:
        print(f"\r\033[K{message}", file=sys.stderr, end="\n" if final else "", flush=True)

    def tick() -> None:
        show(f"{label} elapsed {elapsed()}")
        while not stop.wait(interval):
            show(f"{label} elapsed {elapsed()}")

    thread = Thread(target=tick, daemon=True)
    thread.start()
    outcome = "failed"
    try:
        result = operation()
        outcome = "finished"
        return result
    finally:
        stop.set()
        thread.join(timeout=max(interval, 0.1))
        show(f"{label} {outcome} in {elapsed()}", final=True)


def run_with_live_progress(label: str, operation: Callable[[StatusReporter], Result], interval: float = 1.0) -> Result:
    if not sys.stderr.isatty():
        return operation(_log_status)
    start = monotonic()
    stop = Event()
    output = sys.stderr
    lock = Lock()
    # ponytail: use the initial terminal size; handle SIGWINCH only if resize support is needed.
    rows = max(2, get_terminal_size(fallback=(80, 24)).lines)

    def elapsed() -> str:
        return _format_elapsed(monotonic() - start)

    def timer(outcome: str | None = None) -> str:
        return f"{label} {outcome or 'running'} · elapsed {elapsed()}"

    def refresh_timer(outcome: str | None = None) -> None:
        output.write(f"\033[s\033[1;1H\033[K{timer(outcome)}\033[u")
        output.flush()

    def report(message: str) -> None:
        safe_message = _one_line(message)
        with lock:
            refresh_timer()
            output.write(f"\r\033[K{safe_message}\n")
            output.flush()

    def tick() -> None:
        while not stop.wait(interval):
            with lock:
                refresh_timer()

    with lock:
        output.write(f"\033[2J\033[H{timer()}\033[2;{rows}r\033[2;1H")
        output.flush()
    thread = Thread(target=tick, daemon=True)
    thread.start()
    outcome = "failed"
    try:
        result = operation(report)
        outcome = "finished"
        return result
    finally:
        stop.set()
        thread.join(timeout=max(interval, 0.1))
        with lock:
            final_status = f"{outcome} in {elapsed()}"
            refresh_timer(final_status)
            output.write(f"\r\033[K{label} {final_status}\n\033[r")
            output.flush()
