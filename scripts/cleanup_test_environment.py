from __future__ import annotations

import argparse
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from two_much_two_read.config import GmailSource, Settings, load_sources
from two_much_two_read.gmail import GmailClient, credentials, find_label_id
from two_much_two_read.storage import Database
from two_read_runtime.locking import ProcessLock

PROCESSING_LABELS = ["NewsletterBot/Processed", "NewsletterBot/Failed"]


def database_counts(path: Path) -> dict[str, int]:
    if not path.is_file():
        return {"documents": 0, "items": 0, "digests": 0, "runs": 0}
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("documents", "items", "digests", "runs")
        }
    finally:
        connection.close()


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
        database = Database(settings.database_path)
        try:
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            backup_path = settings.database_path.with_name(f"{settings.database_path.name}.backup-{timestamp}")
            database.backup(backup_path)
            for message_id in message_ids:
                gmail.remove_labels(message_id, PROCESSING_LABELS)
            database.reset()
        finally:
            database.close()
        print(f"backup: {backup_path}")
        print("Reset complete.")


if __name__ == "__main__":
    main()
