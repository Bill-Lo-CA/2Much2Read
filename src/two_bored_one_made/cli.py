from __future__ import annotations

import json
from typing import Annotated

import typer
from pydantic import BaseModel

from two_read_runtime.discord import DiscordDeliveryError, deliver, delivery_error_code

from .config import Settings

app = typer.Typer(no_args_is_help=True)


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
