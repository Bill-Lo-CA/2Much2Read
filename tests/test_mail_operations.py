from __future__ import annotations

from base64 import urlsafe_b64encode
from pathlib import Path

import pytest

from two_much_two_read import mail_operations
from two_much_two_read.command_models import MailSelector
from two_much_two_read.config import Settings
from two_much_two_read.gmail import display_id
from two_much_two_read.schemas import EmailExtraction


@pytest.mark.parametrize(("length", "truncated"), [(45_000, False), (45_001, True)])
def test_inspect_mail_reports_original_and_input_lengths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, length: int, truncated: bool
) -> None:
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text(
        "sources:\n  - id: alphasignal\n    name: AlphaSignal\n    gmail_query: 'from:alphasignal.ai'\n",
        encoding="utf-8",
    )
    settings = Settings(sources_config_path=sources_path)
    body = "x" * length
    encoded = urlsafe_b64encode(body.encode()).decode().rstrip("=")
    calls: list[tuple[str, bool]] = []

    class FakeGmailClient:
        def list_messages(self, query: str, limit: int) -> list[str]:
            assert (query, limit) == ("from:alphasignal.ai", 100)
            return ["gmail-1"]

        def get_message(self, message_id: str) -> dict[str, object]:
            return {
                "internalDate": "0",
                "labelIds": [],
                "payload": {
                    "mimeType": "text/plain",
                    "headers": [],
                    "body": {"data": encoded},
                },
            }

    class FakeOllamaClient:
        def extract(self, source_id: str, content: str, was_truncated: bool, max_items: int) -> EmailExtraction:
            calls.append((content, was_truncated))
            return EmailExtraction(
                source_id=source_id, newsletter_title="Test", newsletter_date=None, overview_zh_tw="摘要", items=[]
            )

    monkeypatch.setattr(mail_operations, "gmail_client", lambda _: FakeGmailClient())
    monkeypatch.setattr(mail_operations, "create_ollama_client", lambda _: FakeOllamaClient())

    result = mail_operations.inspect_mail(settings, MailSelector(source="alphasignal"), display_id("gmail-1"), 100, True)

    assert result.parsed.original_characters == length
    assert result.parsed.input_characters == min(length, 45_000)
    assert result.parsed.truncated is truncated
    assert result.parsed.text == body[:45_000]
    assert calls == [(body[:45_000], truncated)]
