from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from email.utils import parseaddr
from typing import Any

from .command_models import SubscriptionCandidate, SubscriptionListResult, SubscriptionSyncResult, SubscriptionView
from .config import (
    ExcludedSubscription,
    GmailFilter,
    GmailSource,
    Settings,
    Source,
    excluded_subscriptions_path,
    load_excluded_subscriptions,
    load_sources,
    update_subscription_files,
)
from .gmail import GmailClient, message_headers

CATEGORY_OPTIONS = {
    "1": ("AI", "ai-newsPaper"),
    "2": ("CLOUD_DATA", "cloud-data-newspaper"),
    "3": ("CYBERSECURITY", "cyber-newspaper"),
    "4": ("SOFTWARE_ENGINEERING", "dev-newspaper"),
    "5": ("PRODUCT_BUSINESS", "product-business-newspaper"),
}
CATEGORY_LABELS = {label: category for category, label in CATEGORY_OPTIONS.values()}


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
    messages: list[dict[str, Any]],
    configured: list[Source],
    excluded_keys: set[str],
    labels: dict[str, str],
    used_ids: set[str] | None = None,
) -> list[SubscriptionCandidate]:
    configured_by_sender: dict[str, list[Source]] = {}
    for source in configured:
        if sender := sender_from_source(source):
            configured_by_sender.setdefault(sender, []).append(source)
    grouped: dict[str, dict[str, object]] = {}
    for message in messages:
        headers = message_headers(message)
        key, list_id, id_basis = subscription_identity(headers)
        if not list_id and not headers.get("list-unsubscribe") and not headers.get("x-emailoctopus-list-id"):
            continue
        sender_name, sender = parseaddr(headers.get("from", ""))
        if not sender or key in excluded_keys:
            continue
        label = next(
            (name for label_id in message.get("labelIds", []) if (name := labels.get(str(label_id))) in CATEGORY_LABELS),
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

    used_ids = set(used_ids or ()) | {source.id for source in configured}
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


def configured_candidates(settings: Settings, gmail: GmailClient, limit: int) -> list[SubscriptionCandidate]:
    sources = load_sources(settings.sources_config_path).sources
    configured = [source for source in sources if isinstance(source, GmailSource)]
    path = excluded_subscriptions_path(settings.sources_config_path)
    excluded = {item.key for item in load_excluded_subscriptions(path).excluded_subscriptions}
    messages = [gmail.get_message_metadata(message_id) for message_id in gmail.list_messages("newer_than:30d", limit)]
    labels = {label_id: name for name, label_id in gmail.labels.items()}
    return subscription_candidates(messages, configured, excluded, labels, {source.id for source in sources})


def list_subscriptions(settings: Settings, gmail: GmailClient, limit: int) -> SubscriptionListResult:
    candidates = configured_candidates(settings, gmail, limit)
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


def sync_subscriptions(
    settings: Settings, gmail: GmailClient, limit: int, apply: bool, choose_category: CategoryPicker
) -> SubscriptionSyncResult:
    pending = [item for item in configured_candidates(settings, gmail, limit) if not item.configured]
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
