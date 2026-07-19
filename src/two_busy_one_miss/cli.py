from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import date, datetime
from typing import Annotated

import typer
from pydantic import BaseModel

from two_read_runtime.discord import deliver
from two_read_runtime.locking import ProcessLock
from two_read_runtime.oauth import token_status

from .config import Settings, load_reminders
from .google_calendar import credentials
from .pipeline import agenda, calendar_client, discover, next_day_agenda, retry_agenda, retry_delivery, run, test_rules

app = typer.Typer(no_args_is_help=True)
auth_app = typer.Typer(no_args_is_help=True)
calendars_app = typer.Typer(no_args_is_help=True)
rules_app = typer.Typer(no_args_is_help=True)
app.add_typer(auth_app, name="auth")
app.add_typer(calendars_app, name="calendars")
app.add_typer(rules_app, name="rules")


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def emit(result: BaseModel | Mapping[str, object]) -> None:
    values = result.model_dump(mode="json", exclude_none=True) if isinstance(result, BaseModel) else result
    typer.echo(json.dumps(values, ensure_ascii=False, default=_json_default))


@auth_app.command("calendar")
def auth_calendar() -> None:
    settings = Settings()
    with ProcessLock(settings.lock_path):
        credentials(
            settings.google_calendar_credentials_path,
            settings.google_calendar_token_path,
            settings.google_calendar_oauth_callback_port,
            interactive=True,
        )
    emit({"status": "ok"})


@calendars_app.command("list")
def calendars_list() -> None:
    settings = Settings()
    config = load_reminders(settings.reminders_config_path)
    emit({"status": "ok", "calendars": calendar_client(settings, config).list_calendars()})


@app.command()
def doctor(send_test: Annotated[bool, typer.Option()] = False) -> None:
    settings = Settings()
    checks: dict[str, str] = {}
    try:
        load_reminders(settings.reminders_config_path)
        checks["config"] = "ok"
    except Exception as error:
        checks["config"] = str(error)
    checks["google_calendar_token"] = token_status(
        settings.google_calendar_token_path,
        ("https://www.googleapis.com/auth/calendar.readonly",),
    )
    parent = settings.database_path.parent
    checks["database_directory"] = "ok" if parent.exists() and os.access(parent, os.W_OK) else "not_writable"
    checks["discord"] = "configured" if settings.discord_webhook_url else "missing"
    if send_test and settings.discord_webhook_url:
        deliver(settings.discord_webhook_url, "2busy1miss connectivity test", settings.discord_username)
        checks["discord_test"] = "ok"
    status = "ok" if all(value in {"ok", "configured"} for value in checks.values()) else "warning"
    emit({"status": status, "checks": checks})


@app.command("discover")
def discover_command(days: Annotated[int, typer.Option("--days", min=1, max=30)] = 7) -> None:
    emit(discover(Settings(), days))


@rules_app.command("test")
def rules_test(days: Annotated[int, typer.Option("--days", min=1, max=30)] = 7) -> None:
    emit(test_rules(Settings(), days))


@app.command("run")
def run_command(dry_run: Annotated[bool, typer.Option()] = False) -> None:
    emit(run(Settings(), dry_run))


@app.command("agenda")
def agenda_command(
    day: Annotated[str, typer.Argument()],
    dry_run: Annotated[bool, typer.Option()] = False,
) -> None:
    try:
        parsed = date.fromisoformat(day)
    except ValueError as error:
        raise typer.BadParameter("date must use YYYY-MM-DD") from error
    emit(agenda(Settings(), parsed, dry_run))


@app.command("agenda-next-day")
def agenda_next_day_command(
    dry_run: Annotated[bool, typer.Option()] = False,
    force: Annotated[bool, typer.Option()] = False,
    scheduled: Annotated[bool, typer.Option()] = False,
) -> None:
    emit(next_day_agenda(Settings(), dry_run, force, scheduled=scheduled))


@app.command("agenda-retry")
def agenda_retry_command(day: Annotated[str, typer.Argument()]) -> None:
    try:
        parsed = date.fromisoformat(day)
    except ValueError as error:
        raise typer.BadParameter("date must use YYYY-MM-DD") from error
    emit(retry_agenda(Settings(), parsed))


@app.command("retry-delivery")
def retry_delivery_command() -> None:
    emit(retry_delivery(Settings()))
