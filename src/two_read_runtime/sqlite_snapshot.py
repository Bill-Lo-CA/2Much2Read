from __future__ import annotations

import shutil
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from .locking import ProcessLock

SQLITE_SIDECARS = ("", "-wal", "-shm")


@contextmanager
def snapshot_path(path: Path, lock_path: Path) -> Iterator[Path | None]:
    """Yield a private copy of a SQLite database, or None when there is nothing to read yet.

    A command that only reports cannot read the live file. SQLite has to create the -wal and -shm
    sidecars to read a database in WAL mode, and opening it with mode=ro is no exception: reading a
    cleanly closed database puts both files back next to it. That is a change to the data
    directory, which is exactly what --dry-run and status exist not to make.

    The copy is taken while holding the lock the writer holds. A database and its write-ahead log
    copied while a write is in flight are not a consistent pair, and a report built from a moving
    database is worse than one that says it could not be taken, so a contended lock is raised
    rather than worked around.
    """
    if not path.exists():
        yield None
        return
    with ExitStack() as stack:
        # Taking the lock creates the lock file when it is missing, which would itself be the kind
        # of change this function exists to avoid. A missing lock file also means no writer has
        # ever run against this database - a writer takes the lock before it creates anything - so
        # there is nothing to coordinate with and nothing to create.
        if lock_path.exists():
            try:
                stack.enter_context(ProcessLock(lock_path))
            except RuntimeError:
                raise ValueError("another run is writing to this database right now; try again in a moment") from None
        directory = stack.enter_context(TemporaryDirectory())
        copy = Path(directory) / path.name
        for suffix in SQLITE_SIDECARS:
            source = Path(f"{path}{suffix}")
            if source.exists():
                shutil.copy2(source, f"{copy}{suffix}")
        yield copy
