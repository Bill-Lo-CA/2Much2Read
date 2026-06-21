from pathlib import Path
from unittest.mock import MagicMock

import pytest

from newsletter_digest import gmail
from newsletter_digest.config import Source
from newsletter_digest.gmail import SCOPES, GmailClient


def test_old_token_missing_scope_reauthorizes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    credentials_path = tmp_path / "client.json"
    token_path = tmp_path / "token.json"
    credentials_path.write_text("{}", encoding="utf-8")
    token_path.write_text("{}", encoding="utf-8")
    old_credentials = MagicMock()
    old_credentials.has_scopes.return_value = False
    new_credentials = MagicMock()
    new_credentials.to_json.return_value = '{"scopes": []}'
    flow = MagicMock()
    flow.run_local_server.return_value = new_credentials
    load_token = MagicMock(return_value=old_credentials)
    create_flow = MagicMock(return_value=flow)
    monkeypatch.setattr(gmail.Credentials, "from_authorized_user_file", load_token)
    monkeypatch.setattr(gmail.InstalledAppFlow, "from_client_secrets_file", create_flow)

    assert gmail.credentials(credentials_path, token_path) is new_credentials
    create_flow.assert_called_once_with(str(credentials_path), SCOPES)
    flow.run_local_server.assert_called_once_with(port=8765, access_type="offline", prompt="consent", open_browser=False)


def test_ensure_source_filter_creates_label_and_filter() -> None:
    service = MagicMock()
    service.users().settings().filters().list().execute.return_value = {"filter": []}
    service.users().labels().create.return_value.execute.return_value = {"id": "Label_1"}
    service.users().settings().filters().create.return_value.execute.return_value = {"id": "Filter_1"}
    client = object.__new__(GmailClient)
    client.service = service
    client.labels = {}
    source = Source.model_validate(
        {
            "id": "news",
            "name": "News",
            "gmail_query": 'label:"Newsletters/News"',
            "gmail_filter": {
                "label": "Newsletters/News",
                "criteria": {"from": "news@example.com", "subject": "Daily"},
            },
        }
    )

    assert client.ensure_source_filters([source]) == [{"source_id": "news", "filter_id": "Filter_1", "status": "created"}]
    service.users().settings().filters().create.assert_called_once_with(
        userId="me",
        body={
            "criteria": {"from": "news@example.com", "subject": "Daily"},
            "action": {"addLabelIds": ["Label_1"]},
        },
    )


def test_ensure_source_filter_reuses_existing_filter() -> None:
    service = MagicMock()
    service.users().settings().filters().list().execute.return_value = {
        "filter": [
            {
                "id": "Filter_1",
                "criteria": {"from": "news@example.com"},
                "action": {"addLabelIds": ["Label_1", "IMPORTANT"]},
            }
        ]
    }
    client = object.__new__(GmailClient)
    client.service = service
    client.labels = {"Newsletters/News": "Label_1"}
    source = Source.model_validate(
        {
            "id": "news",
            "name": "News",
            "gmail_query": 'label:"Newsletters/News"',
            "gmail_filter": {"label": "Newsletters/News", "criteria": {"from": "news@example.com"}},
        }
    )

    assert client.ensure_source_filters([source]) == [{"source_id": "news", "filter_id": "Filter_1", "status": "exists"}]
    service.users().settings().filters().create.assert_not_called()
