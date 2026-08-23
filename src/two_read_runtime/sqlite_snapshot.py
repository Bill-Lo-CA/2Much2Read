from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

SIDECARS = ("-wal", "-shm")
COPY_ATTEMPTS = 3


def _stamp(path: Path) -> tuple[int, int] | None:
    try:
        status = path.stat()
    except OSError:
        return None
    return status.st_size, status.st_mtime_ns


def _sidecars_present(path: Path) -> bool:
    return all(Path(f"{path}{suffix}").exists() for suffix in SIDECARS)


@contextmanager
def reading_connection(path: Path) -> Iterator[sqlite3.Connection | None]:
    """Open a database for reporting only, or yield None when there is nothing to read yet.

    A dry run has to be possible whatever else is happening, and has to leave the data directory
    exactly as it found it. Neither falls out of a plain read-only connection, so which of the two
    ways in is safe depends on what is already on disk:

    A database in WAL mode needs its -wal and -shm sidecars to be read, and SQLite creates them if
    they are missing - a mode=ro connection included. Where they already exist, opening one changes
    no file at all and never waits for the writer, because concurrent reading is what WAL is for.

    Where they are missing, the database was closed cleanly and holds everything that was written
    to it, so it is read from a copy instead. No writer is running at that moment, and one that
    starts appends to a new write-ahead log rather than touching the file being copied; the copy is
    still re-taken if the source moves underneath it.

    Neither path takes the lock. A reader that waited for the writer would be a dry run that cannot
    run during the thing it exists to describe, and a reader that took the lock for itself would
    create the lock file when it was missing.
    """
    if not path.exists():
        yield None
        return
    with ExitStack() as stack:
        if _sidecars_present(path):
            connection = stack.enter_context(sqlite3.connect(f"file:{path}?mode=ro", uri=True))
            connection.row_factory = sqlite3.Row
            yield connection
            return
        directory = Path(stack.enter_context(TemporaryDirectory()))
        copy = directory / path.name
        for attempt in range(COPY_ATTEMPTS):
            before = _stamp(path)
            shutil.copy2(path, copy)
            if _stamp(path) == before or attempt == COPY_ATTEMPTS - 1:
                break
        connection = stack.enter_context(sqlite3.connect(copy))
        connection.row_factory = sqlite3.Row
        yield connection
