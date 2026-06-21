from __future__ import annotations

import json
import os
from typing import Annotated

import httpx
import typer

from .config import Settings, load_sources
from .gmail import GmailClient, credentials, display_id
from .mime import extract_gmail_payload
from .ollama import OllamaClient
from .pipeline import resend_latest, run_pipeline
from .pipeline import retry_delivery as retry_pending

app = typer.Typer(no_args_is_help=True)
auth_app = typer.Typer()
labels_app = typer.Typer()
filters_app = typer.Typer()
app.add_typer(auth_app, name="auth")
app.add_typer(labels_app, name="labels")
app.add_typer(filters_app, name="filters")


def emit(**values: object) -> None:
    typer.echo(json.dumps(values, ensure_ascii=False, default=str))


@auth_app.command("gmail")
def auth_gmail() -> None:
    settings = Settings()
    credentials(
        settings.gmail_credentials_path,
        settings.gmail_token_path,
        settings.gmail_oauth_callback_port,
    )
    emit(status="ok", message="Gmail authorization saved")


@labels_app.command("ensure")
def labels_ensure() -> None:
    settings = Settings()
    sources = load_sources(settings.sources_config_path).sources
    gmail = GmailClient(
        credentials(
            settings.gmail_credentials_path,
            settings.gmail_token_path,
            settings.gmail_oauth_callback_port,
        )
    )
    gmail.ensure_labels()
    filters = gmail.ensure_source_filters(sources)
    labels = {"NewsletterBot/Processed", "NewsletterBot/Failed"}
    labels.update(source.gmail_filter.label for source in sources if source.gmail_filter is not None)
    emit(status="ok", labels=sorted(labels), filters=filters)


@filters_app.command("list")
def filters_list() -> None:
    settings = Settings()
    gmail = GmailClient(
        credentials(
            settings.gmail_credentials_path,
            settings.gmail_token_path,
            settings.gmail_oauth_callback_port,
        )
    )
    emit(status="ok", filters=gmail.list_filters())


@app.command()
def discover(
    query: Annotated[str | None, typer.Option()] = None,
    source: Annotated[str | None, typer.Option()] = None,
    limit: Annotated[int, typer.Option(min=1, max=100)] = 20,
) -> None:
    if query is not None and source is not None:
        raise typer.BadParameter("--query and --source cannot be used together")
    settings = Settings()
    if source is not None:
        sources = load_sources(settings.sources_config_path).sources
        if source == "list":
            emit(status="ok", source_ids=[item.id for item in sources])
            return
        matching_sources = [item for item in sources if item.id == source]
        if not matching_sources:
            available_ids = ", ".join(item.id for item in sources) or "(none)"
            raise typer.BadParameter(f"unknown source id {source!r}; available source IDs: {available_ids}")
        query = matching_sources[0].gmail_query
    query = query or "newer_than:30d"
    gmail = GmailClient(
        credentials(
            settings.gmail_credentials_path,
            settings.gmail_token_path,
            settings.gmail_oauth_callback_port,
        )
    )
    for message_id in gmail.list_messages(query, limit):
        message = gmail.get_message(message_id)
        payload = message.get("payload", {})
        headers = payload.get("headers", []) if isinstance(payload, dict) else []
        values = {
            str(header.get("name", "")).lower(): str(header.get("value", "")) for header in headers if isinstance(header, dict)
        }
        emit(
            id=display_id(message_id),
            received=message.get("internalDate"),
            sender=values.get("from"),
            subject=values.get("subject"),
        )


@app.command()
def inspect(
    source: Annotated[str, typer.Option()],
    message_id: Annotated[str, typer.Option("--id")],
    extract: Annotated[bool, typer.Option()] = False,
    limit: Annotated[int, typer.Option(min=1, max=100)] = 100,
) -> None:
    settings = Settings()
    sources = load_sources(settings.sources_config_path).sources
    matching_sources = [item for item in sources if item.id == source]
    if not matching_sources:
        available_ids = ", ".join(item.id for item in sources) or "(none)"
        raise typer.BadParameter(f"unknown source id {source!r}; available source IDs: {available_ids}")
    configured_source = matching_sources[0]
    gmail = GmailClient(
        credentials(
            settings.gmail_credentials_path,
            settings.gmail_token_path,
            settings.gmail_oauth_callback_port,
        )
    )
    for gmail_id in gmail.list_messages(configured_source.gmail_query, limit):
        if display_id(gmail_id) != message_id:
            continue
        message = gmail.get_message(gmail_id)
        payload = message.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("email has no Gmail payload")
        text = extract_gmail_payload(payload)
        truncated = len(text) > 45_000
        llm_input = text[:45_000] if truncated else text
        headers = payload.get("headers", [])
        result: dict[str, object] = {
            "status": "ok",
            "id": message_id,
            "metadata": {
                "received": message.get("internalDate"),
                "headers": headers if isinstance(headers, list) else [],
                "label_ids": message.get("labelIds", []),
                "mime_type": payload.get("mimeType"),
            },
            "parsed": {
                "text": llm_input,
                "original_characters": len(text),
                "input_characters": len(llm_input),
                "truncated": truncated,
            },
        }
        if extract:
            ollama = OllamaClient(
                settings.ollama_base_url,
                settings.ollama_model,
                settings.ollama_timeout_seconds,
                settings.ollama_num_ctx,
                settings.ollama_keep_alive,
            )
            extraction = ollama.extract(
                configured_source.id,
                llm_input,
                truncated,
                configured_source.max_items_per_email,
            )
            result["extraction"] = extraction.model_dump(mode="json")
        emit(**result)
        return
    raise typer.BadParameter(f"message id {message_id!r} was not found in source {source!r} within the first {limit} matches")


@app.command()
def doctor(send_test: Annotated[bool, typer.Option()] = False) -> None:
    settings = Settings()
    checks: dict[str, str] = {}
    try:
        load_sources(settings.sources_config_path)
        checks["sources"] = "ok"
    except Exception as error:
        checks["sources"] = str(error)
    checks["gmail_token"] = "ok" if settings.gmail_token_path.is_file() else "missing"
    checks["database_directory"] = "ok" if os.access(settings.database_path.parent, os.W_OK) else "not_writable"
    try:
        response = httpx.get(f"{settings.ollama_base_url.rstrip('/')}/api/tags", timeout=5)
        response.raise_for_status()
        models = [model.get("name") for model in response.json().get("models", [])]
        checks["ollama"] = "ok" if settings.ollama_model in models else "model_missing"
    except Exception:
        checks["ollama"] = "unreachable"
    checks["discord"] = "configured" if settings.discord_webhook_url else "missing"
    if send_test and settings.discord_webhook_url:
        response = httpx.post(
            settings.discord_webhook_url,
            params={"wait": "true"},
            json={
                "content": "Newsletter Digest connectivity test",
                "allowed_mentions": {"parse": []},
            },
            timeout=30,
        )
        checks["discord_test"] = "ok" if response.is_success else "failed"
    emit(
        status="ok" if all(v in {"ok", "configured"} for v in checks.values()) else "warning",
        checks=checks,
    )


@app.command("run")
def run_command(
    dry_run: Annotated[bool, typer.Option()] = False,
    source: Annotated[str | None, typer.Option()] = None,
    deliver: Annotated[bool, typer.Option("--deliver/--no-deliver")] = True,
    max_messages: Annotated[int | None, typer.Option(min=1)] = None,
    force: Annotated[bool, typer.Option()] = False,
    resend: Annotated[bool, typer.Option()] = False,
) -> None:
    if resend:
        if dry_run or source is not None or not deliver or max_messages is not None or force:
            raise typer.BadParameter("--resend cannot be combined with other run options")
        delivered = resend_latest(Settings())
        emit(status="ok" if delivered else "no_digest", delivered=delivered)
        return
    if force and (dry_run or source is None or max_messages is None):
        raise typer.BadParameter("--force requires --source and --max-messages and cannot use --dry-run")
    emit(**run_pipeline(Settings(), source, max_messages, not deliver, dry_run, force))


@app.command("retry-delivery")
def retry_delivery_command() -> None:
    emit(status="ok", delivered=retry_pending(Settings()))


@app.command()
def backfill(
    days: Annotated[int, typer.Option(min=1, max=30)] = 7,
    source: Annotated[str | None, typer.Option()] = None,
    deliver: Annotated[bool, typer.Option()] = False,
) -> None:
    settings = Settings(gmail_lookback_days=days)
    emit(**run_pipeline(settings, source, None, not deliver, False))
