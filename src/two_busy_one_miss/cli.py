from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from datetime import date, datetime
from typing import Annotated

import typer
from pydantic import BaseModel

from two_read_runtime.discord import DiscordDeliveryError, deliver
from two_read_runtime.locking import ProcessLock
from two_read_runtime.oauth import token_status
from two_read_runtime.paths import directory_is_creatable
from two_read_runtime.permissions import runtime_permission_checks
from two_read_runtime.progress import run_with_elapsed
from two_read_runtime.settings_validation import unknown_env_keys

from .config import Settings, load_reminders, settings_env_file
from .google_calendar import credentials
from .pipeline import (
    agenda,
    calendar_client,
    discover,
    next_day_agenda,
    reset_agenda_checkpoint,
    reset_reminder_checkpoint,
    retry_agenda,
    retry_delivery,
    run,
    test_rules,
)
from .todo_prompt import ask_todo
from .todo_web import DEFAULT_PORT, serve
from .todos import TodoAddResult, add_todo, clean_title, complete_todo, list_todos, parse_schedule, remove_todo, reopen_todo

app = typer.Typer(no_args_is_help=True)
auth_app = typer.Typer(no_args_is_help=True)
calendars_app = typer.Typer(no_args_is_help=True)
rules_app = typer.Typer(no_args_is_help=True)
todo_app = typer.Typer()
app.add_typer(auth_app, name="auth")
app.add_typer(calendars_app, name="calendars")
app.add_typer(rules_app, name="rules")
app.add_typer(todo_app, name="todo")

_HEALTHY_CHECKS = {"ok", "not_created", "webhook", "bot", "both"}


def _config_error_status(error: Exception) -> str:
    return "missing" if "not found" in str(error).lower() else "invalid"


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def emit(result: BaseModel | Mapping[str, object]) -> None:
    values = result.model_dump(mode="json", exclude_none=True) if isinstance(result, BaseModel) else result
    typer.echo(json.dumps(values, ensure_ascii=False, default=_json_default))


def emit_delivery_result(result: BaseModel) -> None:
    emit(result)
    if getattr(result, "status", "ok") in {"partial", "failed"}:
        raise typer.Exit(code=1)


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
    emit(
        run_with_elapsed(
            "2busy1miss calendars list",
            lambda: {"status": "ok", "calendars": calendar_client(settings, config).list_calendars()},
        )
    )


@app.command()
def doctor(send_test: Annotated[bool, typer.Option()] = False) -> None:
    settings = Settings()
    checks: dict[str, str] = {}
    try:
        load_reminders(settings.reminders_config_path)
        checks["config"] = "ok"
    except (OSError, ValueError) as error:
        checks["config"] = _config_error_status(error)
    checks["google_calendar_token"] = token_status(
        settings.google_calendar_token_path,
        ("https://www.googleapis.com/auth/calendar.readonly",),
    )
    checks.update(
        runtime_permission_checks(
            "2busy1miss",
            config_path=settings.reminders_config_path,
            credentials_path=settings.google_calendar_credentials_path,
            token_path=settings.google_calendar_token_path,
            database_path=settings.database_path,
            lock_path=settings.lock_path,
        )
    )
    checks["database_directory"] = "ok" if directory_is_creatable(settings.database_path.parent) else "not_writable"
    try:
        destinations = settings.discord_destinations()
        checks["discord"] = settings.discord_delivery_mode
    except DiscordDeliveryError:
        destinations = []
        checks["discord"] = (
            "missing" if settings.discord_delivery_mode == "webhook" and not settings.discord_webhook_url else "invalid"
        )
    if send_test:
        if not destinations:
            checks["discord_test"] = "missing"
        else:
            for destination in destinations:
                try:
                    deliver(destination, "2busy1miss connectivity test", settings.discord_username)
                    checks[f"discord_test_{destination.transport}"] = "ok"
                except DiscordDeliveryError:
                    checks[f"discord_test_{destination.transport}"] = "failed"
    unknown_keys = unknown_env_keys(settings_env_file(), type(settings).model_fields)
    checks["env_keys"] = "ok" if not unknown_keys else "unknown"
    status = "ok" if all(value in _HEALTHY_CHECKS for value in checks.values()) else "warning"
    payload: dict[str, object] = {"status": status, "checks": checks}
    if unknown_keys:
        payload["unknown_env_keys"] = unknown_keys
    emit(payload)


@app.command("discover")
def discover_command(days: Annotated[int, typer.Option("--days", min=1, max=30)] = 7) -> None:
    emit(run_with_elapsed("2busy1miss discover", lambda: discover(Settings(), days)))


@rules_app.command("test")
def rules_test(days: Annotated[int, typer.Option("--days", min=1, max=30)] = 7) -> None:
    emit(run_with_elapsed("2busy1miss rules test", lambda: test_rules(Settings(), days)))


@app.command("run")
def run_command(dry_run: Annotated[bool, typer.Option()] = False) -> None:
    try:
        result = run_with_elapsed("2busy1miss run", lambda: run(Settings(), dry_run))
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    emit_delivery_result(result)


@app.command("agenda")
def agenda_command(
    day: Annotated[str, typer.Argument()],
    dry_run: Annotated[bool, typer.Option()] = False,
    force: Annotated[bool, typer.Option()] = False,
) -> None:
    try:
        parsed = date.fromisoformat(day)
    except ValueError as error:
        raise typer.BadParameter("date must use YYYY-MM-DD") from error
    emit_delivery_result(run_with_elapsed("2busy1miss agenda", lambda: agenda(Settings(), parsed, dry_run, force)))


@app.command("agenda-next-day")
def agenda_next_day_command(
    dry_run: Annotated[bool, typer.Option()] = False,
    force: Annotated[bool, typer.Option()] = False,
    scheduled: Annotated[bool, typer.Option()] = False,
) -> None:
    emit_delivery_result(
        run_with_elapsed("2busy1miss agenda next day", lambda: next_day_agenda(Settings(), dry_run, force, scheduled=scheduled))
    )


@app.command("agenda-retry")
def agenda_retry_command(day: Annotated[str, typer.Argument()]) -> None:
    try:
        parsed = date.fromisoformat(day)
    except ValueError as error:
        raise typer.BadParameter("date must use YYYY-MM-DD") from error
    emit_delivery_result(run_with_elapsed("2busy1miss agenda retry", lambda: retry_agenda(Settings(), parsed)))


@app.command("retry-delivery")
def retry_delivery_command() -> None:
    emit_delivery_result(run_with_elapsed("2busy1miss retry delivery", lambda: retry_delivery(Settings())))


@app.command("reset-delivery-checkpoint")
def reset_delivery_checkpoint(delivery_id: Annotated[int, typer.Option("--delivery-id", min=1)]) -> None:
    try:
        emit(reset_reminder_checkpoint(Settings(), delivery_id))
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error


@app.command("reset-agenda-checkpoint")
def reset_agenda_checkpoint_command(delivery_id: Annotated[int, typer.Option("--delivery-id", min=1)]) -> None:
    try:
        emit(reset_agenda_checkpoint(Settings(), delivery_id))
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error


def _parsed_day(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise typer.BadParameter("date must use YYYY-MM-DD") from error


def _how_often(result: TodoAddResult) -> str:
    if result.period != "day":
        return f"once a {result.period}"
    return "every day" if len(result.weekdays) == 7 else ", ".join(result.weekdays)


def _add_todo_asked(settings: Settings, on: str | None, every: str | None) -> None:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise ValueError("a title is needed outside a terminal, where there is nobody to ask")
    # Before anything is asked, so that nobody types a title only to have it refused for this.
    parse_schedule(on, every or "day")
    try:
        answers = ask_todo(clean_title, on, every)
    except KeyboardInterrupt:
        answers = None
    if answers is None:
        typer.echo("Cancelled; nothing was added.", err=True)
        raise typer.Exit(code=1)
    title, schedule = answers
    result = add_todo(settings, title, schedule.on, period=schedule.period)
    typer.echo(f"Added to-do {result.id}: {result.title} ({_how_often(result)})")


@todo_app.callback(invoke_without_command=True)
def todo_main(context: typer.Context) -> None:
    """Add a to-do by answering prompts, or use one of the commands below."""
    if context.invoked_subcommand is None:
        try:
            _add_todo_asked(Settings(), None, None)
        except ValueError as error:
            raise typer.BadParameter(str(error)) from error


@todo_app.command("add")
def todo_add_command(
    title: Annotated[str | None, typer.Argument(help="Leave it out in a terminal to be asked for it and for how often.")] = None,
    on: Annotated[str | None, typer.Option("--on", help="Weekdays such as mon,wed,fri; every day when left out.")] = None,
    every: Annotated[
        str | None, typer.Option("--every", help="day, week or month. Weekly or monthly is once in that time, on any day.")
    ] = None,
) -> None:
    try:
        settings = Settings()
        if title is None:
            _add_todo_asked(settings, on, every)
        else:
            emit(add_todo(settings, title, on, period=every or "day"))
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error


@todo_app.command("list")
def todo_list_command(day: Annotated[str | None, typer.Option("--day")] = None) -> None:
    parsed = _parsed_day(day)
    try:
        emit(list_todos(Settings(), parsed))
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error


@todo_app.command("done")
def todo_done_command(
    todo_id: Annotated[int, typer.Argument()], day: Annotated[str | None, typer.Option("--day")] = None
) -> None:
    parsed = _parsed_day(day)
    try:
        emit(complete_todo(Settings(), todo_id, parsed))
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error


@todo_app.command("undo")
def todo_undo_command(
    todo_id: Annotated[int, typer.Argument()], day: Annotated[str | None, typer.Option("--day")] = None
) -> None:
    parsed = _parsed_day(day)
    try:
        emit(reopen_todo(Settings(), todo_id, parsed))
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error


@todo_app.command("remove")
def todo_remove_command(todo_id: Annotated[int, typer.Argument()]) -> None:
    try:
        emit(remove_todo(Settings(), todo_id))
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error


@todo_app.command("serve")
def todo_serve_command(
    host: Annotated[str, typer.Option("--host", help="This machine's Tailscale address, from `tailscale ip -4`.")],
    port: Annotated[int, typer.Option("--port", min=1024, max=65535)] = DEFAULT_PORT,
) -> None:
    try:
        serve(Settings(), host, port)
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    except OSError as error:
        # Tailscale not up yet is the expected case, and the service tries again by itself.
        typer.echo(f"cannot listen on {host}:{port}: {error.strerror}", err=True)
        raise typer.Exit(code=1) from error
