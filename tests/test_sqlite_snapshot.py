"""Reading a SQLite database without changing it.

A dry run has to be possible whatever else is happening, and has to leave the data directory exactly
as it found it. A read-only connection to the live file gives neither: where the -wal and -shm
sidecars are missing SQLite creates them, and where they are present a query updates the reader
marks inside -shm without changing its name, size, or mtime.
"""

import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import directory_digest as listing

from two_read_runtime.sqlite_snapshot import SnapshotError, reading_connection


def written(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE t(x)")
    connection.execute("INSERT INTO t VALUES(1)")
    connection.commit()
    connection.close()


def source_file(connection: sqlite3.Connection) -> str:
    return str(connection.execute("PRAGMA database_list").fetchone()[2])


def test_nothing_to_read_yields_nothing(tmp_path: Path) -> None:
    with reading_connection(tmp_path / "absent.sqlite3", ("t",)) as connection:
        assert connection is None


def test_a_cleanly_closed_database_is_read_from_a_copy(tmp_path: Path) -> None:
    # Reading it in place would put the -wal and -shm files back beside it.
    path = tmp_path / "db.sqlite3"
    written(path)
    before = listing(tmp_path)

    with reading_connection(path, ("t",)) as connection:
        assert connection is not None
        assert connection.execute("SELECT count(*) FROM t").fetchone()[0] == 1
        assert source_file(connection) != str(path)

    assert listing(tmp_path) == before


def test_a_database_with_a_live_writer_is_still_read_from_a_copy(tmp_path: Path) -> None:
    # Opening the live file would create nothing here, because the sidecars already exist - but a
    # query through it updates the reader marks inside -shm, which is a change to the data
    # directory even though the file keeps its size and its mtime.
    path = tmp_path / "db.sqlite3"
    written(path)
    writer = sqlite3.connect(path)
    writer.execute("INSERT INTO t VALUES(2)")
    writer.commit()
    try:
        before = listing(tmp_path)

        with reading_connection(path, ("t",)) as connection:
            assert connection is not None
            # The second row is committed but still only in the write-ahead log.
            assert connection.execute("SELECT count(*) FROM t").fetchone()[0] == 2
            assert source_file(connection) != str(path)

        assert listing(tmp_path) == before
    finally:
        writer.close()


def test_rows_come_back_as_mappings(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite3"
    written(path)

    with reading_connection(path, ("t",)) as connection:
        assert connection is not None
        assert connection.execute("SELECT x FROM t").fetchone()["x"] == 1


def test_a_write_during_the_copy_is_retaken(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The copy is only worth taking if it is not a half-written file, so a source that moved while
    # it was being read is copied again rather than reported on.
    path = tmp_path / "db.sqlite3"
    written(path)
    copies: list[int] = []
    real_copy = shutil.copy2

    def copy_then_touch(source, destination, *args, **kwargs):  # type: ignore[no-untyped-def]
        result = real_copy(source, destination, *args, **kwargs)
        copies.append(len(copies))
        if len(copies) == 1:
            connection = sqlite3.connect(path)
            connection.execute("INSERT INTO t VALUES(3)")
            connection.commit()
            connection.close()
        return result

    monkeypatch.setattr("two_read_runtime.sqlite_snapshot.shutil.copy2", copy_then_touch)

    with reading_connection(path, ("t",)) as connection:
        assert connection is not None
        assert connection.execute("SELECT count(*) FROM t").fetchone()[0] == 2

    assert len(copies) == 2


def crashed(path: Path) -> None:
    """Leave a database whose committed rows, and its schema, are still only in the -wal file."""
    statements = (
        "import os, sqlite3;"
        f"c = sqlite3.connect({str(path)!r});"
        "c.execute('PRAGMA journal_mode=WAL');"
        "c.execute('CREATE TABLE t(x)');"
        "c.execute('INSERT INTO t VALUES(1)');c.commit();"
        "c.execute('INSERT INTO t VALUES(2)');c.commit();"
        "os._exit(0)"
    )
    subprocess.run([sys.executable, "-c", statements], check=True)


def test_a_database_whose_rows_are_only_in_the_wal_is_read_in_full(tmp_path: Path) -> None:
    # -shm is an index SQLite rebuilds, so its absence must not mean the write-ahead log is left
    # behind: after an unclean exit the log holds the rows, and the CREATE TABLE that made them.
    path = tmp_path / "db.sqlite3"
    crashed(path)
    Path(f"{path}-shm").unlink()
    before = listing(tmp_path)

    with reading_connection(path, ("t",)) as connection:
        assert connection is not None
        assert connection.execute("SELECT count(*) FROM t").fetchone()[0] == 2
        assert source_file(connection) != str(path)

    assert listing(tmp_path) == before


def test_a_lone_wal_is_not_left_behind_by_the_copy(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite3"
    crashed(path)
    Path(f"{path}-shm").unlink()

    with reading_connection(path, ("t",)) as connection:
        assert connection is not None
        copy = Path(source_file(connection))
        assert Path(f"{copy}-wal").exists()


def test_the_connection_is_closed_when_the_context_ends(tmp_path: Path) -> None:
    # sqlite3.Connection is a transaction context manager, not a closing one, so handing it to an
    # ExitStack does not close it; the copy's directory would then outlive nothing.
    path = tmp_path / "db.sqlite3"
    written(path)

    with reading_connection(path, ("t",)) as connection:
        assert connection is not None
        opened = connection

    with pytest.raises(sqlite3.ProgrammingError):
        opened.execute("SELECT 1")


def test_reading_beside_a_committing_writer_moves_no_bytes(tmp_path: Path) -> None:
    # Separate from the assertion about which file is opened: this one only weighs the bytes, and
    # is the check that fails if a reader ever attaches to the live WAL index again. The reader
    # marks it would move live inside -shm, at the same size and the same mtime.
    path = tmp_path / "db.sqlite3"
    written(path)
    writer = sqlite3.connect(path)
    writer.execute("INSERT INTO t VALUES(2)")
    writer.commit()
    try:
        before = listing(tmp_path)

        with reading_connection(path, ("t",)) as connection:
            assert connection is not None
            connection.execute("SELECT count(*) FROM t").fetchone()

        assert listing(tmp_path) == before
    finally:
        writer.close()


def checkpointed_away(path: Path) -> None:
    """Fold the log into the database and remove it, the way a checkpoint does."""
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    connection.close()
    assert not Path(f"{path}-wal").exists()


def test_a_log_checkpointed_away_between_attempts_is_not_mixed_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The first attempt copies a database whose rows are still in the log. The source then gains a
    # row and is checkpointed, so the second attempt finds no log to copy. Reusing one destination
    # would leave the first attempt's log beside the second attempt's newer database, and replaying
    # it rolls the newer rows back with nothing to signal it.
    path = tmp_path / "db.sqlite3"
    crashed(path)
    Path(f"{path}-shm").unlink()
    real_copy = shutil.copy2
    moved: list[int] = []

    def advance_the_source_after_the_first_log_copy(source, destination, *args, **kwargs):  # type: ignore[no-untyped-def]
        result = real_copy(source, destination, *args, **kwargs)
        if str(source).endswith("-wal") and not moved:
            moved.append(1)
            connection = sqlite3.connect(path)
            connection.execute("INSERT INTO t VALUES(3)")
            connection.commit()
            connection.close()
            checkpointed_away(path)
        return result

    monkeypatch.setattr("two_read_runtime.sqlite_snapshot.shutil.copy2", advance_the_source_after_the_first_log_copy)

    with reading_connection(path, ("t",)) as connection:
        assert connection is not None
        assert connection.execute("SELECT count(*) FROM t").fetchone()[0] == 3
    assert moved == [1], "the source should have moved underneath the first attempt"


def test_a_log_that_vanishes_mid_copy_is_retried_not_raised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # shutil.copy2 raises FileNotFoundError if the log went away between the stat and the read. That
    # is the source moving, not an error the reporting command should die of; the retry finds the
    # rows in the main file, where the checkpoint put them.
    path = tmp_path / "db.sqlite3"
    crashed(path)
    Path(f"{path}-shm").unlink()
    real_copy = shutil.copy2
    removed: list[int] = []

    def checkpoint_before_copying_the_log(source, destination, *args, **kwargs):  # type: ignore[no-untyped-def]
        if str(source).endswith("-wal") and not removed:
            removed.append(1)
            checkpointed_away(path)
        return real_copy(source, destination, *args, **kwargs)

    monkeypatch.setattr("two_read_runtime.sqlite_snapshot.shutil.copy2", checkpoint_before_copying_the_log)

    with reading_connection(path, ("t",)) as connection:
        assert connection is not None
        assert connection.execute("SELECT count(*) FROM t").fetchone()[0] == 2
    assert removed == [1], "the log should have gone away underneath the first attempt"


def test_a_source_that_never_settles_is_an_error_not_a_wrong_answer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "db.sqlite3"
    written(path)
    real_copy = shutil.copy2

    def write_after_every_copy(source, destination, *args, **kwargs):  # type: ignore[no-untyped-def]
        result = real_copy(source, destination, *args, **kwargs)
        connection = sqlite3.connect(path)
        connection.execute("INSERT INTO t VALUES(9)")
        connection.commit()
        connection.close()
        return result

    monkeypatch.setattr("two_read_runtime.sqlite_snapshot.shutil.copy2", write_after_every_copy)

    with pytest.raises(SnapshotError, match="kept changing"), reading_connection(path, ("t",)):
        pass


def test_a_database_created_but_not_yet_populated_reads_as_empty(tmp_path: Path) -> None:
    # prepare_private_file opens the database with O_CREAT|O_EXCL and the schema is written only
    # afterwards, so a reader can find a real, stable, zero-byte file - during that window, or for
    # good if a first run died between the two. Querying it raised sqlite3.OperationalError, which
    # is not a ValueError and so reached the operator as a traceback rather than an empty report.
    from two_read_runtime.permissions import prepare_private_file

    path = tmp_path / "db.sqlite3"
    prepare_private_file(path)
    assert path.stat().st_size == 0, "the reproduction depends on the file existing and being empty"

    with reading_connection(path, ("t",)) as connection:
        assert connection is None


def test_a_half_created_schema_reads_as_empty(tmp_path: Path) -> None:
    # executescript commits each CREATE as it goes, so an interrupted first run leaves some tables
    # and not others. Asking only whether the database has any tables at all would accept this.
    path = tmp_path / "db.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE t(x)")
    connection.commit()
    connection.close()

    with reading_connection(path, ("t", "later")) as snapshot:
        assert snapshot is None


def test_the_guard_does_not_simply_refuse_everything(tmp_path: Path) -> None:
    # The two tests above would also pass if the guard rejected every database, so this pins the
    # other side: a schema that is all there still reads.
    path = tmp_path / "db.sqlite3"
    written(path)

    with reading_connection(path, ("t",)) as connection:
        assert connection is not None
        assert connection.execute("SELECT count(*) FROM t").fetchone()[0] == 1
