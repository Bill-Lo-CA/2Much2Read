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
COPY_ATTEMPTS = 3


def _stamp(path: Path) -> tuple[int, int] | None:
    try:
        status = path.stat()
    except OSError:
        return None
    return status.st_size, status.st_mtime_ns


def _stamps(path: Path) -> tuple[tuple[int, int] | None, ...]:
    return tuple(_stamp(Path(f"{path}{suffix}")) for suffix in COPIED)


@contextmanager
def reading_connection(path: Path) -> Iterator[sqlite3.Connection | None]:
    """Open a database for reporting only, or yield None when there is nothing to read yet.

    A dry run has to be possible whatever else is happening, and has to leave the data directory
    exactly as it found it. A read-only connection to the live file gives neither, in two different
    ways. Where the -wal and -shm sidecars are missing, SQLite creates them, because a database in
    WAL mode cannot be read without them. Where they are present, nothing is created - but running a
    query attaches the reader to the live WAL index and updates its marks inside -shm, leaving the
    file the same size with the same mtime and different contents. That is the shape of change a
    directory listing cannot see, so the reader never touches the live database at all.

    It reads a private copy instead. The write-ahead log is copied with it when there is one, since
    it can hold committed rows that the main file does not - after an unclean exit, up to and
    including the statements that created the tables. The -shm index is deliberately not copied: it
    describes the live database's readers and writers, and SQLite rebuilds it beside the copy.

    Copying takes no lock. A reader that waited for the writer would be a dry run that cannot run
    during the thing it exists to describe, and a reader that took the lock for itself would create
    the lock file when it was missing. It does not need one: a writer appends to the log rather than
    rewriting the file being copied, and the copy is re-taken anyway if the source moves underneath
    it.
    """
    if not path.exists():
        yield None
        return
    with ExitStack() as stack:
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
