import base64
import json
import re
from typing import Any

import pytest
from typer.testing import CliRunner

from two_much_two_read import cli, mail_operations
from two_much_two_read.command_models import (
    DeliveryCheckpointResetResult,
    FiltersResult,
    LabelsReconcileResult,
    NewsletterRetryResult,
    NewsletterRunResult,
)
from two_much_two_read.config import Settings, load_excluded_subscriptions, load_sources
from two_much_two_read.gmail import display_id
from two_much_two_read.ollama import OllamaSchemaError
from two_much_two_read.schemas import EmailExtraction


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
    help_text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", result.stdout)

    assert result.exit_code == 0
    assert "--deliver" in help_text
    assert "--no-deliver" in help_text
    assert "--resend" not in help_text


def test_run_avoids_ansi_progress_when_stderr_is_not_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run_pipeline(*args: object, **kwargs: object) -> NewsletterRunResult:
        return NewsletterRunResult(status="ok", discovered=1, processed=1, failed=0, delivered=0)

    monkeypatch.setattr(cli, "run_pipeline", fake_run_pipeline)

    result = CliRunner().invoke(cli.app, ["run", "--source", "news", "--max-messages", "1"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"status": "ok", "discovered": 1, "processed": 1, "failed": 0, "delivered": 0}
    assert result.stderr == ""


def test_delivery_retry_keeps_its_json_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "retry_delivery", lambda _: NewsletterRetryResult(delivered=1, failed=0))

    result = CliRunner().invoke(cli.app, ["delivery", "retry"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"status": "ok", "delivered": 1, "failed": 0, "failed_by_error_code": {}}


def test_delivery_reset_checkpoint_requires_an_explicit_digest_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "reset_corrupt_delivery", lambda _, digest_id: DeliveryCheckpointResetResult(digest_id=digest_id))

    result = CliRunner().invoke(cli.app, ["delivery", "reset-checkpoint", "--digest-id", "7"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"status": "ok", "digest_id": 7}


def test_labels_reconcile_emits_a_typed_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "reconcile_labels", lambda _: LabelsReconcileResult(reconciled=2, failed=0))

    result = CliRunner().invoke(cli.app, ["labels", "reconcile"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"status": "ok", "reconciled": 2, "failed": 0}


@pytest.mark.parametrize(
    "arguments",
    [[], ["--source", "one", "--query", "from:two"], ["--source", "one", "--subscription", "two"]],
)
def test_mails_require_exactly_one_selector(arguments: list[str]) -> None:
    result = CliRunner().invoke(cli.app, ["mails", "list", *arguments])

    assert result.exit_code == 2
    assert "exactly one of" in result.output


def test_mails_list_uses_configured_source_query(newsletter_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    sources_path = newsletter_settings.sources_config_path
    sources_path.write_text(
        "sources:\n  - id: alphasignal\n    name: AlphaSignal\n    enabled: false\n"
        "    gmail_query: 'label:newsletter-alphasignal'\n",
        encoding="utf-8",
    )

    class FakeGmailClient:
        def list_messages(self, query: str, limit: int) -> list[str]:
            assert (query, limit) == ("label:newsletter-alphasignal", 20)
            return []

    monkeypatch.setattr(cli, "Settings", lambda: newsletter_settings)
    monkeypatch.setattr(mail_operations, "gmail_client", lambda settings: FakeGmailClient())

    result = CliRunner().invoke(cli.app, ["mails", "list", "--source", "alphasignal"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"status": "ok", "mails": []}


def test_mails_list_uses_explicit_query(newsletter_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeGmailClient:
        def list_messages(self, query: str, limit: int) -> list[str]:
            assert (query, limit) == ("subject:weekly", 3)
            return []

    monkeypatch.setattr(cli, "Settings", lambda: newsletter_settings)
    monkeypatch.setattr(mail_operations, "gmail_client", lambda settings: FakeGmailClient())

    result = CliRunner().invoke(cli.app, ["mails", "list", "--query", "subject:weekly", "--limit", "3"])

    assert result.exit_code == 0


def test_subscriptions_list_and_sync_apply(newsletter_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    sources_path = newsletter_settings.sources_config_path
    sources_path.write_text(
        "# keep this comment\nsources:\n"
        "  - id: existing\n    name: Existing\n    category: AI\n"
        "    gmail_query: 'label:ai-newsPaper from:existing@example.com'\n",
        encoding="utf-8",
    )
    sources_path.chmod(0o600)

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

    monkeypatch.setattr(cli, "Settings", lambda: newsletter_settings)
    monkeypatch.setattr(cli, "gmail_client", lambda settings: FakeGmailClient())

    class FakeOllamaClient:
        def classify_subscription(self, name: str, sender: str, list_id: str | None, subject: str | None) -> str:
            assert name == "Example News"
            assert list_id is not None
            assert subject == "Daily update"
            if sender == "news@example.com":
                return "CYBERSECURITY"
            raise OllamaSchemaError("invalid classification")

    monkeypatch.setattr(cli, "create_ollama_client", lambda _: FakeOllamaClient())
    runner = CliRunner()

    preview = runner.invoke(cli.app, ["subscriptions", "sync"])
    assert preview.exit_code == 0
    assert json.loads(preview.stdout)["status"] == "preview"
    assert "news-example-com" not in sources_path.read_text(encoding="utf-8")

    synced = runner.invoke(cli.app, ["subscriptions", "sync", "--apply"], input="6\n")
    assert synced.exit_code == 0
    assert "Automatic classification failed for spam-example-com" in synced.stdout
    assert json.loads(synced.stdout.splitlines()[-1])["status"] == "applied"
    assert "# keep this comment" in sources_path.read_text(encoding="utf-8")
    assert sources_path.stat().st_mode & 0o777 == 0o600
    sources = load_sources(sources_path).sources
    assert [source.id for source in sources] == ["existing", "new-example-com"]
    assert sources[-1].category == "CYBERSECURITY"
    exclusions = load_excluded_subscriptions(sources_path.with_name("excluded-subscriptions.yaml"))
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


def test_mails_inspect_outputs_parsed_text_and_extraction(newsletter_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    sources_path = newsletter_settings.sources_config_path
    sources_path.write_text(
        "sources:\n  - id: alphasignal\n    name: AlphaSignal\n    gmail_query: 'label:newsletter-alphasignal'\n",
        encoding="utf-8",
    )
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

    monkeypatch.setattr(cli, "Settings", lambda: newsletter_settings)
    monkeypatch.setattr(mail_operations, "gmail_client", lambda settings: FakeGmailClient())
    monkeypatch.setattr(mail_operations, "create_ollama_client", lambda _: FakeOllamaClient())

    result = CliRunner().invoke(
        cli.app,
        ["mails", "inspect", "--source", "alphasignal", "--id", display_id("gmail-1"), "--extract"],
    )

    assert result.exit_code == 0
    output = json.loads(result.stdout)
    assert output["metadata"]["mime_type"] == "text/plain"
    assert output["parsed"]["text"] == "Parsed newsletter body"
    assert output["extraction"]["source_id"] == "alphasignal"
