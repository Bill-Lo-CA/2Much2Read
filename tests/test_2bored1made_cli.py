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

    # One destination reached and one missed is still "partial", but a caller that only reads the
    # exit status has to be told something went wrong.
    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "status": "partial",
        "discord_message_ids": ["webhook-message"],
        "delivery_succeeded": 1,
        "delivery_failed": 1,
        "failed_by_error_code": {"DISCORD_BOT_FORBIDDEN": 1},
    }


def test_send_reports_failed_when_no_destination_was_reached(monkeypatch) -> None:
    # The default deployment has one webhook, so "partial" was the only word this command could say
    # about a send that reached nobody - and it said it while exiting zero.
    monkeypatch.setattr(
        cli,
        "Settings",
        lambda: Settings(discord_webhook_url="https://discord.com/api/webhooks/123456789012345678/test-webhook-token"),
    )
    monkeypatch.setattr(cli, "deliver", lambda *args, **kwargs: (_ for _ in ()).throw(cli.DiscordDeliveryError()))

    result = CliRunner().invoke(cli.app, ["send", "--message", "Build failed"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert (payload["status"], payload["delivery_succeeded"], payload["delivery_failed"]) == ("failed", 0, 1)


def test_send_reports_a_broken_configuration_instead_of_a_traceback(monkeypatch) -> None:
    monkeypatch.setattr(cli, "Settings", lambda: Settings(discord_webhook_url="not-a-discord-webhook"))

    result = CliRunner().invoke(cli.app, ["send", "--message", "Build failed"])

    assert result.exit_code == 2
    assert "DISCORD_WEBHOOK_INVALID" in result.output
    assert "Traceback" not in result.output


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
    # Pinned together with the file check, so a fix that simply stops ever saying "missing" cannot
    # pass, and so the two can never again describe different worlds in one report.
    assert payload["checks"]["nudges_file"] == "not_created"
    assert payload["status"] == "warning"


def test_doctor_does_not_read_a_bad_value_as_a_missing_file(tmp_path, monkeypatch) -> None:
    # "not found" is ordinary English, and a validation error quotes back whatever the operator
    # wrote. Deciding missingness by searching the rendered message reported this file - present,
    # readable, and named in the same output as ok - as missing, sending the operator to create a
    # file that already exists instead of to the timezone that actually broke it.
    monkeypatch.setattr(cli, "env_file", lambda _: tmp_path / "absent.env")
    body = "timezone: not found\nnudges: []\n"
    (tmp_path / "nudges.yaml").write_text(body, encoding="utf-8")
    (tmp_path / "nudges.yaml").chmod(0o600)
    monkeypatch.setattr(cli, "Settings", lambda: _nudge_settings(tmp_path, body))

    result = CliRunner().invoke(cli.app, ["doctor"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["checks"]["nudges"] == "invalid"
    assert payload["checks"]["nudges_file"] == "ok", "the file is there, and the report has to agree"


NUDGE_WITH_MENTION = "nudges:\n  - id: stretch\n    message: hi\n    at: ['09:00']\n    total_sends: 3\n    user_id: '456'\n"


def test_doctor_reports_a_nudge_whose_mention_is_not_allowed(tmp_path, monkeypatch) -> None:
    # A configuration that loads is not one that works: this nudge raises NUDGE_MENTION_NOT_ALLOWED
    # the moment its time comes, and doctor used to call the whole thing healthy.
    monkeypatch.setattr(cli, "env_file", lambda _: tmp_path / "absent.env")
    monkeypatch.setattr(
        cli,
        "Settings",
        lambda: _nudge_settings(tmp_path, NUDGE_WITH_MENTION, discord_allowed_mention_ids="123"),
    )

    result = CliRunner().invoke(cli.app, ["doctor"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["checks"]["nudges"] == "ok"
    assert payload["checks"]["nudges_deliverable"] == "unusable"
    assert payload["nudges_unusable"] == {"stretch": "NUDGE_MENTION_NOT_ALLOWED"}
    assert payload["status"] == "warning"


def test_doctor_does_not_read_a_nudge_id_as_a_verdict(tmp_path, monkeypatch) -> None:
    # Nudge ids are the operator's, and "bot" is as valid an id as "stretch". Naming the failing
    # nudge in the check itself put that word into the vocabulary the healthy/warning verdict reads,
    # so this nudge - which can never be delivered - was reported as a healthy installation.
    #
    # Every other input is deliberately healthy. A missing env file or a world-readable YAML would
    # make this a warning on its own, and the test would then pass without the bug being fixed.
    env_path = tmp_path / ".2bored1made.env"
    env_path.write_text("DISCORD_WEBHOOK_URL=ignored\n", encoding="utf-8")
    env_path.chmod(0o600)
    monkeypatch.setattr(cli, "env_file", lambda _: env_path)
    body = NUDGE_WITH_MENTION.replace("id: stretch", "id: bot")
    # Written and tightened here so the rewrite inside the settings factory inherits the mode.
    (tmp_path / "nudges.yaml").write_text(body, encoding="utf-8")
    (tmp_path / "nudges.yaml").chmod(0o600)
    monkeypatch.setattr(
        cli,
        "Settings",
        lambda: _nudge_settings(tmp_path, body, discord_allowed_mention_ids="123"),
    )

    result = CliRunner().invoke(cli.app, ["doctor"])

    payload = json.loads(result.stdout)
    assert payload["checks"]["nudges_deliverable"] == "unusable"
    assert payload["nudges_unusable"] == {"bot": "NUDGE_MENTION_NOT_ALLOWED"}
    assert payload["status"] == "warning", f"the only unhealthy input is the nudge: {payload['checks']}"


def test_doctor_is_happy_once_the_mention_is_allowed(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".2bored1made.env"
    env_path.write_text("DISCORD_WEBHOOK_URL=ignored\n", encoding="utf-8")
    env_path.chmod(0o600)
    monkeypatch.setattr(cli, "env_file", lambda _: env_path)
    monkeypatch.setattr(
        cli,
        "Settings",
        lambda: _nudge_settings(tmp_path, NUDGE_WITH_MENTION, discord_allowed_mention_ids="456"),
    )

    result = CliRunner().invoke(cli.app, ["doctor"])

    payload = json.loads(result.stdout)
    assert payload["checks"]["nudges_deliverable"] == "ok"
    assert "nudges_unusable" not in payload, "nothing to report is reported by saying nothing"


def test_doctor_ignores_a_disabled_nudge(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".2bored1made.env"
    env_path.write_text("DISCORD_WEBHOOK_URL=ignored\n", encoding="utf-8")
    env_path.chmod(0o600)
    monkeypatch.setattr(cli, "env_file", lambda _: env_path)
    monkeypatch.setattr(
        cli,
        "Settings",
        lambda: _nudge_settings(tmp_path, NUDGE_WITH_MENTION.replace("total_sends: 3", "total_sends: 3\n    enabled: false")),
    )

    result = CliRunner().invoke(cli.app, ["doctor"])

    assert json.loads(result.stdout)["checks"]["nudges_deliverable"] == "ok"


def test_doctor_cannot_judge_nudges_it_could_not_load(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "env_file", lambda _: tmp_path / "absent.env")
    monkeypatch.setattr(cli, "Settings", lambda: _nudge_settings(tmp_path, "nudges:\n  - id: broken\n"))

    result = CliRunner().invoke(cli.app, ["doctor"])

    payload = json.loads(result.stdout)
    assert payload["checks"]["nudges"] == "invalid"
    assert payload["checks"]["nudges_deliverable"] == "unknown"


def test_status_and_dry_run_read_an_uninitialized_database_as_empty(tmp_path, monkeypatch) -> None:
    # The database file is created before its schema is, so a first run that died between the two
    # leaves a real, zero-byte database behind. Reading it raised sqlite3.OperationalError, which is
    # not a ValueError and so reached the operator as a traceback from both commands.
    from two_read_runtime.permissions import prepare_private_file

    prepare_private_file(tmp_path / "2bored1made.sqlite3")
    monkeypatch.setattr(cli, "Settings", lambda: _nudge_settings(tmp_path, NUDGE_WITH_MENTION))

    status_result = CliRunner().invoke(cli.app, ["status"])
    dry_run_result = CliRunner().invoke(cli.app, ["run", "--dry-run"])

    assert status_result.exit_code == 0, status_result.output
    assert dry_run_result.exit_code == 0, dry_run_result.output
    nudge = json.loads(status_result.stdout)["nudges"][0]
    assert nudge["delivered"] == 0
    assert nudge["remaining"] == 3
