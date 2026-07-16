from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import-untyped]
from googleapiclient.discovery import build  # type: ignore[import-untyped]

SCOPES = ("https://www.googleapis.com/auth/calendar.readonly",)


@dataclass(frozen=True)
class CalendarEvent:
    calendar_id: str
    calendar_name: str | None
    event_id: str
    instance_id: str
    title: str
    location: str
    start: datetime
    end: datetime
    all_day: bool


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
            raise ValueError(f"GOOGLE_CALENDAR_AUTH_REQUIRED: missing {credentials_path}")
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
        creds = flow.run_local_server(port=port, access_type="offline", prompt="consent", open_browser=False)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    os.chmod(token_path, 0o600)
    return creds


def _parse_datetime(value: str, timezone: ZoneInfo) -> datetime:
    if "T" in value:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone)
    return datetime.combine(date.fromisoformat(value), time.min, timezone)


class CalendarClient:
    def __init__(self, creds: Credentials, timezone: str) -> None:
        self.service: Any = build("calendar", "v3", credentials=creds, cache_discovery=False)
        self.timezone = ZoneInfo(timezone)

    def list_calendars(self) -> list[dict[str, str]]:
        result = self.service.calendarList().list().execute()
        return [{"id": str(item["id"]), "name": str(item.get("summary", item["id"]))} for item in result.get("items", [])]

    def list_events(
        self,
        calendar_id: str,
        calendar_name: str | None,
        time_min: datetime,
        time_max: datetime,
    ) -> list[CalendarEvent]:
        events: list[CalendarEvent] = []
        page_token: str | None = None
        while True:
            result = (
                self.service.events()
                .list(
                    calendarId=calendar_id,
                    timeMin=time_min.isoformat(),
                    timeMax=time_max.isoformat(),
                    singleEvents=True,
                    orderBy="startTime",
                    pageToken=page_token,
                )
                .execute()
            )
            for item in result.get("items", []):
                start_data = item.get("start", {})
                end_data = item.get("end", {})
                start_raw = start_data.get("dateTime") or start_data.get("date")
                end_raw = end_data.get("dateTime") or end_data.get("date")
                if not start_raw or not end_raw:
                    continue
                event_id = str(item.get("recurringEventId") or item["id"])
                instance_id = str(item["id"])
                events.append(
                    CalendarEvent(
                        calendar_id=calendar_id,
                        calendar_name=calendar_name,
                        event_id=event_id,
                        instance_id=instance_id,
                        title=str(item.get("summary") or "(untitled)"),
                        location=str(item.get("location") or ""),
                        start=_parse_datetime(str(start_raw), self.timezone),
                        end=_parse_datetime(str(end_raw), self.timezone),
                        all_day="date" in start_data,
                    )
                )
            page_token = result.get("nextPageToken")
            if not page_token:
                return events
