import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from two_busy_one_miss import cli
from two_busy_one_miss.config import Settings
from two_busy_one_miss.pipeline import AgendaRetryResult, ReminderRetryResult, ReminderRunResult
from two_read_runtime.discord import DiscordDeliveryError


def test_doctor_reports_a_failed_discord_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reminders_path = tmp_path / "reminders.yaml"
    reminders_path.write_text("calendars:\n  - id: primary\n", encoding="utf-8")
    settings = Settings(
        reminders_config_path=reminders_path,
        database_path=tmp_path / "reminders.sqlite3",
        discord_webhook_url="https://discord.example",
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
        discord_webhook_url="https://discord.example",
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


@pytest.mark.parametrize(
    ("command", "operation", "delivery_result"),
    [
        (
            ["run"],
            "run",
            ReminderRunResult(status="failed", sent=0, failed=1, failed_by_error_code={}, expired=0),
        ),
        (
            ["agenda-retry", "2026-07-09"],
            "retry_agenda",
            AgendaRetryResult(status="failed", day="2026-07-09", delivered=0, failed=1, failed_by_error_code={}),
        ),
        (
            ["retry-delivery"],
            "retry_delivery",
            ReminderRetryResult(status="failed", delivered=0, failed=1, failed_by_error_code={}, expired=0),
        ),
    ],
)
def test_delivery_commands_exit_nonzero_when_delivery_fails(
    command: list[str], operation: str, delivery_result: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, operation, lambda *args: delivery_result)

    result = CliRunner().invoke(cli.app, command)

    assert result.exit_code == 1
    assert json.loads(result.stdout)["status"] == "failed"


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
