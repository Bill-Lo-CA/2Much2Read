from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from email.utils import parseaddr

import httpx
import yaml
from pydantic import BaseModel, Field, model_validator

from two_read_runtime.locking import ProcessLock
from two_read_runtime.oauth import token_status
from two_read_runtime.paths import directory_is_creatable

from .config import (
    ExcludedSubscription,
    GmailFilter,
    Settings,
    Source,
    excluded_subscriptions_path,
    load_excluded_subscriptions,
    load_sources,
    update_subscription_files,
)
from .gmail import FilterStatus, GmailClient, credentials, display_id, message_headers
from .mime import extract_gmail_payload
from .ollama import create_ollama_client

CATEGORY_OPTIONS = {
    "1": ("AI", "ai-newsPaper"),
    "2": ("CLOUD_DATA", "cloud-data-newspaper"),
    "3": ("CYBERSECURITY", "cyber-newspaper"),
    "4": ("SOFTWARE_ENGINEERING", "dev-newspaper"),
    "5": ("PRODUCT_BUSINESS", "product-business-newspaper"),
}
CATEGORY_LABELS = {label: category for category, label in CATEGORY_OPTIONS.values()}


class CommandResult(BaseModel):
    status: str = "ok"


class MailSelector(BaseModel):
    source: str | None = None
    query: str | None = None
    subscription: str | None = None

    @model_validator(mode="after")
    def exactly_one(self) -> MailSelector:
        if sum(value is not None for value in (self.source, self.query, self.subscription)) != 1:
            raise ValueError("exactly one of --source, --query, or --subscription is required")
        return self


class MailSummary(BaseModel):
    id: str
    received: object | None
    sender: str | None
    subject: str | None


class MailListResult(CommandResult):
    mails: list[MailSummary]


class MailMetadata(BaseModel):
    received: object | None
    headers: list[dict[str, object]]
    label_ids: list[object]
    mime_type: object | None


class ParsedMail(BaseModel):
    text: str
    original_characters: int
    input_characters: int
    truncated: bool


class MailInspectionResult(CommandResult):
    id: str
    metadata: MailMetadata
    parsed: ParsedMail
    extraction: dict[str, object] | None = None


class SubscriptionCandidate(BaseModel):
    id: str
    name: str
    key: str
    sender: str
    from_header: str
    list_id: str | None
    subject: str | None
    label: str | None
    configured: bool
    base_query: str
    filter_criteria: dict[str, str | bool | int]
    query_ambiguous: bool
    proposal: Source


class SubscriptionView(BaseModel):
    id: str
    name: str
    key: str
    sender: str
    from_header: str
    list_id: str | None
    subject: str | None
    label: str | None
    configured: bool
    base_query: str
    query_ambiguous: bool


class SubscriptionListResult(CommandResult):
    subscriptions: list[SubscriptionView]


class SubscriptionSyncResult(CommandResult):
    sources: list[Source]
    ambiguous: list[Source] = Field(default_factory=list)


class LabelsResult(CommandResult):
    labels: list[str]


class FilterView(BaseModel):
    source_id: str
    label: str
    filter_id: str | None
    status: str


class FiltersResult(CommandResult):
    filters: list[FilterView]


class DoctorResult(CommandResult):
    checks: dict[str, str]


def gmail_client(settings: Settings) -> GmailClient:
    with ProcessLock(settings.lock_path):
        return GmailClient(
            credentials(
                settings.gmail_credentials_path,
                settings.gmail_token_path,
                settings.gmail_oauth_callback_port,
            )
        )


def _model_name(value: str) -> str:
    value = value.strip()
    return value if ":" in value.rsplit("/", 1)[-1] else f"{value}:latest"


def normalized_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")


def from_identity(value: str) -> tuple[str, str]:
    name, sender = parseaddr(value)
    return " ".join(name.casefold().split()), sender.casefold()


def subscription_identity(headers: dict[str, str]) -> tuple[str, str | None, str]:
    raw_list_id = headers.get("list-id", "").strip()
    match = re.search(r"<([^<>]+)>", raw_list_id)
    list_id = (match.group(1) if match else raw_list_id).strip()
    if list_id:
        return list_id.casefold(), list_id, list_id
    provider_id = headers.get("x-emailoctopus-list-id", "").strip()
    sender_name, sender = parseaddr(headers.get("from", ""))
    if provider_id:
        digest = hashlib.sha256(provider_id.encode()).hexdigest()[:16]
        return f"emailoctopus:{digest}", None, sender_name or sender
    raw_from = headers.get("from", "").strip()
    return f"from:{raw_from.casefold()}", None, sender_name or sender


def sender_from_source(source: Source) -> str | None:
    sender = source.gmail_filter.criteria.get("from") if source.gmail_filter is not None else None
    if isinstance(sender, str):
        return sender.casefold()
    match = re.search(r'(?:^|\s)from:(?:"([^"]+)"|(\S+))', source.gmail_query, re.I)
    return (match.group(1) or match.group(2)).casefold() if match else None


def subscription_candidates(
    gmail: GmailClient,
    configured: list[Source],
    excluded_keys: set[str],
    limit: int,
) -> list[SubscriptionCandidate]:
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
        if not sender or key in excluded_keys:
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
                "list_id": list_id,
                "subject": headers.get("subject"),
                "label": label,
            },
        )

    sender_counts: dict[str, int] = {}
    from_counts: dict[tuple[str, str], int] = {}
    for item in grouped.values():
        sender = str(item["sender"])
        sender_counts[sender] = sender_counts.get(sender, 0) + 1
        identity = from_identity(str(item["from_header"]))
        from_counts[identity] = from_counts.get(identity, 0) + 1

    used_ids = {source.id for source in configured}
    candidates: list[SubscriptionCandidate] = []
    for item in (grouped[key] for key in sorted(grouped)):
        sender = str(item["sender"])
        sender_sources = configured_by_sender.get(sender, [])
        configured_source = next(
            (source for source in sender_sources if normalized_name(source.name) == normalized_name(str(item["name"]))),
            None,
        )
        if configured_source is None and sender_counts[sender] == 1 and len(sender_sources) == 1:
            configured_source = sender_sources[0]
        source_id = configured_source.id if configured_source else normalized_name(str(item["id_basis"])) or "newsletter"
        if configured_source is None:
            base_id = source_id
            suffix = 2
            while source_id == "list" or source_id in used_ids:
                source_id = f"{base_id}-{suffix}"
                suffix += 1
        used_ids.add(source_id)
        label = str(item["label"]) if item["label"] else None
        name = str(item["name"])
        escaped_name = name.replace("\\", "\\\\").replace('"', '\\"')
        shared_sender = sender_counts[sender] > 1
        base_query = f'from:{sender} from:"{escaped_name}"' if shared_sender else f"from:{sender}"
        criteria: dict[str, str | bool | int] = {"from": sender}
        if shared_sender:
            criteria["query"] = f'from:"{escaped_name}"'
        proposal = Source(
            id=source_id,
            name=name,
            category=CATEGORY_LABELS.get(label or "", "OTHER"),
            gmail_query=f"label:{label} {base_query}" if label else base_query,
            gmail_filter=GmailFilter(label=label, criteria=criteria) if label else None,
        )
        candidates.append(
            SubscriptionCandidate(
                id=source_id,
                name=name,
                key=str(item["key"]),
                sender=sender,
                from_header=str(item["from_header"]),
                list_id=str(item["list_id"]) if item["list_id"] else None,
                subject=str(item["subject"]) if item["subject"] else None,
                label=label,
                configured=configured_source is not None,
                base_query=base_query,
                filter_criteria=criteria,
                query_ambiguous=from_counts[from_identity(str(item["from_header"]))] > 1,
                proposal=proposal,
            )
        )
    return candidates


def _configured_candidates(settings: Settings, gmail: GmailClient, limit: int) -> list[SubscriptionCandidate]:
    configured = load_sources(settings.sources_config_path).sources
    path = excluded_subscriptions_path(settings.sources_config_path)
    excluded = {item.key for item in load_excluded_subscriptions(path).excluded_subscriptions}
    return subscription_candidates(gmail, configured, excluded, limit)


def _query(settings: Settings, gmail: GmailClient, selector: MailSelector, limit: int) -> str:
    if selector.query is not None:
        return selector.query
    if selector.source is not None:
        sources = load_sources(settings.sources_config_path).sources
        source = next((item for item in sources if item.id == selector.source), None)
        if source is None:
            available = ", ".join(item.id for item in sources) or "(none)"
            raise ValueError(f"unknown source id {selector.source!r}; available source IDs: {available}")
        return source.gmail_query
    candidates = _configured_candidates(settings, gmail, limit)
    candidate = next((item for item in candidates if item.id == selector.subscription), None)
    if candidate is None:
        available = ", ".join(item.id for item in candidates) or "(none)"
        raise ValueError(f"unknown subscription id {selector.subscription!r}; available subscription IDs: {available}")
    return candidate.proposal.gmail_query


def list_mails(settings: Settings, selector: MailSelector, limit: int) -> MailListResult:
    gmail = gmail_client(settings)
    query = _query(settings, gmail, selector, limit)
    mails: list[MailSummary] = []
    for message_id in gmail.list_messages(query, limit):
        message = gmail.get_message_metadata(message_id)
        headers = message_headers(message)
        mails.append(
            MailSummary(
                id=display_id(message_id),
                received=message.get("internalDate"),
                sender=headers.get("from"),
                subject=headers.get("subject"),
            )
        )
    return MailListResult(mails=mails)


def inspect_mail(settings: Settings, selector: MailSelector, message_id: str, limit: int, extract: bool) -> MailInspectionResult:
    gmail = gmail_client(settings)
    query = _query(settings, gmail, selector, limit)
    for gmail_id in gmail.list_messages(query, limit):
        if display_id(gmail_id) != message_id:
            continue
        message = gmail.get_message(gmail_id)
        payload = message.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("email has no Gmail payload")
        text = extract_gmail_payload(payload)
        llm_input = text[:45_000]
        extraction: dict[str, object] | None = None
        if extract:
            source_id = selector.source or selector.subscription or "query"
            source = next(
                (item for item in load_sources(settings.sources_config_path).sources if item.id == source_id),
                None,
            )
            max_items = source.max_items_per_email if source else 10
            extraction = (
                create_ollama_client(settings)
                .extract(source_id, llm_input, len(text) > 45_000, max_items)
                .model_dump(mode="json")
            )
        headers = payload.get("headers", [])
        label_ids = message.get("labelIds", [])
        return MailInspectionResult(
            id=message_id,
            metadata=MailMetadata(
                received=message.get("internalDate"),
                headers=headers if isinstance(headers, list) else [],
                label_ids=label_ids if isinstance(label_ids, list) else [],
                mime_type=payload.get("mimeType"),
            ),
            parsed=ParsedMail(
                text=llm_input,
                original_characters=len(text),
                input_characters=len(llm_input),
                truncated=len(text) > 45_000,
            ),
            extraction=extraction,
        )
    raise ValueError(f"message id {message_id!r} was not found within the first {limit} matches")


def list_subscriptions(settings: Settings, limit: int) -> SubscriptionListResult:
    candidates = _configured_candidates(settings, gmail_client(settings), limit)
    return SubscriptionListResult(
        subscriptions=[
            SubscriptionView.model_validate(item.model_dump(exclude={"proposal", "filter_criteria"})) for item in candidates
        ]
    )


def valid_subscription_query(gmail: GmailClient, candidate: SubscriptionCandidate, limit: int) -> bool:
    message_ids = gmail.list_messages(candidate.base_query, limit)
    if not message_ids or candidate.query_ambiguous:
        return False
    expected = from_identity(candidate.from_header)
    for message_id in message_ids:
        headers = message_headers(gmail.get_message_metadata(message_id))
        if from_identity(headers.get("from", "")) != expected:
            return False
        if not any(headers.get(name) for name in ("list-id", "list-unsubscribe", "x-emailoctopus-list-id")):
            return False
    return True


CategoryPicker = Callable[[SubscriptionCandidate], tuple[str, str] | None]


def sync_subscriptions(settings: Settings, limit: int, apply: bool, choose_category: CategoryPicker) -> SubscriptionSyncResult:
    gmail = gmail_client(settings)
    pending = [item for item in _configured_candidates(settings, gmail, limit) if not item.configured]
    if not apply:
        return SubscriptionSyncResult(status="preview", sources=[item.proposal for item in pending])
    selected: list[Source] = []
    excluded: list[ExcludedSubscription] = []
    ambiguous: list[Source] = []
    for item in pending:
        if not valid_subscription_query(gmail, item, limit):
            ambiguous.append(item.proposal)
            continue
        category = choose_category(item)
        if category is None:
            excluded.append(
                ExcludedSubscription(key=item.key, id=item.id, name=item.name, sender=item.sender, list_id=item.list_id)
            )
            continue
        category_name, label = category
        selected.append(
            item.proposal.model_copy(
                update={
                    "category": category_name,
                    "gmail_query": f"label:{label} {item.base_query}",
                    "gmail_filter": GmailFilter(label=label, criteria=item.filter_criteria),
                }
            )
        )
    update_subscription_files(
        settings.sources_config_path, selected, excluded_subscriptions_path(settings.sources_config_path), excluded
    )
    return SubscriptionSyncResult(status="partial" if ambiguous else "applied", sources=selected, ambiguous=ambiguous)


def authorize_gmail(settings: Settings) -> CommandResult:
    with ProcessLock(settings.lock_path):
        credentials(
            settings.gmail_credentials_path,
            settings.gmail_token_path,
            settings.gmail_oauth_callback_port,
            interactive=True,
        )
    return CommandResult()


def ensure_labels(settings: Settings) -> LabelsResult:
    sources = load_sources(settings.sources_config_path).sources
    gmail = gmail_client(settings)
    gmail.ensure_labels()
    source_labels = gmail.ensure_source_labels(sources)
    return LabelsResult(labels=sorted(["NewsletterBot/Failed", "NewsletterBot/Processed", *source_labels]))


def filters(settings: Settings, ensure: bool) -> FiltersResult:
    sources = load_sources(settings.sources_config_path).sources
    gmail = gmail_client(settings)
    results: list[FilterStatus] = gmail.ensure_source_filters(sources) if ensure else gmail.audit_source_filters(sources)
    status = "ok" if ensure or all(item.status == "exists" for item in results) else "warning"
    return FiltersResult(
        status=status,
        filters=[
            FilterView(source_id=item.source_id, label=item.label, filter_id=item.filter_id, status=item.status)
            for item in results
        ],
    )


def doctor(settings: Settings, send_test: bool) -> DoctorResult:
    checks: dict[str, str] = {}
    try:
        load_sources(settings.sources_config_path)
        checks["sources"] = "ok"
    except (OSError, ValueError, yaml.YAMLError) as error:
        checks["sources"] = str(error)
    checks["gmail_token"] = token_status(
        settings.gmail_token_path,
        ("https://www.googleapis.com/auth/gmail.modify", "https://www.googleapis.com/auth/gmail.settings.basic"),
    )
    checks["database_directory"] = "ok" if directory_is_creatable(settings.database_path.parent) else "not_writable"
    try:
        response = httpx.get(f"{settings.ollama_base_url.rstrip('/')}/api/tags", timeout=5)
        response.raise_for_status()
        payload = response.json()
        models = (
            [model.get("name") for model in payload.get("models", []) if isinstance(model, dict)]
            if isinstance(payload, dict)
            else []
        )
        checks["ollama"] = (
            "ok" if _model_name(settings.ollama_model) in {_model_name(str(model)) for model in models} else "model_missing"
        )
    except (httpx.HTTPError, ValueError):
        checks["ollama"] = "unreachable"
    checks["discord"] = "configured" if settings.discord_webhook_url else "missing"
    if send_test and settings.discord_webhook_url:
        response = httpx.post(
            settings.discord_webhook_url,
            params={"wait": "true"},
            json={"content": "2much2read connectivity test", "allowed_mentions": {"parse": []}},
            timeout=30,
        )
        checks["discord_test"] = "ok" if response.is_success else "failed"
    status = "ok" if all(value in {"ok", "configured"} for value in checks.values()) else "warning"
    return DoctorResult(status=status, checks=checks)
