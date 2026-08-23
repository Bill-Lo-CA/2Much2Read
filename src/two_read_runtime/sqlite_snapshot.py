from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

# -shm is an index SQLite rebuilds from -wal, but -wal holds committed rows - including, after an
# unclean exit, the statements that created the tables - so a copy that leaves it behind is not the
# same database.
COPIED = ("", "-wal")
LIVE_SIDECARS = ("-wal", "-shm")
COPY_ATTEMPTS = 3


def _stamp(path: Path) -> tuple[int, int] | None:
    try:
        status = path.stat()
    except OSError:
        return None
    return status.st_size, status.st_mtime_ns


def _stamps(path: Path) -> tuple[tuple[int, int] | None, ...]:
    return tuple(_stamp(Path(f"{path}{suffix}")) for suffix in COPIED)


def _readable_in_place(path: Path) -> bool:
    return all(Path(f"{path}{suffix}").exists() for suffix in LIVE_SIDECARS)


@contextmanager
def reading_connection(path: Path) -> Iterator[sqlite3.Connection | None]:
    """Open a database for reporting only, or yield None when there is nothing to read yet.

    A dry run has to be possible whatever else is happening, and has to leave the data directory
    exactly as it found it. Neither falls out of a plain read-only connection, so which of the two
    ways in is safe depends on what is already on disk.

    A database in WAL mode needs its -wal and -shm sidecars to be read, and SQLite creates them if
    they are missing - a mode=ro connection included. Where both already exist, opening one changes
    no file at all and never waits for the writer, because concurrent reading is what WAL is for.

    Otherwise the database is read from a private copy. The write-ahead log is copied with it when
    there is one: it can hold committed rows that the main file does not, and SQLite rebuilds the
    -shm index beside the copy. No writer is running when the -shm is absent, and one that starts
    appends to a new log rather than rewriting the file being copied; the copy is still re-taken if
    the source moves underneath it.

    Neither path takes the lock. A reader that waited for the writer would be a dry run that cannot
    run during the thing it exists to describe, and a reader that took the lock for itself would
    create the lock file when it was missing.
    """
    if not path.exists():
        yield None
        return
    with ExitStack() as stack:
        if _readable_in_place(path):
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        else:
            directory = Path(stack.enter_context(TemporaryDirectory()))
            connection = sqlite3.connect(_copied(path, directory / path.name))
        # A connection is its own transaction context manager, not a closing one, so closing it is
        # registered here; the stack unwinds it before the directory holding the copy goes away.
        stack.callback(connection.close)
        connection.row_factory = sqlite3.Row
        yield connection


def _copied(path: Path, destination: Path) -> Path:
    for attempt in range(COPY_ATTEMPTS):
        before = _stamps(path)
        for suffix in COPIED:
            source = Path(f"{path}{suffix}")
            if source.exists():
                shutil.copy2(source, f"{destination}{suffix}")
        if _stamps(path) == before or attempt == COPY_ATTEMPTS - 1:
            break
    return destination
