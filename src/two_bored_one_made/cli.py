from __future__ import annotations

import json
from collections.abc import Callable
from typing import Annotated

import typer
from pydantic import BaseModel

from two_read_runtime.discord import DiscordDeliveryError, deliver, delivery_error_code
from two_read_runtime.paths import directory_is_creatable, env_file
from two_read_runtime.permissions import private_directory_status, private_file_status, sqlite_files_status

from .config import NudgesConfigNotFound, Settings, load_nudges
from .pipeline import reset as reset_nudge
from .pipeline import run as run_nudges
from .pipeline import status as nudge_status
from .pipeline import unusable_nudges

app = typer.Typer(no_args_is_help=True)
_HEALTHY_CHECKS = {"ok", "webhook", "bot", "both", "not_created"}


def emit(result: BaseModel) -> None:
    typer.echo(json.dumps(result.model_dump(mode="json", exclude_none=True), ensure_ascii=False, default=str))


def emit_delivery_result(result: BaseModel) -> None:
    emit(result)
    if getattr(result, "status", "ok") in {"partial", "failed"}:
        raise typer.Exit(code=1)


def invoke(operation: Callable[[], BaseModel]) -> None:
    try:
        emit(operation())
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error


class SendResult(BaseModel):
    status: str = "ok"
    discord_message_ids: list[str]
    delivery_succeeded: int = 0
    delivery_failed: int = 0
    failed_by_error_code: dict[str, int] = {}


@app.callback()
def main() -> None:
    pass


@app.command()
def doctor() -> None:
    settings = Settings()
    problems: dict[str, str] = {}
    checks: dict[str, str] = {"env_file": private_file_status(env_file("2bored1made"))}
    try:
        settings.discord_destinations()
        checks["discord"] = settings.discord_delivery_mode
    except DiscordDeliveryError:
        checks["discord"] = (
            "missing" if settings.discord_delivery_mode == "webhook" and not settings.discord_webhook_url else "invalid"
        )
    try:
        config = load_nudges(settings.nudges_config_path)
        checks["nudges"] = "ok"
        # Loading is not enough: a nudge can name a user the environment file does not allow, or
        # inherit a destination that will not resolve, and neither shows up until it is too late.
        #
        # Every value in `checks` is a status word from a closed vocabulary, because that is what
        # the healthy/warning verdict reads. Nudge ids are the operator's, and "bot", "ok" and
        # "both" are all valid ones, so naming them here would let a broken nudge read as healthy.
        problems = unusable_nudges(settings, config)
        checks["nudges_deliverable"] = "ok" if not problems else "unusable"
    except (OSError, ValueError) as error:
        # The type, not the text. Only the loader can know the file was absent, and a validation
        # error quotes the operator's own words back - so a bad value that happens to read "not
        # found" was reported as a missing file while nudges_file said it was right there.
        checks["nudges"] = "missing" if isinstance(error, NudgesConfigNotFound) else "invalid"
        checks["nudges_deliverable"] = "unknown"
    checks["nudges_file"] = private_file_status(settings.nudges_config_path, missing_ok=True)
    checks["data_dir"] = private_directory_status(settings.database_path.parent, missing_ok=True)
    checks["database"] = sqlite_files_status(settings.database_path)
    checks["lock_file"] = private_file_status(settings.lock_path, missing_ok=True)
    checks["database_directory"] = "ok" if directory_is_creatable(settings.database_path.parent) else "not_writable"
    status = "ok" if all(value in _HEALTHY_CHECKS for value in checks.values()) else "warning"
    payload: dict[str, object] = {"status": status, "checks": checks}
    # Which nudge, and why. The check above only says that something is wrong; an operator fixing it
    # needs the id and the error code the send path would have raised.
    if problems:
        payload["nudges_unusable"] = problems
    typer.echo(json.dumps(payload))


@app.command("run")
def run_command(dry_run: Annotated[bool, typer.Option()] = False) -> None:
    """Deliver every nudge whose scheduled time has arrived today."""
    try:
        result = run_nudges(Settings(), dry_run)
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    emit_delivery_result(result)


@app.command()
def status() -> None:
    """Report how far each nudge has counted down and when it fires next."""
    invoke(lambda: nudge_status(Settings()))


@app.command()
def reset(nudge: Annotated[str, typer.Option("--nudge")]) -> None:
    """Clear one nudge's send history so its count starts again from zero."""
    invoke(lambda: reset_nudge(Settings(), nudge))


@app.command()
def send(
    message: Annotated[str, typer.Option("--message")],
    mention: Annotated[list[str] | None, typer.Option("--mention")] = None,
) -> None:
    if not message.strip():
        raise typer.BadParameter("--message must not be empty")
    settings = Settings()
    mention_ids = list(dict.fromkeys(mention or []))
    if invalid_ids := set(mention_ids) - settings.allowed_mention_ids:
        raise typer.BadParameter(f"mention IDs are not allowed: {', '.join(sorted(invalid_ids))}")
    content = message.replace("@", "@\u200b")
    message_ids: list[str] = []
    failed_by_error_code: dict[str, int] = {}
    for destination in settings.discord_destinations():
        try:
            message_ids.extend(
                deliver(
                    destination,
                    content,
                    settings.discord_username,
                    allowed_user_ids=mention_ids,
                    mention_user_ids=mention_ids,
                )
            )
        except DiscordDeliveryError as error:
            code = delivery_error_code(error)
            failed_by_error_code[code] = failed_by_error_code.get(code, 0) + 1
    result = SendResult(
        status="partial" if failed_by_error_code else "ok",
        discord_message_ids=message_ids,
        delivery_succeeded=len(settings.discord_destinations()) - sum(failed_by_error_code.values()),
        delivery_failed=sum(failed_by_error_code.values()),
        failed_by_error_code=failed_by_error_code,
    )
    typer.echo(json.dumps(result.model_dump()))
