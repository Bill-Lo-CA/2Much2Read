import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from two_busy_one_miss import cli
from two_busy_one_miss.config import Settings
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
    assert json.loads(result.stdout)["checks"]["discord_test"] == "failed"


def test_reset_delivery_checkpoint_requires_an_explicit_attempt_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "reset_reminder_checkpoint", lambda _, attempt_id: {"status": "ok", "attempt_id": attempt_id})

    result = CliRunner().invoke(cli.app, ["reset-delivery-checkpoint", "--attempt-id", "9"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"status": "ok", "attempt_id": 9}


def test_reset_agenda_checkpoint_requires_an_explicit_delivery_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "reset_agenda_checkpoint", lambda _, delivery_id: {"status": "ok", "delivery_id": delivery_id})

    result = CliRunner().invoke(cli.app, ["reset-agenda-checkpoint", "--delivery-id", "9"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"status": "ok", "delivery_id": 9}
