import json

from typer.testing import CliRunner

from two_bored_one_made import cli
from two_bored_one_made.config import Settings


def test_send_mentions_only_configured_user_ids(monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "Settings",
        lambda: Settings(discord_webhook_url="https://discord.example", discord_allowed_mention_ids="123,456"),
    )
    calls: list[tuple[str, str, str, list[str]]] = []

    def fake_deliver(webhook_url: str, content: str, username: str, *, allowed_user_ids: list[str]) -> list[str]:
        calls.append((webhook_url, content, username, allowed_user_ids))
        return ["message-id"]

    monkeypatch.setattr(cli, "deliver", fake_deliver)

    result = CliRunner().invoke(
        cli.app,
        ["send", "--message", "Build @everyone <@456>", "--mention", "123"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"status": "ok", "discord_message_ids": ["message-id"]}
    assert calls == [
        (
            "https://discord.example",
            "<@123> Build @\u200beveryone <@\u200b456>",
            "2bored1made",
            ["123"],
        )
    ]


def test_send_rejects_unconfigured_mentions(monkeypatch) -> None:
    monkeypatch.setattr(cli, "Settings", lambda: Settings(discord_allowed_mention_ids="123"))

    result = CliRunner().invoke(cli.app, ["send", "--message", "Build failed", "--mention", "456"])

    assert result.exit_code == 2
    assert "not allowed" in result.output
