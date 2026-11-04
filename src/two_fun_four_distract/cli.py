from __future__ import annotations

import json
from datetime import date
from typing import Annotated

import typer
from pydantic import BaseModel

from two_read_runtime.discord import DiscordDeliveryError
from two_read_runtime.paths import config_dir, data_dir, directory_is_creatable
from two_read_runtime.permissions import private_directory_status, private_file_status, sqlite_files_status
from two_read_runtime.settings_validation import unknown_env_keys

from .config import Settings, settings_env_file
from .pipeline import preview_summary, report_text, send_summary
from .receiver import DEFAULT_PORT, serve

app = typer.Typer(no_args_is_help=True)
_HEALTHY_CHECKS = {"ok", "webhook", "bot", "both", "not_created"}


def _emit(result: BaseModel | dict[str, object]) -> None:
    values = result.model_dump(mode="json") if isinstance(result, BaseModel) else result
    typer.echo(json.dumps(values, ensure_ascii=False))


def _parsed_day(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise typer.BadParameter("date must use YYYY-MM-DD") from error


@app.callback()
def main() -> None:
    pass


@app.command()
def doctor() -> None:
    """Report whether the settings, files and Discord destination are usable."""
    settings = Settings()
    checks: dict[str, str] = {
        "env_file": private_file_status(settings_env_file()),
        "config_dir": private_directory_status(config_dir(), missing_ok=True),
        "data_root": private_directory_status(data_dir(), missing_ok=True),
        "data_dir": private_directory_status(settings.database_path.parent, missing_ok=True),
        "database": sqlite_files_status(settings.database_path),
        "lock_file": private_file_status(settings.lock_path, missing_ok=True),
        "database_directory": "ok" if directory_is_creatable(settings.database_path.parent) else "not_writable",
    }
    try:
        settings.discord_destinations()
        checks["discord"] = settings.discord_delivery_mode
    except DiscordDeliveryError:
        checks["discord"] = (
            "missing" if settings.discord_delivery_mode == "webhook" and not settings.discord_webhook_url else "invalid"
        )
    unknown_keys = unknown_env_keys(settings_env_file(), type(settings).model_fields)
    checks["env_keys"] = "ok" if not unknown_keys else "unknown"
    payload: dict[str, object] = {
        "status": "ok" if all(value in _HEALTHY_CHECKS for value in checks.values()) else "warning",
        "checks": checks,
    }
    if unknown_keys:
        payload["unknown_env_keys"] = unknown_keys
    _emit(payload)


@app.command()
def report(day: Annotated[str | None, typer.Option("--day", help="YYYY-MM-DD; today when left out.")] = None) -> None:
    """Show one day's time, read without changing anything."""
    parsed = _parsed_day(day)
    try:
        typer.echo(report_text(Settings(), parsed)[1])
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error


@app.command()
def summary(
    day: Annotated[str | None, typer.Option("--day", help="YYYY-MM-DD; yesterday when left out.")] = None,
    dry_run: Annotated[bool, typer.Option()] = False,
    force: Annotated[bool, typer.Option(help="Send it again even if it was already sent.")] = False,
) -> None:
    """Send one day's summary to Discord, once per destination."""
    parsed = _parsed_day(day)
    settings = Settings()
    try:
        if dry_run:
            _emit(preview_summary(settings, parsed))
            return
        result = send_summary(settings, parsed, force=force)
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    _emit(result)
    if result.status != "ok":
        raise typer.Exit(code=1)


@app.command("serve")
def serve_command(
    host: Annotated[str, typer.Option("--host", help="This machine's Tailscale address, from `tailscale ip -4`.")],
    phone: Annotated[str, typer.Option("--phone", help="The phone's Tailscale address.")],
    computer: Annotated[str, typer.Option("--computer", help="The computer's Tailscale address.")],
    port: Annotated[int, typer.Option("--port", min=1024, max=65535)] = DEFAULT_PORT,
) -> None:
    """Take reports from the phone and the computer until stopped."""
    try:
        serve(Settings(), host, port, phone=phone, computer=computer)
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    except OSError as error:
        # Tailscale not up yet is the expected case, and the service tries again by itself.
        typer.echo(f"cannot listen on {host}:{port}: {error.strerror}", err=True)
        raise typer.Exit(code=1) from error
