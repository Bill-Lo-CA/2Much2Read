import json
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from two_busy_one_miss import cli
from two_busy_one_miss.config import Settings
from two_busy_one_miss.pipeline import AgendaDeliveryResult, AgendaRetryResult, ReminderRetryResult, ReminderRunResult
from two_read_runtime.discord import DiscordDeliveryError


def test_doctor_reports_a_failed_discord_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reminders_path = tmp_path / "reminders.yaml"
    reminders_path.write_text("calendars:\n  - id: primary\n", encoding="utf-8")
    settings = Settings(
        reminders_config_path=reminders_path,
        database_path=tmp_path / "reminders.sqlite3",
        discord_webhook_url="https://discord.com/api/webhooks/123456789012345678/test-webhook-token",
    )

    def offline(*args: object, **kwargs: object) -> None:
        raise DiscordDeliveryError("offline")

    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(cli, "deliver", offline)

    result = CliRunner().invoke(cli.app, ["doctor", "--send-test"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["checks"]["discord_test_webhook"] == "failed"


def test_doctor_tests_each_both_destination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reminders_path = tmp_path / "reminders.yaml"
    reminders_path.write_text("calendars:\n  - id: primary\n", encoding="utf-8")
    settings = Settings(
        reminders_config_path=reminders_path,
        database_path=tmp_path / "reminders.sqlite3",
        discord_delivery_mode="both",
        discord_webhook_url="https://discord.com/api/webhooks/123456789012345678/test-webhook-token",
        discord_bot_token="token",
        discord_bot_channel_id="123",
    )
    sent: list[str] = []
    monkeypatch.setattr(cli, "Settings", lambda: settings)

    def send(destination: object, *args: object) -> list[str]:
        transport = destination.transport  # type: ignore[attr-defined]
        sent.append(transport)
        if transport == "bot":
            raise DiscordDeliveryError("bot unavailable")
        return [transport]

    monkeypatch.setattr(cli, "deliver", send)

    result = CliRunner().invoke(cli.app, ["doctor", "--send-test"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["checks"].get("discord") == "both"
    assert json.loads(result.stdout)["checks"]["discord_test_webhook"] == "ok"
    assert json.loads(result.stdout)["checks"]["discord_test_bot"] == "failed"
    assert sent == ["webhook", "bot"]


def test_doctor_redacts_missing_config_path_and_reports_custom_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret_path = tmp_path / "calendar-client-secret-must-not-appear.yaml"
    settings = Settings(
        reminders_config_path=secret_path,
        database_path=tmp_path / "custom.sqlite3",
        lock_path=tmp_path / "custom.lock",
        discord_webhook_url="https://discord.com/api/webhooks/123456789012345678/test-webhook-token",
    )
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(
        cli,
        "runtime_permission_checks",
        lambda *args, **kwargs: {
            "config_dir": "ok",
            "token_dir": "ok",
            "data_dir": "ok",
            "env_file": "ok",
            "config_file": "missing",
            "client_secret": "missing",
            "token_file": "missing",
            "database": "not_created",
            "lock_file": "not_created",
            "runtime_paths": "custom",
        },
    )

    result = CliRunner().invoke(cli.app, ["doctor"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "warning"
    assert payload["checks"]["config"] == "missing"
    assert payload["checks"]["runtime_paths"] == "custom"
    assert str(secret_path) not in result.stdout


@pytest.mark.parametrize(
    ("command", "operation", "delivery_result", "expected_status"),
    [
        (
            ["run"],
            "run",
            ReminderRunResult(status="failed", sent=0, failed=1, failed_by_error_code={}, expired=0),
            "failed",
        ),
        (
            ["agenda-retry", "2026-07-09"],
            "retry_agenda",
            AgendaRetryResult(status="failed", day="2026-07-09", delivered=0, failed=1, failed_by_error_code={}),
            "failed",
        ),
        (
            ["retry-delivery"],
            "retry_delivery",
            ReminderRetryResult(status="failed", delivered=0, failed=1, failed_by_error_code={}, expired=0),
            "failed",
        ),
        # The two the timers actually execute. Both reported a failed delivery and exited zero, so
        # a systemd unit that could not reach Discord still looked like a clean run.
        (
            ["agenda", "2026-07-09"],
            "agenda",
            AgendaDeliveryResult(status="partial", sent=0, day=date(2026, 7, 9)),
            "partial",
        ),
        (
            ["agenda-next-day", "--scheduled"],
            "next_day_agenda",
            AgendaDeliveryResult(status="partial", sent=0, day=date(2026, 7, 9)),
            "partial",
        ),
    ],
)
def test_delivery_commands_exit_nonzero_when_delivery_fails(
    command: list[str], operation: str, delivery_result: object, expected_status: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, operation, lambda *args, **kwargs: delivery_result)

    result = CliRunner().invoke(cli.app, command)

    assert result.exit_code == 1
    assert json.loads(result.stdout)["status"] == expected_status


def test_reset_delivery_checkpoint_requires_an_explicit_delivery_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "reset_reminder_checkpoint", lambda _, delivery_id: {"status": "ok", "delivery_id": delivery_id})

    result = CliRunner().invoke(cli.app, ["reset-delivery-checkpoint", "--delivery-id", "9"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"status": "ok", "delivery_id": 9}


def test_reset_agenda_checkpoint_requires_an_explicit_delivery_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "reset_agenda_checkpoint", lambda _, delivery_id: {"status": "ok", "delivery_id": delivery_id})

    result = CliRunner().invoke(cli.app, ["reset-agenda-checkpoint", "--delivery-id", "9"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"status": "ok", "delivery_id": 9}


def test_doctor_names_a_misspelled_environment_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_home: Path) -> None:
    env_path = isolated_home / ".config" / "2much2read-runtime" / ".2busy1miss.env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text("DISCORD_WEBHOOK_URL=ignored\nREMINDER_TIMEZON=typo\n", encoding="utf-8")
    reminders_path = tmp_path / "reminders.yaml"
    reminders_path.write_text("calendars:\n  - id: primary\n", encoding="utf-8")
    monkeypatch.setattr(
        cli,
        "Settings",
        lambda: Settings(
            reminders_config_path=reminders_path,
            database_path=tmp_path / "reminders.sqlite3",
            lock_path=tmp_path / "reminders.lock",
            discord_webhook_url="https://discord.com/api/webhooks/123456789012345678/test-webhook-token",
        ),
    )

    result = CliRunner().invoke(cli.app, ["doctor"])

    payload = json.loads(result.stdout)
    assert payload["status"] == "warning"
    assert payload["checks"]["env_keys"] == "unknown"
    # AGENDA_SCHEDULE_TIME really is a setting here, unlike the newsletter tool's DIGEST_SCHEDULE_*
    assert payload["unknown_env_keys"] == ["REMINDER_TIMEZON"]
