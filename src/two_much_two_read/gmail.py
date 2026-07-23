from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build  # type: ignore[import-untyped]

from two_read_runtime.oauth import load_credentials

from .config import Source

SCOPES = (
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.settings.basic",
)
LABEL_PREFIX = "NewsletterBot/"


@dataclass(frozen=True)
class FilterStatus:
    source_id: str
    label: str
    filter_id: str | None
    status: str


def find_label_id(labels: dict[str, str], name: str) -> str | None:
    return next((label_id for label_name, label_id in labels.items() if label_name.casefold() == name.casefold()), None)


def source_backfill_query(source: Source) -> str:
    if source.gmail_filter is None:
        raise ValueError(f"source {source.id!r} has no gmail_filter")
    sender = source.gmail_filter.criteria.get("from")
    if not isinstance(sender, str) or not sender.strip():
        raise ValueError(f"source {source.id!r} has no string gmail_filter.criteria.from")
    extra = source.gmail_filter.criteria.get("query")
    query = f"from:{sender}"
    if isinstance(extra, str) and extra.strip():
        query += f" {extra}"
    return f'{query} -label:"{source.gmail_filter.label}"'


def message_headers(message: dict[str, object]) -> dict[str, str]:
    payload = message.get("payload", {})
    headers = payload.get("headers", []) if isinstance(payload, dict) else []
    return {str(item.get("name", "")).lower(): str(item.get("value", "")) for item in headers if isinstance(item, dict)}


def credentials(credentials_path: Path, token_path: Path, port: int = 8765, *, interactive: bool = False) -> Credentials:
    return load_credentials(
        credentials_path,
        token_path,
        SCOPES,
        port,
        interactive=interactive,
        auth_command="2much2read auth gmail",
        missing_credentials_code="GMAIL_AUTH_REQUIRED",
    )


class GmailClient:
    def __init__(self, creds: Credentials) -> None:
        self.service: Any = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self.labels = self._label_map()

    def _label_map(self) -> dict[str, str]:
        data = self.service.users().labels().list(userId="me").execute()
        return {label["name"]: label["id"] for label in data.get("labels", [])}

    def _ensure_label(self, name: str) -> str:
        label_id = find_label_id(self.labels, name)
        if label_id is None:
            label = (
                self.service.users()
                .labels()
                .create(
                    userId="me",
                    body={
                        "name": name,
                        "labelListVisibility": "labelShow",
                        "messageListVisibility": "show",
                    },
                )
                .execute()
            )
            label_id = str(label["id"])
        self.labels[name] = label_id
        return label_id

    def ensure_labels(self) -> dict[str, str]:
        for name in (f"{LABEL_PREFIX}Processed", f"{LABEL_PREFIX}Failed"):
            self._ensure_label(name)
        return self.labels

    def ensure_source_labels(self, sources: list[Source]) -> list[str]:
        labels = sorted({source.gmail_filter.label for source in sources if source.gmail_filter is not None})
        for label in labels:
            self._ensure_label(label)
        return labels

    def _source_filter_statuses(self, sources: list[Source], create: bool) -> list[FilterStatus]:
        existing: list[dict[str, Any]] = self.service.users().settings().filters().list(userId="me").execute().get("filter", [])
        results: list[FilterStatus] = []
        for source in sources:
            if source.gmail_filter is None:
                continue
            label_name = source.gmail_filter.label
            label_id = find_label_id(self.labels, label_name)
            if label_id is None and not create:
                results.append(FilterStatus(source.id, label_name, None, "label_missing"))
                continue
            label_id = label_id or self._ensure_label(label_name)
            body: dict[str, Any] = {
                "criteria": source.gmail_filter.criteria,
                "action": {"addLabelIds": [label_id]},
            }
            found = next(
                (
                    item
                    for item in existing
                    if item.get("criteria") == body["criteria"] and label_id in item.get("action", {}).get("addLabelIds", [])
                ),
                None,
            )
            if found is None and create:
                found = self.service.users().settings().filters().create(userId="me", body=body).execute()
                existing.append({**body, **found})
                status = "created"
            elif found is not None:
                status = "exists"
            else:
                status = "filter_missing"
            results.append(FilterStatus(source.id, label_name, str(found["id"]) if found else None, status))
        return results

    def ensure_source_filters(self, sources: list[Source]) -> list[FilterStatus]:
        return self._source_filter_statuses(sources, create=True)

    def audit_source_filters(self, sources: list[Source]) -> list[FilterStatus]:
        return self._source_filter_statuses(sources, create=False)

    def iter_messages(self, query: str) -> Iterator[str]:
        page_token: str | None = None
        while True:
            response = (
                self.service.users()
                .messages()
                .list(
                    userId="me",
                    q=query,
                    maxResults=100,
                    pageToken=page_token,
                )
                .execute()
            )
            yield from (message["id"] for message in response.get("messages", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break

    def list_messages(self, query: str, limit: int | None = None) -> list[str]:
        if limit is not None and limit <= 0:
            return []
        ids: list[str] = []
        for message_id in self.iter_messages(query):
            ids.append(message_id)
            if limit is not None and len(ids) >= limit:
                break
        return ids

    def get_message(self, message_id: str) -> dict[str, Any]:
        result: dict[str, Any] = self.service.users().messages().get(userId="me", id=message_id, format="full").execute()
        return result

    def get_message_metadata(self, message_id: str) -> dict[str, Any]:
        result: dict[str, Any] = (
            self.service.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["From", "Subject", "List-ID", "List-Unsubscribe", "X-EmailOctopus-List-Id"],
            )
            .execute()
        )
        return result

    def add_labels(self, message_id: str, names: list[str]) -> None:
        if any(not name.startswith(LABEL_PREFIX) for name in names):
            raise ValueError("refusing to modify a non-NewsletterBot label")
        self.service.users().messages().modify(
            userId="me", id=message_id, body={"addLabelIds": [self.labels[name] for name in names]}
        ).execute()

    def sync_processing_label(self, message_id: str, state: str) -> None:
        labels = {
            "processed": (f"{LABEL_PREFIX}Processed", f"{LABEL_PREFIX}Failed"),
            "failed": (f"{LABEL_PREFIX}Failed", f"{LABEL_PREFIX}Processed"),
        }
        try:
            add_name, remove_name = labels[state]
        except KeyError as error:
            raise ValueError(f"unknown processing state: {state}") from error
        self.service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"addLabelIds": [self.labels[add_name]], "removeLabelIds": [self.labels[remove_name]]},
        ).execute()

    def add_label_id(self, message_id: str, label_id: str) -> None:
        self.service.users().messages().modify(userId="me", id=message_id, body={"addLabelIds": [label_id]}).execute()

    def remove_labels(self, message_id: str, names: list[str]) -> None:
        if any(not name.startswith(LABEL_PREFIX) for name in names):
            raise ValueError("refusing to remove a non-NewsletterBot label")
        label_ids = [label_id for name in names if (label_id := find_label_id(self.labels, name)) is not None]
        if label_ids:
            self.service.users().messages().modify(userId="me", id=message_id, body={"removeLabelIds": label_ids}).execute()


def display_id(message_id: str) -> str:
    return hashlib.sha256(message_id.encode()).hexdigest()[:10]
