from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Annotated

import httpx
import typer
from pydantic import BaseModel, ValidationError

from two_read_runtime.progress import run_with_live_progress

from .command_models import MailSelector, SubscriptionCandidate
from .config import Settings
from .diagnostics import doctor as run_doctor
from .mail_operations import (
    authorize_gmail,
    ensure_labels,
    filters,
    gmail_client,
    inspect_mail,
    list_mails,
)
from .ollama import OllamaClient, OllamaSchemaError, create_ollama_client
from .pipeline import retry_delivery, run_pipeline
from .subscription_operations import (
    CATEGORY_OPTIONS,
    list_subscriptions,
    sync_subscriptions,
)

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


def choose_category_with_ollama(ollama: OllamaClient, candidate: SubscriptionCandidate) -> tuple[str, str] | None:
    try:
        category = ollama.classify_subscription(candidate.name, candidate.sender, candidate.list_id, candidate.subject)
    except (httpx.HTTPError, OllamaSchemaError) as error:
        typer.echo(f"Automatic classification failed for {candidate.id}: {error}. Choose manually.")
        return choose_category(candidate)
    return next(option for option in CATEGORY_OPTIONS.values() if option[0] == category)


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
    settings = Settings()
    invoke(lambda: list_subscriptions(settings, gmail_client(settings), limit))


@subscriptions_app.command("sync")
def subscriptions_sync(
    apply: Annotated[bool, typer.Option()] = False,
    limit: Annotated[int, typer.Option(min=1, max=500)] = 100,
) -> None:
    settings = Settings()
    if apply:
        ollama = create_ollama_client(settings)

        def category_picker(candidate: SubscriptionCandidate) -> tuple[str, str] | None:
            return choose_category_with_ollama(ollama, candidate)

    else:
        category_picker = choose_category
    invoke(lambda: sync_subscriptions(settings, gmail_client(settings), limit, apply, category_picker))


@delivery_app.command("retry")
def delivery_retry() -> None:
    emit(retry_delivery(Settings()))


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
    emit(
        run_with_live_progress(
            "2much2read run",
            lambda status: run_pipeline(Settings(), source, max_messages, not deliver, dry_run, force, status=status),
        )
    )


@app.command()
def backfill(
    days: Annotated[int, typer.Option(min=1, max=30)] = 7,
    source: Annotated[str | None, typer.Option()] = None,
    deliver: Annotated[bool, typer.Option()] = False,
) -> None:
    emit(run_pipeline(Settings(gmail_lookback_days=days), source, None, not deliver, False))
