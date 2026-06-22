import base64
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from newsletter_digest import cli
from newsletter_digest.config import Settings
from newsletter_digest.schemas import EmailExtraction


def test_run_help_uses_clear_delivery_flags() -> None:
    result = CliRunner().invoke(cli.app, ["run", "--help"])

    assert result.exit_code == 0
    assert "--deliver" in result.stdout
    assert "--no-deliver" in result.stdout
    assert "--no-no-deliver" not in result.stdout


def test_discover_mails_uses_configured_query(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text(
        "sources:\n  - id: alphasignal\n    name: AlphaSignal\n    enabled: false\n"
        "    gmail_query: 'label:newsletter-alphasignal'\n",
        encoding="utf-8",
    )
    settings = Settings(sources_config_path=sources_path)
    monkeypatch.setattr(cli, "Settings", lambda: settings)

    class FakeGmailClient:
        labels: dict[str, str] = {}

        def list_messages(self, query: str, limit: int) -> list[str]:
            assert (query, limit) == ("label:newsletter-alphasignal", 20)
            return []

    monkeypatch.setattr(cli, "gmail_client", lambda settings: FakeGmailClient())

    result = CliRunner().invoke(cli.app, ["discover", "mails", "--source", "alphasignal"])

    assert result.exit_code == 0


def test_discover_query_uses_explicit_query(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeGmailClient:
        def list_messages(self, query: str, limit: int) -> list[str]:
            assert (query, limit) == ("subject:weekly", 3)
            return []

    monkeypatch.setattr(cli, "gmail_client", lambda settings: FakeGmailClient())

    result = CliRunner().invoke(cli.app, ["discover", "--query", "subject:weekly", "--limit", "3"])

    assert result.exit_code == 0


def test_discover_subscription_source_uses_detected_query(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text("sources: []\n", encoding="utf-8")
    settings = Settings(sources_config_path=sources_path)

    class FakeGmailClient:
        labels = {"ai-newsPaper": "Label_AI"}

        def list_messages(self, query: str, limit: int) -> list[str]:
            if query == "newer_than:30d":
                return ["new"]
            assert query == "label:ai-newsPaper from:news@example.com"
            return []

        def get_message_metadata(self, message_id: str) -> dict[str, Any]:
            return {
                "labelIds": ["Label_AI"],
                "payload": {
                    "headers": [
                        {"name": "From", "value": "Example News <news@example.com>"},
                        {"name": "List-ID", "value": "Example Newsletter <newsletter.example.com>"},
                    ]
                },
            }

    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(cli, "gmail_client", lambda settings: FakeGmailClient())

    result = CliRunner().invoke(
        cli.app,
        ["discover", "subscriptions", "--source", "newsletter-example-com"],
    )

    assert result.exit_code == 0


def test_subscriptions_list_and_sync_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text(
        "# keep this comment\nsources:\n"
        "  - id: existing\n    name: Existing\n    category: AI\n"
        "    gmail_query: 'label:ai-newsPaper from:existing@example.com'\n",
        encoding="utf-8",
    )
    sources_path.chmod(0o600)
    settings = Settings(sources_config_path=sources_path)

    class FakeGmailClient:
        labels = {"ai-newsPaper": "Label_AI"}

        def list_messages(self, query: str, limit: int) -> list[str]:
            assert limit == 100
            if query == "newer_than:30d":
                return ["existing", "new", "spam"]
            if query == "from:news@example.com":
                return ["new"]
            if query == "from:no-reply@substack.com":
                return ["spam"]
            raise AssertionError(query)

        def get_message_metadata(self, message_id: str) -> dict[str, Any]:
            sender = {
                "existing": "existing@example.com",
                "new": "news@example.com",
                "spam": "no-reply@substack.com",
            }[message_id]
            return {
                "labelIds": ["Label_AI"],
                "payload": {
                    "headers": [
                        {"name": "From", "value": f"Example News <{sender}>"},
                        {"name": "Subject", "value": "Daily update"},
                        {"name": "List-ID", "value": f"<{message_id}.example.com>"},
                        {"name": "List-Unsubscribe", "value": "<mailto:unsubscribe@example.com>"},
                    ]
                },
            }

    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(cli, "gmail_client", lambda settings: FakeGmailClient())

    listed = CliRunner().invoke(cli.app, ["discover", "subscriptions", "list"])

    assert listed.exit_code == 0
    subscriptions = json.loads(listed.stdout)["subscriptions"]
    assert [item["configured"] for item in subscriptions] == [True, False, False]

    synced = CliRunner().invoke(cli.app, ["discover", "subscriptions", "--sync", "--apply"], input="9\n3\n6\n")

    assert synced.exit_code == 0
    assert "Please enter a number from 1 to 6." in synced.stdout
    output = json.loads(synced.stdout.splitlines()[-1])
    assert output["status"] == "applied"
    assert output["ambiguous"] == []
    assert "# keep this comment" in sources_path.read_text(encoding="utf-8")
    assert sources_path.stat().st_mode & 0o777 == 0o600
    sources = cli.load_sources(sources_path).sources
    assert [source.id for source in sources] == ["existing", "new-example-com"]
    assert sources[-1].enabled is True
    assert sources[-1].category == "CYBERSECURITY"
    assert sources[-1].gmail_filter is not None
    assert sources[-1].gmail_filter.label == "cyber-newspaper"
    exclusions = cli.load_excluded_subscriptions(sources_path.with_name("excluded-subscriptions.yaml"))
    assert [(item.id, item.sender) for item in exclusions.excluded_subscriptions] == [
        ("spam-example-com", "no-reply@substack.com")
    ]

    listed_again = CliRunner().invoke(cli.app, ["discover", "subscriptions", "list"])

    assert listed_again.exit_code == 0
    assert [item["id"] for item in json.loads(listed_again.stdout)["subscriptions"]] == ["existing", "new-example-com"]


def test_shared_sender_uses_display_name_and_rejects_ambiguous_query() -> None:
    class FakeGmailClient:
        labels: dict[str, str] = {}

        def list_messages(self, query: str, limit: int) -> list[str]:
            if query == "newer_than:30d":
                return ["ai", "dev", "main"]
            if query == 'from:dan@example.com from:"TLDR AI"':
                return ["ai"]
            if query == 'from:dan@example.com from:"TLDR"':
                return ["main", "confirmation"]
            raise AssertionError(query)

        def get_message_metadata(self, message_id: str) -> dict[str, Any]:
            names = {"ai": "TLDR AI", "dev": "TLDR Dev", "main": "TLDR", "confirmation": "TLDR"}
            headers = [{"name": "From", "value": f"{names[message_id]} <dan@example.com>"}]
            if message_id != "confirmation":
                headers.extend(
                    [
                        {"name": "List-Unsubscribe", "value": "<https://example.com/unsubscribe>"},
                        {"name": "X-EmailOctopus-List-Id", "value": f"list-{message_id}"},
                    ]
                )
            return {"payload": {"headers": headers}}

    gmail = FakeGmailClient()
    candidates = cli.subscription_candidates(gmail, [], set(), 100)  # type: ignore[arg-type]
    by_name = {item["name"]: item for item in candidates}

    assert set(by_name) == {"TLDR", "TLDR AI", "TLDR Dev"}
    assert by_name["TLDR AI"]["id"] == "tldr-ai"
    assert by_name["TLDR AI"]["base_query"] == 'from:dan@example.com from:"TLDR AI"'
    assert cli.valid_subscription_query(gmail, by_name["TLDR AI"], 100)  # type: ignore[arg-type]
    assert not cli.valid_subscription_query(gmail, by_name["TLDR"], 100)  # type: ignore[arg-type]


def test_filters_list_outputs_existing_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeGmailClient:
        def __init__(self, creds: object) -> None:
            pass

        def list_filters(self) -> list[dict[str, Any]]:
            return [{"id": "Filter_1", "criteria": {"from": "news@example.com"}, "action": {"addLabelIds": ["Label_1"]}}]

    monkeypatch.setattr(cli, "credentials", lambda *args: object())
    monkeypatch.setattr(cli, "GmailClient", FakeGmailClient)

    result = CliRunner().invoke(cli.app, ["filters", "list"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["filters"][0]["id"] == "Filter_1"


def test_inspect_outputs_parsed_text_and_optional_extraction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text(
        "sources:\n  - id: alphasignal\n    name: AlphaSignal\n    gmail_query: 'label:newsletter-alphasignal'\n",
        encoding="utf-8",
    )
    settings = Settings(sources_config_path=sources_path)
    encoded_body = base64.urlsafe_b64encode(b"Parsed newsletter body").decode().rstrip("=")

    class FakeGmailClient:
        def __init__(self, creds: object) -> None:
            pass

        def list_messages(self, query: str, limit: int) -> list[str]:
            assert query == "label:newsletter-alphasignal"
            assert limit == 100
            return ["gmail-1"]

        def get_message(self, message_id: str) -> dict[str, Any]:
            assert message_id == "gmail-1"
            return {
                "internalDate": "1234567890",
                "labelIds": ["Label_1"],
                "payload": {
                    "mimeType": "text/plain",
                    "headers": [{"name": "Subject", "value": "Test newsletter"}],
                    "body": {"data": encoded_body},
                },
            }

    class FakeOllamaClient:
        def __init__(self, *args: object) -> None:
            pass

        def extract(self, source_id: str, content: str, truncated: bool, max_items: int) -> EmailExtraction:
            assert (source_id, content, truncated, max_items) == (
                "alphasignal",
                "Parsed newsletter body",
                False,
                10,
            )
            return EmailExtraction(
                source_id=source_id,
                newsletter_title="Test newsletter",
                newsletter_date=None,
                overview_zh_tw="摘要",
                items=[],
                truncated_input=truncated,
            )

    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(cli, "credentials", lambda *args: object())
    monkeypatch.setattr(cli, "GmailClient", FakeGmailClient)
    monkeypatch.setattr(cli, "OllamaClient", FakeOllamaClient)

    result = CliRunner().invoke(
        cli.app,
        ["inspect", "--source", "alphasignal", "--id", cli.display_id("gmail-1"), "--extract"],
    )

    assert result.exit_code == 0
    output = json.loads(result.stdout)
    assert output["metadata"]["mime_type"] == "text/plain"
    assert output["metadata"]["label_ids"] == ["Label_1"]
    assert output["parsed"] == {
        "text": "Parsed newsletter body",
        "original_characters": 22,
        "input_characters": 22,
        "truncated": False,
    }
    assert output["extraction"]["source_id"] == "alphasignal"
