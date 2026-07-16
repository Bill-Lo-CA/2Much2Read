import base64
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from newsletter_digest import cli, operations
from newsletter_digest.config import Settings
from newsletter_digest.operations import FiltersResult
from newsletter_digest.schemas import EmailExtraction


def test_cli_has_only_target_command_tree() -> None:
    runner = CliRunner()

    root = runner.invoke(cli.app, ["--help"])
    assert root.exit_code == 0
    for command in ("auth", "labels", "filters", "mails", "subscriptions", "delivery", "doctor", "run", "backfill"):
        assert command in root.stdout
    assert "discover" not in root.stdout
    assert "resend" not in root.stdout

    delivery = runner.invoke(cli.app, ["delivery", "--help"])
    assert delivery.exit_code == 0
    assert "retry" in delivery.stdout
    assert "resend" not in delivery.stdout


def test_run_help_uses_clear_delivery_flags_without_resend() -> None:
    result = CliRunner().invoke(cli.app, ["run", "--help"])

    assert result.exit_code == 0
    assert "--deliver" in result.stdout
    assert "--no-deliver" in result.stdout
    assert "--resend" not in result.stdout


def test_run_outputs_elapsed_time_without_polluting_json(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run_pipeline(*args: object) -> dict[str, int | str]:
        return {"status": "ok", "discovered": 1, "processed": 1, "delivered": 0}

    monkeypatch.setattr(cli, "run_pipeline", fake_run_pipeline)

    result = CliRunner().invoke(cli.app, ["run", "--source", "news", "--max-messages", "1"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"status": "ok", "discovered": 1, "processed": 1, "delivered": 0}
    assert "2much2read run elapsed" in result.stderr
    assert "2much2read run finished in" in result.stderr


@pytest.mark.parametrize(
    "arguments",
    [[], ["--source", "one", "--query", "from:two"], ["--source", "one", "--subscription", "two"]],
)
def test_mails_require_exactly_one_selector(arguments: list[str]) -> None:
    result = CliRunner().invoke(cli.app, ["mails", "list", *arguments])

    assert result.exit_code == 2
    assert "exactly one of" in result.output


def test_mails_list_uses_configured_source_query(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text(
        "sources:\n  - id: alphasignal\n    name: AlphaSignal\n    enabled: false\n"
        "    gmail_query: 'label:newsletter-alphasignal'\n",
        encoding="utf-8",
    )
    settings = Settings(sources_config_path=sources_path)

    class FakeGmailClient:
        def list_messages(self, query: str, limit: int) -> list[str]:
            assert (query, limit) == ("label:newsletter-alphasignal", 20)
            return []

    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(operations, "gmail_client", lambda settings: FakeGmailClient())

    result = CliRunner().invoke(cli.app, ["mails", "list", "--source", "alphasignal"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"status": "ok", "mails": []}


def test_mails_list_uses_explicit_query(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeGmailClient:
        def list_messages(self, query: str, limit: int) -> list[str]:
            assert (query, limit) == ("subject:weekly", 3)
            return []

    monkeypatch.setattr(operations, "gmail_client", lambda settings: FakeGmailClient())

    result = CliRunner().invoke(cli.app, ["mails", "list", "--query", "subject:weekly", "--limit", "3"])

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
    monkeypatch.setattr(operations, "gmail_client", lambda settings: FakeGmailClient())
    runner = CliRunner()

    preview = runner.invoke(cli.app, ["subscriptions", "sync"])
    assert preview.exit_code == 0
    assert json.loads(preview.stdout)["status"] == "preview"
    assert "news-example-com" not in sources_path.read_text(encoding="utf-8")

    synced = runner.invoke(cli.app, ["subscriptions", "sync", "--apply"], input="9\n3\n6\n")
    assert synced.exit_code == 0
    assert "Please enter a number from 1 to 6." in synced.stdout
    assert json.loads(synced.stdout.splitlines()[-1])["status"] == "applied"
    assert "# keep this comment" in sources_path.read_text(encoding="utf-8")
    assert sources_path.stat().st_mode & 0o777 == 0o600
    sources = operations.load_sources(sources_path).sources
    assert [source.id for source in sources] == ["existing", "new-example-com"]
    assert sources[-1].category == "CYBERSECURITY"
    exclusions = operations.load_excluded_subscriptions(sources_path.with_name("excluded-subscriptions.yaml"))
    assert [(item.id, item.sender) for item in exclusions.excluded_subscriptions] == [
        ("spam-example-com", "no-reply@substack.com")
    ]


def test_filters_commands_share_one_operation(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []

    def fake_filters(settings: Settings, ensure: bool) -> FiltersResult:
        calls.append(ensure)
        return FiltersResult(filters=[])

    monkeypatch.setattr(cli, "filters", fake_filters)
    runner = CliRunner()

    assert runner.invoke(cli.app, ["filters", "ensure"]).exit_code == 0
    assert runner.invoke(cli.app, ["filters", "audit"]).exit_code == 0
    assert calls == [True, False]


def test_mails_inspect_outputs_parsed_text_and_extraction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text(
        "sources:\n  - id: alphasignal\n    name: AlphaSignal\n    gmail_query: 'label:newsletter-alphasignal'\n",
        encoding="utf-8",
    )
    settings = Settings(sources_config_path=sources_path)
    encoded_body = base64.urlsafe_b64encode(b"Parsed newsletter body").decode().rstrip("=")

    class FakeGmailClient:
        def list_messages(self, query: str, limit: int) -> list[str]:
            assert (query, limit) == ("label:newsletter-alphasignal", 100)
            return ["gmail-1"]

        def get_message(self, message_id: str) -> dict[str, Any]:
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
            assert (source_id, content, truncated, max_items) == ("alphasignal", "Parsed newsletter body", False, 10)
            return EmailExtraction(
                source_id=source_id,
                newsletter_title="Test newsletter",
                newsletter_date=None,
                overview_zh_tw="摘要",
                items=[],
            )

    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(operations, "gmail_client", lambda settings: FakeGmailClient())
    monkeypatch.setattr(operations, "OllamaClient", FakeOllamaClient)

    result = CliRunner().invoke(
        cli.app,
        ["mails", "inspect", "--source", "alphasignal", "--id", operations.display_id("gmail-1"), "--extract"],
    )

    assert result.exit_code == 0
    output = json.loads(result.stdout)
    assert output["metadata"]["mime_type"] == "text/plain"
    assert output["parsed"]["text"] == "Parsed newsletter body"
    assert output["extraction"]["source_id"] == "alphasignal"
