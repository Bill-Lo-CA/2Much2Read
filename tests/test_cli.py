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


def test_discover_source_list_reads_config_without_gmail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text(
        "sources:\n  - id: alphasignal\n    name: AlphaSignal\n    enabled: false\n"
        "    gmail_query: 'label:newsletter-alphasignal'\n",
        encoding="utf-8",
    )
    settings = Settings(sources_config_path=sources_path)
    monkeypatch.setattr(cli, "Settings", lambda: settings)

    result = CliRunner().invoke(cli.app, ["discover", "--source", "list"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload == {"status": "ok", "source_ids": ["alphasignal"]}


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
