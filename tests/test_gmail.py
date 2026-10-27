from pathlib import Path
from unittest.mock import MagicMock

import pytest

from two_much_two_read import gmail
from two_much_two_read.config import Source
from two_much_two_read.gmail import SCOPES, FilterStatus, GmailClient, find_label_id, source_backfill_query


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


def test_backfill_query_includes_filter_query_for_shared_sender() -> None:
    source = Source.model_validate(
        {
            "id": "tldr-ai",
            "name": "TLDR AI",
            "gmail_query": 'label:ai-newsPaper from:dan@example.com from:"TLDR AI"',
            "gmail_filter": {
                "label": "ai-newsPaper",
                "criteria": {"from": "dan@example.com", "query": 'from:"TLDR AI"'},
            },
        }
    )

    assert source_backfill_query(source) == 'from:dan@example.com from:"TLDR AI" -label:"ai-newsPaper"'


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
    monkeypatch.setattr("two_read_runtime.oauth.Credentials.from_authorized_user_file", load_token)
    monkeypatch.setattr("two_read_runtime.oauth.InstalledAppFlow.from_client_secrets_file", create_flow)

    assert gmail.credentials(credentials_path, token_path, interactive=True) is new_credentials
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


@pytest.mark.parametrize(
    ("state", "expected_add", "expected_remove"),
    [
        ("processed", "Processed", "Failed"),
        ("failed", "Failed", "Processed"),
    ],
)
def test_sync_processing_label_changes_only_the_processing_labels(state: str, expected_add: str, expected_remove: str) -> None:
    service = MagicMock()
    client = object.__new__(GmailClient)
    client.service = service
    client.labels = {
        "NewsletterBot/Processed": "Processed",
        "NewsletterBot/Failed": "Failed",
        "Inbox": "Inbox",
    }

    client.sync_processing_label("gmail-1", state)

    service.users().messages().modify.assert_called_once_with(
        userId="me",
        id="gmail-1",
        body={"addLabelIds": [expected_add], "removeLabelIds": [expected_remove]},
    )


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


def test_iter_messages_follows_gmail_page_tokens() -> None:
    service = MagicMock()
    service.users().messages().list.return_value.execute.side_effect = [
        {"messages": [{"id": "first"}], "nextPageToken": "next"},
        {"messages": [{"id": "second"}]},
    ]
    client = object.__new__(GmailClient)
    client.service = service

    assert list(client.iter_messages("label:newsletter")) == ["first", "second"]
    assert [call.kwargs for call in service.users().messages().list.call_args_list] == [
        {"userId": "me", "q": "label:newsletter", "maxResults": 100, "pageToken": None},
        {"userId": "me", "q": "label:newsletter", "maxResults": 100, "pageToken": "next"},
    ]


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

    assert client.ensure_source_filters([source]) == [FilterStatus("news", "Newsletters/News", "Filter_1", "created")]
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

    assert client.ensure_source_filters([source]) == [FilterStatus("news", "ai-newsPaper", "Filter_1", "exists")]
    service.users().labels().create.assert_not_called()
    service.users().settings().filters().create.assert_not_called()


def _client_with_labels() -> tuple[GmailClient, MagicMock]:
    client = object.__new__(GmailClient)
    client.service = MagicMock()
    client.labels = {"INBOX": "INBOX", "Work": "Label_9", "NewsletterBot/Processed": "Label_P", "NewsletterBot/Failed": "Label_F"}
    return client, client.service.users.return_value.messages.return_value.modify


@pytest.mark.parametrize("method", ["add_labels", "remove_labels"])
def test_only_the_bots_own_labels_are_ever_added_or_removed(method: str) -> None:
    """The mailbox is the user's: a label outside NewsletterBot/ is never touched, even beside an owned one."""
    client, modify = _client_with_labels()

    for names in (["INBOX"], ["Work"], ["NewsletterBot/Processed", "INBOX"]):
        with pytest.raises(ValueError, match="non-NewsletterBot"):
            getattr(client, method)("m1", names)

    modify.assert_not_called()


def test_owned_labels_are_added_and_removed_by_their_ids() -> None:
    client, modify = _client_with_labels()

    client.add_labels("m1", ["NewsletterBot/Processed"])
    client.labels["NewsletterBot/failed"] = client.labels.pop("NewsletterBot/Failed")
    client.remove_labels("m1", ["NewsletterBot/Failed", "NewsletterBot/Unknown"])
    client.remove_labels("m1", ["NewsletterBot/Unknown"])

    assert [call.kwargs for call in modify.call_args_list] == [
        {"userId": "me", "id": "m1", "body": {"addLabelIds": ["Label_P"]}},
        # Found however Gmail cases it; one that does not exist is skipped, and none at all asks nothing.
        {"userId": "me", "id": "m1", "body": {"removeLabelIds": ["Label_F"]}},
    ]


@pytest.mark.parametrize(("state", "added", "removed"), [("processed", "Label_P", "Label_F"), ("failed", "Label_F", "Label_P")])
def test_a_processing_label_replaces_the_other_one(state: str, added: str, removed: str) -> None:
    client, modify = _client_with_labels()

    client.sync_processing_label("m1", state)

    modify.assert_called_once_with(userId="me", id="m1", body={"addLabelIds": [added], "removeLabelIds": [removed]})
    with pytest.raises(ValueError, match="unknown processing state"):
        client.sync_processing_label("m1", "archived")
