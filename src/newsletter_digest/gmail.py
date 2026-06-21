from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import-untyped]
from googleapiclient.discovery import build  # type: ignore[import-untyped]

from .config import Source

SCOPES = (
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.settings.basic",
)
LABEL_PREFIX = "NewsletterBot/"


def credentials(credentials_path: Path, token_path: Path, port: int = 8765) -> Credentials:
    creds: Credentials | None = None
    if token_path.is_file():
        creds = Credentials.from_authorized_user_file(str(token_path))  # type: ignore[no-untyped-call]
    if creds and not creds.has_scopes(SCOPES):  # type: ignore[no-untyped-call]
        creds = None
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())  # type: ignore[no-untyped-call]
    if not creds or not creds.valid:
        if not credentials_path.is_file():
            raise ValueError(f"GMAIL_AUTH_REQUIRED: missing {credentials_path}")
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
        creds = flow.run_local_server(port=port, access_type="offline", prompt="consent", open_browser=False)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    os.chmod(token_path, 0o600)
    return creds


class GmailClient:
    def __init__(self, creds: Credentials) -> None:
        self.service: Any = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self.labels = self._label_map()

    def _label_map(self) -> dict[str, str]:
        data = self.service.users().labels().list(userId="me").execute()
        return {label["name"]: label["id"] for label in data.get("labels", [])}

    def ensure_labels(self, source_names: list[str]) -> dict[str, str]:
        names = [f"{LABEL_PREFIX}Processed", f"{LABEL_PREFIX}Failed"] + [
            f"{LABEL_PREFIX}Source/{name.replace('/', '-')}" for name in source_names
        ]
        for name in names:
            if name not in self.labels:
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
                self.labels[name] = label["id"]
        return self.labels

    def ensure_source_filters(self, sources: list[Source]) -> list[dict[str, str]]:
        existing: list[dict[str, Any]] = self.service.users().settings().filters().list(userId="me").execute().get("filter", [])
        results: list[dict[str, str]] = []
        for source in sources:
            if source.gmail_filter is None:
                continue
            label_name = source.gmail_filter.label
            if label_name not in self.labels:
                label = (
                    self.service.users()
                    .labels()
                    .create(
                        userId="me",
                        body={
                            "name": label_name,
                            "labelListVisibility": "labelShow",
                            "messageListVisibility": "show",
                        },
                    )
                    .execute()
                )
                self.labels[label_name] = label["id"]
            body: dict[str, Any] = {
                "criteria": source.gmail_filter.criteria,
                "action": {"addLabelIds": [self.labels[label_name]]},
            }
            found = next(
                (
                    item
                    for item in existing
                    if item.get("criteria") == body["criteria"]
                    and self.labels[label_name] in item.get("action", {}).get("addLabelIds", [])
                ),
                None,
            )
            if found is None:
                found = self.service.users().settings().filters().create(userId="me", body=body).execute()
                existing.append({**body, **found})
                status = "created"
            else:
                status = "exists"
            results.append({"source_id": source.id, "filter_id": str(found["id"]), "status": status})
        return results

    def list_messages(self, query: str, limit: int) -> list[str]:
        ids: list[str] = []
        page_token: str | None = None
        while len(ids) < limit:
            response = (
                self.service.users()
                .messages()
                .list(
                    userId="me",
                    q=query,
                    maxResults=min(100, limit - len(ids)),
                    pageToken=page_token,
                )
                .execute()
            )
            ids.extend(message["id"] for message in response.get("messages", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return ids

    def get_message(self, message_id: str) -> dict[str, Any]:
        result: dict[str, Any] = self.service.users().messages().get(userId="me", id=message_id, format="full").execute()
        return result

    def add_labels(self, message_id: str, names: list[str]) -> None:
        if any(not name.startswith(LABEL_PREFIX) for name in names):
            raise ValueError("refusing to modify a non-NewsletterBot label")
        self.service.users().messages().modify(
            userId="me", id=message_id, body={"addLabelIds": [self.labels[name] for name in names]}
        ).execute()


def display_id(message_id: str) -> str:
    return hashlib.sha256(message_id.encode()).hexdigest()[:10]
