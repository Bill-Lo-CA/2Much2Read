from __future__ import annotations

import json
from typing import Annotated

import typer
from pydantic import BaseModel

from two_read_runtime.discord import deliver

from .config import Settings

app = typer.Typer(no_args_is_help=True)


class SendResult(BaseModel):
    status: str = "ok"
    discord_message_ids: list[str]


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
    result = SendResult(
        discord_message_ids=deliver(
            settings.discord_webhook_url,
            content,
            settings.discord_username,
            allowed_user_ids=mention_ids,
            mention_user_ids=mention_ids,
        )
    )
    typer.echo(json.dumps(result.model_dump()))
