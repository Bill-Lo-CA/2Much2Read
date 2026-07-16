from __future__ import annotations

import argparse

from newsletter_digest.config import Settings, load_sources
from newsletter_digest.gmail import GmailClient, credentials, find_label_id, source_backfill_query


def main() -> None:
    parser = argparse.ArgumentParser(description="Add category labels to existing newsletter emails")
    parser.add_argument("--apply", action="store_true", help="add labels; otherwise only show counts")
    parser.add_argument("--limit-per-source", type=int, default=500)
    parser.add_argument("--source", action="append", help="limit to one source ID; repeat for multiple sources")
    args = parser.parse_args()
    if args.limit_per_source < 1:
        parser.error("--limit-per-source must be at least 1")

    settings = Settings()
    sources = [source for source in load_sources(settings.sources_config_path).sources if source.enabled]
    requested = set(args.source or [])
    unknown = requested - {source.id for source in sources}
    if unknown:
        parser.error(f"unknown or disabled source ID(s): {', '.join(sorted(unknown))}")
    if requested:
        sources = [source for source in sources if source.id in requested]
    gmail = GmailClient(
        credentials(
            settings.gmail_credentials_path,
            settings.gmail_token_path,
            settings.gmail_oauth_callback_port,
        )
    )
    jobs: list[tuple[str, str, str, str]] = []
    for source in sources:
        query = source_backfill_query(source)
        assert source.gmail_filter is not None
        label_name = source.gmail_filter.label
        label_id = find_label_id(gmail.labels, label_name)
        if label_id is None:
            raise ValueError(f"Gmail label {label_name!r} does not exist; run '2much2read labels ensure' first")
        jobs.append((source.id, label_name, label_id, query))

    messages_by_topic: dict[tuple[str, str], set[str]] = {}
    for _, label_name, label_id, query in jobs:
        messages_by_topic.setdefault((label_name, label_id), set()).update(gmail.list_messages(query, args.limit_per_source))

    total = 0
    for (label_name, label_id), message_ids in sorted(messages_by_topic.items()):
        print(f"{label_name}: {len(message_ids)} message(s)")
        total += len(message_ids)
        if args.apply:
            for message_id in message_ids:
                gmail.add_label_id(message_id, label_id)
    if not args.apply:
        print(f"Dry run: {total} message(s). Rerun with --apply to add the labels.")


if __name__ == "__main__":
    main()
