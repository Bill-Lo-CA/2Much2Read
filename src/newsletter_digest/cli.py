from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Annotated

import typer
from pydantic import BaseModel, ValidationError

from .config import Settings
from .operations import (
    CATEGORY_OPTIONS,
    MailSelector,
    SubscriptionCandidate,
    authorize_gmail,
    ensure_labels,
    filters,
    inspect_mail,
    list_mails,
    list_subscriptions,
    sync_subscriptions,
)
from .operations import (
    doctor as run_doctor,
)
from .pipeline import retry_delivery, run_pipeline

app = typer.Typer(no_args_is_help=True)
auth_app = typer.Typer(no_args_is_help=True)
labels_app = typer.Typer(no_args_is_help=True)
filters_app = typer.Typer(no_args_is_help=True)
mails_app = typer.Typer(no_args_is_help=True)
subscriptions_app = typer.Typer(no_args_is_help=True)
delivery_app = typer.Typer(no_args_is_help=True)
app.add_typer(auth_app, name="auth")
app.add_typer(labels_app, name="labels")
app.add_typer(filters_app, name="filters")
app.add_typer(mails_app, name="mails")
app.add_typer(subscriptions_app, name="subscriptions")
app.add_typer(delivery_app, name="delivery")

SourceOption = Annotated[str | None, typer.Option("--source")]
QueryOption = Annotated[str | None, typer.Option("--query")]
SubscriptionOption = Annotated[str | None, typer.Option("--subscription")]


def emit(result: BaseModel | Mapping[str, object]) -> None:
    values = result.model_dump(mode="json", exclude_none=True) if isinstance(result, BaseModel) else result
    typer.echo(json.dumps(values, ensure_ascii=False, default=str))


def selector(source: str | None, query: str | None, subscription: str | None) -> MailSelector:
    try:
        return MailSelector(source=source, query=query, subscription=subscription)
    except ValidationError as error:
        raise typer.BadParameter("exactly one of --source, --query, or --subscription is required") from error


def invoke(operation: Callable[[], BaseModel]) -> None:
    try:
        emit(operation())
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error


def choose_category(candidate: SubscriptionCandidate) -> tuple[str, str] | None:
    typer.echo(f"\n{candidate.id}: {candidate.name}")
    while True:
        choice = typer.prompt(
            "Category (1=AI, 2=CLOUD_DATA, 3=CYBERSECURITY, 4=SOFTWARE_ENGINEERING, 5=PRODUCT_BUSINESS, 6=EXCLUDED)"
        ).strip()
        if choice == "6":
            return None
        if selected := CATEGORY_OPTIONS.get(choice):
            return selected
        typer.echo("Please enter a number from 1 to 6.")


@auth_app.command("gmail")
def auth_gmail() -> None:
    invoke(lambda: authorize_gmail(Settings()))


@labels_app.command("ensure")
def labels_ensure() -> None:
    invoke(lambda: ensure_labels(Settings()))


@filters_app.command("ensure")
def filters_ensure() -> None:
    invoke(lambda: filters(Settings(), ensure=True))


@filters_app.command("audit")
def filters_audit() -> None:
    invoke(lambda: filters(Settings(), ensure=False))


@mails_app.command("list")
def mails_list(
    source: SourceOption = None,
    query: QueryOption = None,
    subscription: SubscriptionOption = None,
    limit: Annotated[int, typer.Option(min=1, max=100)] = 20,
) -> None:
    invoke(lambda: list_mails(Settings(), selector(source, query, subscription), limit))


@mails_app.command("inspect")
def mails_inspect(
    message_id: Annotated[str, typer.Option("--id")],
    source: SourceOption = None,
    query: QueryOption = None,
    subscription: SubscriptionOption = None,
    extract: Annotated[bool, typer.Option()] = False,
    limit: Annotated[int, typer.Option(min=1, max=100)] = 100,
) -> None:
    invoke(lambda: inspect_mail(Settings(), selector(source, query, subscription), message_id, limit, extract))


@subscriptions_app.command("list")
def subscriptions_list(limit: Annotated[int, typer.Option(min=1, max=500)] = 100) -> None:
    invoke(lambda: list_subscriptions(Settings(), limit))


@subscriptions_app.command("sync")
def subscriptions_sync(
    apply: Annotated[bool, typer.Option()] = False,
    limit: Annotated[int, typer.Option(min=1, max=500)] = 100,
) -> None:
    invoke(lambda: sync_subscriptions(Settings(), limit, apply, choose_category))


@delivery_app.command("retry")
def delivery_retry() -> None:
    emit({"status": "ok", "delivered": retry_delivery(Settings())})


@app.command()
def doctor(send_test: Annotated[bool, typer.Option()] = False) -> None:
    invoke(lambda: run_doctor(Settings(), send_test))


@app.command("run")
def run_command(
    dry_run: Annotated[bool, typer.Option()] = False,
    source: Annotated[str | None, typer.Option()] = None,
    deliver: Annotated[bool, typer.Option("--deliver/--no-deliver")] = True,
    max_messages: Annotated[int | None, typer.Option(min=1)] = None,
    force: Annotated[bool, typer.Option()] = False,
) -> None:
    if force and (dry_run or source is None or max_messages is None):
        raise typer.BadParameter("--force requires --source and --max-messages and cannot use --dry-run")
    emit(run_pipeline(Settings(), source, max_messages, not deliver, dry_run, force))


@app.command()
def backfill(
    days: Annotated[int, typer.Option(min=1, max=30)] = 7,
    source: Annotated[str | None, typer.Option()] = None,
    deliver: Annotated[bool, typer.Option()] = False,
) -> None:
    emit(run_pipeline(Settings(gmail_lookback_days=days), source, None, not deliver, False))
