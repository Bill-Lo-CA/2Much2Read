from __future__ import annotations

from two_read_runtime.locking import ProcessLock

from .command_models import (
    CommandResult,
    FiltersResult,
    FilterView,
    LabelsReconcileResult,
    LabelsResult,
    MailInspectionResult,
    MailListResult,
    MailMetadata,
    MailSelector,
    MailSummary,
    ParsedMail,
)
from .config import GmailSource, Settings, load_sources
from .gmail import FilterStatus, GmailClient, credentials, display_id, message_headers
from .mime import extract_gmail_payload
from .ollama import create_ollama_client
from .storage import Database
from .subscription_operations import configured_candidates


def gmail_client(settings: Settings) -> GmailClient:
    with ProcessLock(settings.lock_path):
        return GmailClient(
            credentials(
                settings.gmail_credentials_path,
                settings.gmail_token_path,
                settings.gmail_oauth_callback_port,
            )
        )


def mail_query(settings: Settings, gmail: GmailClient, selector: MailSelector, limit: int) -> str:
    if selector.query is not None:
        return selector.query
    if selector.source is not None:
        sources = load_sources(settings.sources_config_path).sources
        source = next((item for item in sources if item.id == selector.source), None)
        if source is None:
            available = ", ".join(item.id for item in sources) or "(none)"
            raise ValueError(f"unknown source id {selector.source!r}; available source IDs: {available}")
        if not isinstance(source, GmailSource):
            raise ValueError(f"source {selector.source!r} is not a Gmail source")
        return source.gmail_query
    if selector.subscription is not None:
        candidates = configured_candidates(settings, gmail, limit)
        candidate = next((item for item in candidates if item.id == selector.subscription), None)
        if candidate is None:
            available = ", ".join(item.id for item in candidates) or "(none)"
            raise ValueError(f"unknown subscription id {selector.subscription!r}; available subscription IDs: {available}")
        return candidate.proposal.gmail_query
    return ""


def list_mails(settings: Settings, selector: MailSelector, limit: int) -> MailListResult:
    gmail = gmail_client(settings)
    query = mail_query(settings, gmail, selector, limit)
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
    query = mail_query(settings, gmail, selector, limit)
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
            if source is not None and not isinstance(source, GmailSource):
                raise ValueError(f"source {source_id!r} is not a Gmail source")
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
    sources = [source for source in load_sources(settings.sources_config_path).sources if isinstance(source, GmailSource)]
    gmail = gmail_client(settings)
    gmail.ensure_labels()
    source_labels = gmail.ensure_source_labels(sources)
    return LabelsResult(labels=sorted(["NewsletterBot/Failed", "NewsletterBot/Processed", *source_labels]))


def reconcile_labels(settings: Settings) -> LabelsReconcileResult:
    reconciled = 0
    failed = 0
    with ProcessLock(settings.lock_path):
        database = Database(settings.database_path)
        try:
            gmail = GmailClient(
                credentials(
                    settings.gmail_credentials_path,
                    settings.gmail_token_path,
                    settings.gmail_oauth_callback_port,
                )
            )
            gmail.ensure_labels()
            for message in database.gmail_documents_for_label_reconciliation():
                try:
                    gmail.sync_processing_label(str(message["gmail_message_id"]), str(message["state"]))
                except Exception:
                    database.fail_label_sync(int(message["id"]))
                    failed += 1
                else:
                    database.mark_label_synced(int(message["id"]))
                    reconciled += 1
        finally:
            database.close()
    return LabelsReconcileResult(status="partial" if failed else "ok", reconciled=reconciled, failed=failed)


def filters(settings: Settings, ensure: bool) -> FiltersResult:
    sources = [source for source in load_sources(settings.sources_config_path).sources if isinstance(source, GmailSource)]
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
