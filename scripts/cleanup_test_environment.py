from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from two_much_two_read.config import GmailSource, Settings, load_sources
from two_much_two_read.gmail import GmailClient, credentials, find_label_id
from two_much_two_read.storage import SCHEMA_VERSION, Database
from two_read_runtime.locking import ProcessLock

PROCESSING_LABELS = ["NewsletterBot/Processed", "NewsletterBot/Failed"]


def database_counts(path: Path) -> dict[str, int]:
    if not path.is_file():
        return {"documents": 0, "items": 0, "digests": 0, "runs": 0}
    connection = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    try:
        tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "documents" not in tables:
            return {"documents": 0, "items": 0, "digests": 0, "runs": 0}
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("documents", "items", "digests", "runs")
        }
    finally:
        connection.close()


def reset_database(path: Path) -> None:
    for suffix in ("", "-journal", "-shm", "-wal"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
    database = Database(path)
    database.close()


def gmail_message_ids(gmail: GmailClient, queries: list[str]) -> set[str]:
    labels = [name for name in PROCESSING_LABELS if find_label_id(gmail.labels, name) is not None]
    if not labels:
        return set()
    state_query = "{" + " ".join(f'label:"{name}"' for name in labels) + "}"
    ids: set[str] = set()
    for query in queries:
        ids.update(gmail.list_messages(f"({query}) {state_query}"))
    return ids


def main() -> None:
    parser = argparse.ArgumentParser(description="Reset all local 2much2read test state")
    parser.add_argument("--apply", action="store_true", help="perform the reset; otherwise only show counts")
    args = parser.parse_args()
    settings = Settings()
    queries = [
        source.gmail_query for source in load_sources(settings.sources_config_path).sources if isinstance(source, GmailSource)
    ]
    with ProcessLock(settings.lock_path):
        gmail = GmailClient(
            credentials(
                settings.gmail_credentials_path,
                settings.gmail_token_path,
                settings.gmail_oauth_callback_port,
            )
        )

    def inspect() -> tuple[set[str], dict[str, int]]:
        message_ids = gmail_message_ids(gmail, queries)
        counts = database_counts(settings.database_path)
        print(f"gmail_messages: {len(message_ids)}")
        for table, count in counts.items():
            print(f"sqlite_{table}: {count}")
        return message_ids, counts

    if not args.apply:
        inspect()
        print("Dry run. Rerun with --apply to reset the test environment.")
        return

    with ProcessLock(settings.lock_path):
        message_ids, _ = inspect()
        reset_database(settings.database_path)
        for message_id in message_ids:
            gmail.remove_labels(message_id, PROCESSING_LABELS)
        print(f"Reset complete: schema v{SCHEMA_VERSION}.")


if __name__ == "__main__":
    main()
