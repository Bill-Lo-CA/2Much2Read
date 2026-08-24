from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

# -shm is an index SQLite rebuilds from -wal, but -wal holds committed rows - including, after an
# unclean exit, the statements that created the tables - so a copy that leaves it behind is not the
# same database. -journal is left out too: every database here sets journal_mode=WAL on open, so a
# rollback journal only survives from a version that predates that, and copying one would need the
# rollback replayed rather than the log.
COPIED = ("", "-wal")
COPY_ATTEMPTS = 3


class SnapshotError(ValueError):
    """The database would not hold still long enough to be copied whole."""


def _stamp(path: Path) -> tuple[int, int] | None:
    """Enough of a file to tell whether it moved: its size and its modification time.

    This is a change detector, not a content check - the same measurement that proved blind to a
    byte rewritten in place inside -shm, which keeps its name, size and mtime. It is trustworthy
    here for a different reason: the timestamp is nanosecond-resolution, and the writer these
    stamps are watching appends to the log rather than editing what was already copied.
    """
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
        connection = sqlite3.connect(_copied(path, directory))
        # A connection is its own transaction context manager, not a closing one, so closing it is
        # registered here; the stack unwinds it before the directory holding the copy goes away.
        stack.callback(connection.close)
        connection.row_factory = sqlite3.Row
        yield connection


def _copy_once(path: Path, destination: Path) -> bool:
    """Copy the database and its log, reporting whether the source held still throughout.

    Only what was there when the stamps were taken is copied, so a log that has since been
    checkpointed away is not silently skipped: it disappeared mid-copy, which means the source moved
    and this attempt is worthless, not that there was never a log.
    """
    before = _stamps(path)
    for suffix, stamp in zip(COPIED, before, strict=True):
        if stamp is None:
            continue
        try:
            shutil.copy2(f"{path}{suffix}", f"{destination}{suffix}")
        except FileNotFoundError:
            return False
    return _stamps(path) == before


def _copied(path: Path, directory: Path) -> Path:
    """A copy of the database that is whole, or an error.

    Every attempt gets its own directory. Reusing one would leave the previous attempt's write-ahead
    log in place when the next attempt finds none to copy, and replaying a stale log over a database
    that has since been checkpointed silently rolls rows back - a wrong answer with nothing to
    signal it. The last attempt gets no exemption either, for the same reason: a report built from a
    copy known to be inconsistent is worse than one that says it could not be taken.
    """
    for attempt in range(COPY_ATTEMPTS):
        destination = directory / str(attempt) / path.name
        destination.parent.mkdir()
        if _copy_once(path, destination):
            return destination
    raise SnapshotError(f"{path} kept changing while it was being copied; try again in a moment")
