"""Reading a SQLite database without changing it.

A dry run has to be possible whatever else is happening, and has to leave the data directory
exactly as it found it. A plain read-only connection gives neither on its own: SQLite creates the
-wal and -shm sidecars when they are missing, even through mode=ro.
"""

import sqlite3
from pathlib import Path

from two_read_runtime.sqlite_snapshot import reading_connection


def written(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE t(x)")
    connection.execute("INSERT INTO t VALUES(1)")
    connection.commit()
    connection.close()


def listing(path: Path) -> dict[str, tuple[int, int]]:
    return {entry.name: (entry.stat().st_size, entry.stat().st_mtime_ns) for entry in path.iterdir()}


def source_file(connection: sqlite3.Connection) -> str:
    return str(connection.execute("PRAGMA database_list").fetchone()[2])


def test_nothing_to_read_yields_nothing(tmp_path: Path) -> None:
    with reading_connection(tmp_path / "absent.sqlite3") as connection:
        assert connection is None


def test_a_cleanly_closed_database_is_read_from_a_copy(tmp_path: Path) -> None:
    # Reading it in place would put the -wal and -shm files back beside it.
    path = tmp_path / "db.sqlite3"
    written(path)
    before = listing(tmp_path)

    with reading_connection(path) as connection:
        assert connection is not None
        assert connection.execute("SELECT count(*) FROM t").fetchone()[0] == 1
        assert source_file(connection) != str(path)

    assert listing(tmp_path) == before


def test_a_database_with_its_sidecars_is_read_in_place(tmp_path: Path) -> None:
    # The sidecars exist, so opening the live file creates nothing - and reading it directly avoids
    # copying a database and a write-ahead log that a writer is still moving.
    path = tmp_path / "db.sqlite3"
    written(path)
    writer = sqlite3.connect(path)
    writer.execute("INSERT INTO t VALUES(2)")
    writer.commit()
    try:
        before = listing(tmp_path)

        with reading_connection(path) as connection:
            assert connection is not None
            assert connection.execute("SELECT count(*) FROM t").fetchone()[0] == 2
            assert source_file(connection) == str(path)

        assert listing(tmp_path) == before
    finally:
        writer.close()


def test_rows_come_back_as_mappings(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite3"
    written(path)

    with reading_connection(path) as connection:
        assert connection is not None
        assert connection.execute("SELECT x FROM t").fetchone()["x"] == 1


def test_a_write_during_the_copy_is_retaken(tmp_path: Path, monkeypatch) -> None:
    # The copy is only worth taking if it is not a half-written file, so a source that moved while
    # it was being read is copied again rather than reported on.
    import two_read_runtime.sqlite_snapshot as module

    path = tmp_path / "db.sqlite3"
    written(path)
    copies: list[int] = []
    real_copy = module.shutil.copy2

    def copy_then_touch(source, destination, *args, **kwargs):  # type: ignore[no-untyped-def]
        result = real_copy(source, destination, *args, **kwargs)
        copies.append(len(copies))
        if len(copies) == 1:
            connection = sqlite3.connect(path)
            connection.execute("INSERT INTO t VALUES(3)")
            connection.commit()
            connection.close()
        return result

    monkeypatch.setattr(module.shutil, "copy2", copy_then_touch)

    with reading_connection(path) as connection:
        assert connection is not None
        assert connection.execute("SELECT count(*) FROM t").fetchone()[0] == 2

    assert len(copies) == 2
