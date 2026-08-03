import json

from typer.testing import CliRunner

from two_bored_one_made import cli
from two_bored_one_made.config import Settings
from two_read_runtime.discord import DiscordDestination


def test_send_mentions_only_configured_user_ids(monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "Settings",
        lambda: Settings(
            discord_webhook_url="https://discord.com/api/webhooks/123456789012345678/test-webhook-token",
            discord_allowed_mention_ids="123,456",
        ),
    )
    calls: list[tuple[DiscordDestination, str, str, list[str], list[str]]] = []

    def fake_deliver(
        destination: DiscordDestination, content: str, username: str, *, allowed_user_ids: list[str], mention_user_ids: list[str]
    ) -> list[str]:
        calls.append((destination, content, username, allowed_user_ids, mention_user_ids))
        return ["message-id"]

    monkeypatch.setattr(cli, "deliver", fake_deliver)

    result = CliRunner().invoke(
        cli.app,
        ["send", "--message", "Build @everyone <@456>", "--mention", "123"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "status": "ok",
        "discord_message_ids": ["message-id"],
        "delivery_succeeded": 1,
        "delivery_failed": 0,
        "failed_by_error_code": {},
    }
    destination, content, username, allowed_user_ids, mention_user_ids = calls[0]
    assert destination.transport == "webhook"
    assert destination.webhook_url == "https://discord.com/api/webhooks/123456789012345678/test-webhook-token"
    assert (content, username, allowed_user_ids, mention_user_ids) == (
        "Build @\u200beveryone <@\u200b456>",
        "2bored1made",
        ["123"],
        ["123"],
    )


def test_send_rejects_unconfigured_mentions(monkeypatch) -> None:
    monkeypatch.setattr(cli, "Settings", lambda: Settings(discord_allowed_mention_ids="123"))

    result = CliRunner().invoke(cli.app, ["send", "--message", "Build failed", "--mention", "456"])

    assert result.exit_code == 2
    assert "not allowed" in result.output


def test_send_both_reports_a_partial_delivery(monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "Settings",
        lambda: Settings(
            discord_delivery_mode="both",
            discord_webhook_url="https://discord.com/api/webhooks/123456789012345678/test-webhook-token",
            discord_bot_token="token",
            discord_bot_channel_id="123",
        ),
    )

    def fake_deliver(destination: DiscordDestination, *args: object, **kwargs: object) -> list[str]:
        if destination.transport == "bot":
            raise cli.DiscordDeliveryError("DISCORD_BOT_FORBIDDEN")
        return ["webhook-message"]

    monkeypatch.setattr(cli, "deliver", fake_deliver)
    result = CliRunner().invoke(cli.app, ["send", "--message", "Build failed"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "status": "partial",
        "discord_message_ids": ["webhook-message"],
        "delivery_succeeded": 1,
        "delivery_failed": 1,
        "failed_by_error_code": {"DISCORD_BOT_FORBIDDEN": 1},
    }
