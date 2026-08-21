import json

from typer.testing import CliRunner

from two_bored_one_made import cli, pipeline
from two_bored_one_made.config import Settings
from two_read_runtime.discord import DiscordDestination


def _nudge_settings(tmp_path, body: str, **overrides):
    (tmp_path / "nudges.yaml").write_text(body, encoding="utf-8")
    values = {
        "discord_webhook_url": "https://discord.com/api/webhooks/123456789012345678/test-webhook-token",
        "database_path": tmp_path / "2bored1made.sqlite3",
        "lock_path": tmp_path / "2bored1made.lock",
        "nudges_config_path": tmp_path / "nudges.yaml",
    }
    values.update(overrides)
    return Settings(**values)


def test_doctor_validates_env_and_discord_without_sending(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".2bored1made.env"
    env_path.write_text("DISCORD_WEBHOOK_URL=ignored\n", encoding="utf-8")
    env_path.chmod(0o600)
    secret = "test-webhook-token-that-must-not-appear"
    monkeypatch.setattr(cli, "env_file", lambda _: env_path)
    (tmp_path / "nudges.yaml").write_text("nudges: []\n", encoding="utf-8")
    (tmp_path / "nudges.yaml").chmod(0o600)
    monkeypatch.setattr(
        cli,
        "Settings",
        lambda: _nudge_settings(
            tmp_path,
            "nudges: []\n",
            discord_webhook_url=f"https://discord.com/api/webhooks/123456789012345678/{secret}",
        ),
    )
    monkeypatch.setattr(cli, "deliver", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("doctor sent")))

    result = CliRunner().invoke(cli.app, ["doctor"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["checks"]["env_file"] == "ok"
    assert payload["checks"]["discord"] == "webhook"
    assert payload["checks"]["nudges"] == "ok"
    assert secret not in result.stdout


def test_doctor_warns_for_unsafe_env_file(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".2bored1made.env"
    env_path.write_text("DISCORD_WEBHOOK_URL=ignored\n", encoding="utf-8")
    env_path.chmod(0o644)
    monkeypatch.setattr(cli, "env_file", lambda _: env_path)
    monkeypatch.setattr(cli, "Settings", lambda: _nudge_settings(tmp_path, "nudges: []\n"))

    result = CliRunner().invoke(cli.app, ["doctor"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "warning"
    assert payload["checks"]["env_file"] == "unsafe"
    assert payload["checks"]["discord"] == "webhook"


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


ONE_NUDGE = "nudges:\n  - id: stretch\n    message: hi\n    at: ['00:01']\n    total_sends: 2\n"


def test_run_exits_nonzero_when_nothing_was_delivered(tmp_path, monkeypatch) -> None:
    # A scheduled unit that exits zero on a failed delivery reports success to systemd, so this
    # command sets the exit status from the delivery result rather than from having run at all.
    monkeypatch.setattr(cli, "Settings", lambda: _nudge_settings(tmp_path, ONE_NUDGE))
    monkeypatch.setattr(pipeline, "deliver", lambda *args, **kwargs: (_ for _ in ()).throw(cli.DiscordDeliveryError()))

    result = CliRunner().invoke(cli.app, ["run"])

    assert result.exit_code == 1
    assert json.loads(result.stdout)["status"] == "failed"


def test_run_exits_zero_when_a_nudge_is_delivered(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "Settings", lambda: _nudge_settings(tmp_path, ONE_NUDGE))
    monkeypatch.setattr(pipeline, "deliver", lambda *args, **kwargs: ["message-id"])

    result = CliRunner().invoke(cli.app, ["run"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert (payload["status"], payload["sent"]) == ("ok", 1)


def test_run_reports_a_broken_configuration_instead_of_a_traceback(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "Settings", lambda: _nudge_settings(tmp_path, "nudges:\n  - id: a\n"))

    result = CliRunner().invoke(cli.app, ["run"])

    assert result.exit_code == 2
    assert "message" in result.output


def test_status_reports_the_countdown(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "Settings", lambda: _nudge_settings(tmp_path, ONE_NUDGE))
    monkeypatch.setattr(pipeline, "deliver", lambda *args, **kwargs: ["message-id"])
    CliRunner().invoke(cli.app, ["run"])

    result = CliRunner().invoke(cli.app, ["status"])

    assert result.exit_code == 0
    nudge = json.loads(result.stdout)["nudges"][0]
    assert (nudge["id"], nudge["delivered"], nudge["remaining"], nudge["done"]) == ("stretch", 1, 1, False)


def test_reset_clears_the_history(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "Settings", lambda: _nudge_settings(tmp_path, ONE_NUDGE))
    monkeypatch.setattr(pipeline, "deliver", lambda *args, **kwargs: ["message-id"])
    CliRunner().invoke(cli.app, ["run"])

    result = CliRunner().invoke(cli.app, ["reset", "--nudge", "stretch"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["cleared"] == 1


def test_reset_rejects_an_unknown_nudge(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "Settings", lambda: _nudge_settings(tmp_path, ONE_NUDGE))

    result = CliRunner().invoke(cli.app, ["reset", "--nudge", "ghost"])

    assert result.exit_code == 2
    assert "unknown nudge id" in result.output


def test_doctor_reports_a_missing_nudges_file_without_failing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "env_file", lambda _: tmp_path / "absent.env")
    monkeypatch.setattr(
        cli,
        "Settings",
        lambda: Settings(
            discord_webhook_url="https://discord.com/api/webhooks/123456789012345678/test-webhook-token",
            database_path=tmp_path / "2bored1made.sqlite3",
            lock_path=tmp_path / "2bored1made.lock",
            nudges_config_path=tmp_path / "absent.yaml",
        ),
    )

    result = CliRunner().invoke(cli.app, ["doctor"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["checks"]["nudges"] == "missing"
    assert payload["status"] == "warning"
