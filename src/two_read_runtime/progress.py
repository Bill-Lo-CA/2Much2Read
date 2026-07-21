from __future__ import annotations

import sys
from collections.abc import Callable
from threading import Event, Thread
from time import monotonic
from typing import TypeVar

Result = TypeVar("Result")


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
