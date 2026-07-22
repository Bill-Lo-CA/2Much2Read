from __future__ import annotations

import sys
from collections.abc import Callable
from shutil import get_terminal_size
from threading import Event, Lock, Thread
from time import monotonic
from typing import TypeVar

Result = TypeVar("Result")
StatusReporter = Callable[[str], None]


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
        return operation(lambda _: None)
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
        safe_message = "".join(" " if ord(character) < 32 or ord(character) == 127 else character for character in message)
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
            refresh_timer(f"{outcome} in {elapsed()}")
            output.write("\033[r")
            output.flush()
