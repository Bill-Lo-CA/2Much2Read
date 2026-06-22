from __future__ import annotations

import hashlib
import json
import os
import re
from email.utils import parseaddr
from typing import Annotated, cast

import httpx
import typer

from .config import (
    Settings,
    Source,
    append_excluded_subscriptions,
    append_sources,
    excluded_subscriptions_path,
    load_excluded_subscriptions,
    load_sources,
)
from .gmail import GmailClient, credentials, display_id
from .mime import extract_gmail_payload
from .ollama import OllamaClient
from .pipeline import resend_latest, run_pipeline
from .pipeline import retry_delivery as retry_pending

app = typer.Typer(no_args_is_help=True)
auth_app = typer.Typer()
labels_app = typer.Typer()
filters_app = typer.Typer()
discover_app = typer.Typer(invoke_without_command=True)
subscriptions_app = typer.Typer(invoke_without_command=True)
app.add_typer(auth_app, name="auth")
app.add_typer(labels_app, name="labels")
app.add_typer(filters_app, name="filters")
app.add_typer(discover_app, name="discover")
discover_app.add_typer(subscriptions_app, name="subscriptions")

CATEGORY_OPTIONS = {
    "1": ("AI", "ai-newsPaper"),
    "2": ("CLOUD_DATA", "cloud-data-newspaper"),
    "3": ("CYBERSECURITY", "cyber-newspaper"),
    "4": ("SOFTWARE_ENGINEERING", "dev-newspaper"),
    "5": ("PRODUCT_BUSINESS", "product-business-newspaper"),
}
CATEGORY_LABELS = {label: category for category, label in CATEGORY_OPTIONS.values()}


def emit(**values: object) -> None:
    typer.echo(json.dumps(values, ensure_ascii=False, default=str))


def gmail_client(settings: Settings) -> GmailClient:
    return GmailClient(
        credentials(
            settings.gmail_credentials_path,
            settings.gmail_token_path,
            settings.gmail_oauth_callback_port,
        )
    )


def message_headers(message: dict[str, object]) -> dict[str, str]:
    payload = message.get("payload", {})
    headers = payload.get("headers", []) if isinstance(payload, dict) else []
    return {str(item.get("name", "")).lower(): str(item.get("value", "")) for item in headers if isinstance(item, dict)}


def emit_messages(gmail: GmailClient, query: str, limit: int) -> None:
    for message_id in gmail.list_messages(query, limit):
        message = gmail.get_message_metadata(message_id)
        headers = message_headers(message)
        emit(
            id=display_id(message_id),
            received=message.get("internalDate"),
            sender=headers.get("from"),
            subject=headers.get("subject"),
        )


def choose_category(proposal: dict[str, object], base_query: str, filter_criteria: dict[str, str]) -> bool:
    typer.echo(f"\n{proposal['id']}: {proposal['name']}")
    while True:
        choice = typer.prompt(
            "Category (1=AI, 2=CLOUD_DATA, 3=CYBERSECURITY, 4=SOFTWARE_ENGINEERING, 5=PRODUCT_BUSINESS, 6=EXCLUDED)"
        )
        if choice.strip() == "6":
            return False
        selected = CATEGORY_OPTIONS.get(choice.strip())
        if selected:
            break
        typer.echo("Please enter a number from 1 to 6.")
    category, label = selected
    proposal.update(
        enabled=True,
        category=category,
        gmail_query=f"label:{label} {base_query}",
        gmail_filter={"label": label, "criteria": filter_criteria},
    )
    return True


def normalized_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")


def from_identity(value: str) -> tuple[str, str]:
    name, sender = parseaddr(value)
    return (" ".join(name.casefold().split()), sender.casefold())


def subscription_identity(headers: dict[str, str]) -> tuple[str, str | None, str]:
    raw_list_id = headers.get("list-id", "").strip()
    list_id_match = re.search(r"<([^<>]+)>", raw_list_id)
    list_id = (list_id_match.group(1) if list_id_match else raw_list_id).strip()
    if list_id:
        return list_id.casefold(), list_id, list_id
    provider_id = headers.get("x-emailoctopus-list-id", "").strip()
    sender_name, sender = parseaddr(headers.get("from", ""))
    if provider_id:
        digest = hashlib.sha256(provider_id.encode()).hexdigest()[:16]
        return f"emailoctopus:{digest}", None, sender_name or sender
    raw_from = headers.get("from", "").strip()
    return f"from:{raw_from.casefold()}", None, sender_name or sender


def valid_subscription_query(gmail: GmailClient, candidate: dict[str, object], limit: int) -> bool:
    message_ids = gmail.list_messages(str(candidate["base_query"]), limit)
    if not message_ids or candidate["query_ambiguous"]:
        return False
    expected = from_identity(str(candidate["from_header"]))
    for message_id in message_ids:
        headers = message_headers(gmail.get_message_metadata(message_id))
        if from_identity(headers.get("from", "")) != expected:
            return False
        if not any(headers.get(name) for name in ("list-id", "list-unsubscribe", "x-emailoctopus-list-id")):
            return False
    return True


def sender_from_source(source: Source) -> str | None:
    gmail_filter = source.gmail_filter
    sender = gmail_filter.criteria.get("from") if gmail_filter is not None else None
    if isinstance(sender, str):
        return sender.casefold()
    match = re.search(r"(?:^|\s)from:(?:\"([^\"]+)\"|(\S+))", source.gmail_query, re.I)
    return (match.group(1) or match.group(2)).casefold() if match else None


def subscription_candidates(
    gmail: GmailClient,
    configured: list[Source],
    excluded_keys: set[str],
    limit: int,
) -> list[dict[str, object]]:
    configured_by_sender: dict[str, list[Source]] = {}
    for source in configured:
        if sender := sender_from_source(source):
            configured_by_sender.setdefault(sender, []).append(source)
    labels_by_id = {label_id: name for name, label_id in gmail.labels.items()}
    grouped: dict[str, dict[str, object]] = {}
    for message_id in gmail.list_messages("newer_than:30d", limit):
        message = gmail.get_message_metadata(message_id)
        headers = message_headers(message)
        key, list_id, id_basis = subscription_identity(headers)
        if not list_id and not headers.get("list-unsubscribe") and not headers.get("x-emailoctopus-list-id"):
            continue
        sender_name, sender = parseaddr(headers.get("from", ""))
        if not sender:
            continue
        if key in excluded_keys:
            continue
        label = next(
            (name for label_id in message.get("labelIds", []) if (name := labels_by_id.get(str(label_id))) in CATEGORY_LABELS),
            None,
        )
        grouped.setdefault(
            key,
            {
                "name": sender_name or (list_id.split(".", 1)[0] if list_id else sender),
                "key": key,
                "id_basis": id_basis,
                "sender": sender.casefold(),
                "from_header": headers.get("from", ""),
                "list_id": list_id or None,
                "subject": headers.get("subject"),
                "label": label,
            },
        )

    sender_counts: dict[str, int] = {}
    from_counts: dict[tuple[str, str], int] = {}
    for candidate in grouped.values():
        sender = str(candidate["sender"])
        sender_counts[sender] = sender_counts.get(sender, 0) + 1
        identity = from_identity(str(candidate["from_header"]))
        from_counts[identity] = from_counts.get(identity, 0) + 1

    used_ids = {str(source.id) for source in configured}
    candidates: list[dict[str, object]] = []
    for _key, candidate in sorted(grouped.items()):
        candidate_sender = str(candidate["sender"])
        sender_sources = configured_by_sender.get(candidate_sender, [])
        configured_source = next(
            (source for source in sender_sources if normalized_name(source.name) == normalized_name(str(candidate["name"]))),
            None,
        )
        if configured_source is None and sender_counts[candidate_sender] == 1 and len(sender_sources) == 1:
            configured_source = sender_sources[0]
        source_id = str(configured_source.id) if configured_source else normalized_name(str(candidate["id_basis"]))
        source_id = source_id or "newsletter"
        if not configured_source:
            base_id = source_id
            suffix = 2
            while source_id == "list" or source_id in used_ids:
                source_id = f"{base_id}-{suffix}"
                suffix += 1
        used_ids.add(source_id)
        candidate_label = str(candidate["label"]) if candidate["label"] else None
        sender_name = str(candidate["name"])
        shared_sender = sender_counts[candidate_sender] > 1
        escaped_name = sender_name.replace("\\", "\\\\").replace('"', '\\"')
        base_query = f'from:{candidate_sender} from:"{escaped_name}"' if shared_sender else f"from:{candidate_sender}"
        filter_criteria = {"from": candidate_sender}
        if shared_sender:
            filter_criteria["query"] = f'from:"{escaped_name}"'
        proposal: dict[str, object] = {
            "id": source_id,
            "name": candidate["name"],
            "enabled": True,
            "category": CATEGORY_LABELS.get(str(candidate_label), "OTHER"),
            "gmail_query": f"label:{candidate_label} {base_query}" if candidate_label else base_query,
        }
        if candidate_label:
            proposal["gmail_filter"] = {"label": candidate_label, "criteria": filter_criteria}
        candidates.append(
            {
                **candidate,
                "id": source_id,
                "configured": configured_source is not None,
                "base_query": base_query,
                "filter_criteria": filter_criteria,
                "query_ambiguous": from_counts[from_identity(str(candidate["from_header"]))] > 1,
                "proposal": proposal,
            }
        )
    return candidates


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


@discover_app.callback()
def discover_query(
    ctx: typer.Context,
    query: Annotated[str | None, typer.Option()] = None,
    limit: Annotated[int, typer.Option(min=1, max=100)] = 20,
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    if query is None:
        raise typer.BadParameter("--query is required")
    settings = Settings()
    emit_messages(gmail_client(settings), query, limit)


@discover_app.command("mails")
def discover_mails(
    source: Annotated[str, typer.Option()],
    limit: Annotated[int, typer.Option(min=1, max=100)] = 20,
) -> None:
    settings = Settings()
    sources = load_sources(settings.sources_config_path).sources
    matching = [item for item in sources if item.id == source]
    if not matching:
        available_ids = ", ".join(item.id for item in sources) or "(none)"
        raise typer.BadParameter(f"unknown source id {source!r}; available source IDs: {available_ids}")
    emit_messages(gmail_client(settings), matching[0].gmail_query, limit)


@subscriptions_app.callback()
def discover_subscriptions(
    ctx: typer.Context,
    source: Annotated[str | None, typer.Option()] = None,
    sync: Annotated[bool, typer.Option()] = False,
    apply: Annotated[bool, typer.Option()] = False,
    limit: Annotated[int, typer.Option(min=1, max=500)] = 100,
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    if source is not None and sync:
        raise typer.BadParameter("--source and --sync cannot be used together")
    if apply and not sync:
        raise typer.BadParameter("--apply requires --sync")
    if source is None and not sync:
        raise typer.BadParameter("use --source, --sync, or the list subcommand")
    settings = Settings()
    configured = load_sources(settings.sources_config_path).sources
    exclusions_path = excluded_subscriptions_path(settings.sources_config_path)
    excluded_keys = {item.key for item in load_excluded_subscriptions(exclusions_path).excluded_subscriptions}
    gmail = gmail_client(settings)
    candidates = subscription_candidates(gmail, configured, excluded_keys, limit)
    if source is not None:
        matching = [item for item in candidates if item["id"] == source]
        if not matching:
            available_ids = ", ".join(str(item["id"]) for item in candidates) or "(none)"
            raise typer.BadParameter(f"unknown subscription id {source!r}; available subscription IDs: {available_ids}")
        proposal = cast(dict[str, object], matching[0]["proposal"])
        emit_messages(gmail, str(proposal["gmail_query"]), limit)
        return
    pending = [item for item in candidates if not item["configured"]]
    proposals = [cast(dict[str, object], item["proposal"]) for item in pending]
    ambiguous: list[dict[str, object]] = []
    if apply:
        selected: list[dict[str, object]] = []
        excluded: list[dict[str, object]] = []
        for item, proposal in zip(pending, proposals, strict=True):
            if not valid_subscription_query(gmail, item, limit):
                ambiguous.append({"id": item["id"], "name": item["name"], "query": item["base_query"]})
                continue
            if choose_category(
                proposal,
                str(item["base_query"]),
                cast(dict[str, str], item["filter_criteria"]),
            ):
                selected.append(proposal)
            else:
                excluded.append({key: item[key] for key in ("key", "id", "name", "sender", "list_id")})
        append_excluded_subscriptions(exclusions_path, excluded)
        append_sources(settings.sources_config_path, selected)
        proposals = selected
    emit(
        status="partial" if apply and ambiguous else "applied" if apply else "preview",
        sources=proposals,
        ambiguous=ambiguous if apply else [],
    )


@subscriptions_app.command("list")
def subscriptions_list(limit: Annotated[int, typer.Option(min=1, max=500)] = 100) -> None:
    settings = Settings()
    configured = load_sources(settings.sources_config_path).sources
    exclusions_path = excluded_subscriptions_path(settings.sources_config_path)
    excluded_keys = {item.key for item in load_excluded_subscriptions(exclusions_path).excluded_subscriptions}
    candidates = subscription_candidates(gmail_client(settings), configured, excluded_keys, limit)
    emit(status="ok", subscriptions=[{key: value for key, value in item.items() if key != "proposal"} for item in candidates])


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
