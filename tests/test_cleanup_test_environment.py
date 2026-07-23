import importlib.util
import sqlite3
from pathlib import Path

from two_much_two_read.storage import SCHEMA_VERSION, Database

script_path = Path(__file__).parents[1] / "scripts" / "cleanup_test_environment.py"
spec = importlib.util.spec_from_file_location("cleanup_test_environment", script_path)
assert spec is not None and spec.loader is not None
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


def test_reset_database_replaces_a_legacy_database_with_v2(tmp_path: Path) -> None:
    path = tmp_path / "digest.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE schema_version(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);"
        "INSERT INTO schema_version VALUES(1, 'now');"
        "CREATE TABLE messages(id INTEGER PRIMARY KEY);"
    )
    connection.close()
    Path(f"{path}-wal").write_text("stale", encoding="utf-8")
    Path(f"{path}-journal").write_text("stale", encoding="utf-8")

    assert cleanup.database_counts(path) == {"documents": 0, "items": 0, "digests": 0, "runs": 0}
    cleanup.reset_database(path)

    database = Database(path)
    assert database.connection.execute("SELECT version FROM schema_version").fetchone()[0] == SCHEMA_VERSION
    assert database.connection.execute("SELECT 1 FROM sqlite_master WHERE name='messages'").fetchone() is None
    assert database.counts() == {"documents": 0, "gmail_document_state": 0, "items": 0, "digests": 0, "runs": 0}
    database.close()
