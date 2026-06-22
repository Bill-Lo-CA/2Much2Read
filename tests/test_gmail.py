from pathlib import Path
from unittest.mock import MagicMock

import pytest

from newsletter_digest import gmail
from newsletter_digest.config import Source
from newsletter_digest.gmail import SCOPES, GmailClient, find_label_id, source_backfill_query


def test_backfill_query_uses_sender_and_missing_category_label() -> None:
    source = Source.model_validate(
        {
            "id": "news",
            "name": "News",
            "gmail_query": "label:ai-newsPaper from:news@example.com",
            "gmail_filter": {"label": "ai-newsPaper", "criteria": {"from": "news@example.com"}},
        }
    )

    assert source_backfill_query(source) == 'from:news@example.com -label:"ai-newsPaper"'
    assert find_label_id({"ai-newspaper": "Label_1"}, "ai-newsPaper") == "Label_1"


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


def test_ensure_labels_creates_only_processing_labels() -> None:
    service = MagicMock()
    service.users().labels().create.return_value.execute.side_effect = [{"id": "Processed"}, {"id": "Failed"}]
    client = object.__new__(GmailClient)
    client.service = service
    client.labels = {}

    client.ensure_labels()

    names = [call.kwargs["body"]["name"] for call in service.users().labels().create.call_args_list]
    assert names == ["NewsletterBot/Processed", "NewsletterBot/Failed"]


def test_message_metadata_requests_only_subscription_headers() -> None:
    service = MagicMock()
    service.users().messages().get.return_value.execute.return_value = {"id": "message-1"}
    client = object.__new__(GmailClient)
    client.service = service

    assert client.get_message_metadata("message-1") == {"id": "message-1"}
    service.users().messages().get.assert_called_once_with(
        userId="me",
        id="message-1",
        format="metadata",
        metadataHeaders=["From", "Subject", "List-ID", "List-Unsubscribe", "X-EmailOctopus-List-Id"],
    )


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
    client.labels = {"ai-newspaper": "Label_1"}
    source = Source.model_validate(
        {
            "id": "news",
            "name": "News",
            "gmail_query": "label:ai-newsPaper from:news@example.com",
            "gmail_filter": {"label": "ai-newsPaper", "criteria": {"from": "news@example.com"}},
        }
    )

    assert client.ensure_source_filters([source]) == [{"source_id": "news", "filter_id": "Filter_1", "status": "exists"}]
    service.users().labels().create.assert_not_called()
    service.users().settings().filters().create.assert_not_called()
