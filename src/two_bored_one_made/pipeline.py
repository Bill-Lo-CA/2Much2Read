from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal, overload
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from two_read_runtime.discord import (
    DiscordDeliveryError,
    DiscordDestination,
    configured_destination,
    deliver,
    delivery_error_code,
)
from two_read_runtime.locking import ProcessLock

from .config import NudgeConfig, NudgesConfig, Settings, load_nudges
from .storage import Database, snapshot

# A slot that keeps failing is retried on the next timer firing, but not forever. The real fallback
# is the next slot, so a handful of attempts is enough to ride out a brief Discord outage without
# turning a persistent failure into a per-minute request loop.
MAX_SLOT_ATTEMPTS = 3

MENTION_NOT_ALLOWED = "NUDGE_MENTION_NOT_ALLOWED"


@dataclass(frozen=True)
class DueSlot:
    nudge: NudgeConfig
    slot_date: date
    slot_time: time


class NudgeView(BaseModel):
    id: str
    enabled: bool
    delivered: int
    total_sends: int
    remaining: int
    at: list[str]
    next_slot: datetime | None = None
    overdue: bool = False
    last_delivered_at: str | None = None
    done: bool


class NudgeStatusResult(BaseModel):
    status: str = "ok"
    timezone: str
    nudges: list[NudgeView]


class NudgeDryRunResult(BaseModel):
    status: str = "ok"
    timezone: str
    due: list[str]


class NudgeRunResult(BaseModel):
    status: str = "ok"
    sent: int = 0
    failed: int = 0
    completed: list[str] = []
    discord_message_ids: list[str] = []
    failed_by_error_code: dict[str, int] = {}


class NudgeResetResult(BaseModel):
    status: str = "ok"
    nudge_id: str
    cleared: int


def _timezone(config: NudgesConfig, settings: Settings) -> ZoneInfo:
    return ZoneInfo(config.timezone or settings.nudge_timezone)


def nudge_destination(settings: Settings, nudge: NudgeConfig) -> DiscordDestination:
    """The single place one nudge is delivered to.

    A nudge counts down a fixed number of sends, so it has to have exactly one destination: with
    two, a run that reached one and not the other would have no honest answer to whether it spent
    one of the remaining sends. A nudge with its own webhook uses it; otherwise the configured
    webhook, falling back to the bot when the deployment has no webhook at all.
    """
    if nudge.webhook_url:
        return configured_destination("webhook", nudge.webhook_url, "", "")
    destinations = settings.discord_destinations()
    return next((item for item in destinations if item.transport == "webhook"), destinations[0])


def due_slots(
    config: NudgesConfig,
    delivered_counts: dict[str, int],
    slot_states: dict[tuple[str, str], tuple[str, int]],
    now: datetime,
) -> list[DueSlot]:
    """The slots to send right now: at most one per nudge, and only from today.

    One per run keeps a machine that was switched off all morning from firing every missed slot in
    the same second; with a per-minute timer the backlog drains one message a minute instead. Slots
    from earlier days are dropped rather than delivered late, because a nudge that arrives a day
    after the moment it was scheduled for is noise, not a reminder.
    """
    today = now.date()
    due: list[DueSlot] = []
    for nudge in config.enabled_nudges:
        if delivered_counts.get(nudge.id, 0) >= nudge.total_sends:
            continue
        for moment in nudge.at:
            if datetime.combine(today, moment, now.tzinfo) > now:
                break
            state, attempts = slot_states.get((nudge.id, moment.isoformat("minutes")), ("", 0))
            if state == "delivered" or attempts >= MAX_SLOT_ATTEMPTS:
                continue
            due.append(DueSlot(nudge, today, moment))
            break
    return due


def _next_slot(
    nudge: NudgeConfig,
    delivered_counts: dict[str, int],
    slot_states: dict[tuple[str, str], tuple[str, int]],
    now: datetime,
) -> tuple[datetime, bool]:
    """When the next delivery happens, and whether that slot is already owed.

    A report that disagrees with the thing it reports on is worse than no report. The next firing is
    whatever the next run would send, so this asks `due_slots` rather than restating its rules:
    today's owed slot if there is one - at 21:05 an untouched nudge is still owed its 09:00 - and
    only otherwise the next time of day that has not arrived yet.
    """
    owed = due_slots(NudgesConfig(nudges=[nudge]), delivered_counts, slot_states, now)
    if owed:
        return datetime.combine(owed[0].slot_date, owed[0].slot_time, now.tzinfo), True
    for moment in nudge.at:
        candidate = datetime.combine(now.date(), moment, now.tzinfo)
        if candidate > now:
            return candidate, False
    return datetime.combine(now.date() + timedelta(days=1), nudge.at[0], now.tzinfo), False


def _mention_ids(settings: Settings, nudge: NudgeConfig) -> list[str]:
    if nudge.user_id is None:
        return []
    if nudge.user_id not in settings.allowed_mention_ids:
        raise DiscordDeliveryError(MENTION_NOT_ALLOWED)
    return [nudge.user_id]


def unusable_nudges(settings: Settings, config: NudgesConfig) -> dict[str, str]:
    """Enabled nudges that would fail when their time came, by id and error code.

    A configuration that loads is not a configuration that works: a user_id the environment file
    does not allow, or a destination the global settings cannot resolve, only shows up at the moment
    the message was supposed to go out. This asks the same two questions the send path asks, through
    the same functions, so `doctor` cannot drift into approving what a run would reject.
    """
    problems: dict[str, str] = {}
    for nudge in config.enabled_nudges:
        try:
            nudge_destination(settings, nudge)
            _mention_ids(settings, nudge)
        except DiscordDeliveryError as error:
            problems[nudge.id] = delivery_error_code(error) if error.code != MENTION_NOT_ALLOWED else MENTION_NOT_ALLOWED
    return problems


def nudge_content(nudge: NudgeConfig) -> str:
    """The configured message, verbatim apart from neutralising literal at-signs.

    The text is the operator's own, so it is not rewritten or annotated with progress; `status`
    is where the countdown lives. Escaping `@` matches `send` and keeps a message that happens to
    contain `@everyone` from reading as one.
    """
    return nudge.message.replace("@", "@\u200b")


def _send_slot(settings: Settings, database: Database, slot: DueSlot) -> tuple[list[str], str | None]:
    content = nudge_content(slot.nudge)
    try:
        destination = nudge_destination(settings, slot.nudge)
        mention_ids = _mention_ids(settings, slot.nudge)
        message_ids = deliver(
            destination,
            content,
            settings.discord_username,
            allowed_user_ids=mention_ids,
            mention_user_ids=mention_ids,
        )
    except DiscordDeliveryError as error:
        code = delivery_error_code(error) if error.code != MENTION_NOT_ALLOWED else MENTION_NOT_ALLOWED
        database.record_send(
            slot.nudge.id,
            slot.slot_date,
            slot.slot_time,
            message=content,
            destination_key=_destination_key(settings, slot.nudge),
            delivered=False,
            error_code=code,
        )
        return [], code
    database.record_send(
        slot.nudge.id,
        slot.slot_date,
        slot.slot_time,
        message=content,
        destination_key=destination.key,
        delivered=True,
        message_ids=message_ids,
    )
    return message_ids, None


def _destination_key(settings: Settings, nudge: NudgeConfig) -> str:
    try:
        return nudge_destination(settings, nudge).key
    except DiscordDeliveryError:
        return "unresolved"


@overload
def run(settings: Settings, dry_run: Literal[True], *, now: datetime | None = None) -> NudgeDryRunResult: ...
@overload
def run(settings: Settings, dry_run: Literal[False], *, now: datetime | None = None) -> NudgeRunResult: ...
@overload
def run(settings: Settings, dry_run: bool, *, now: datetime | None = None) -> NudgeRunResult | NudgeDryRunResult: ...
def run(settings: Settings, dry_run: bool, *, now: datetime | None = None) -> NudgeRunResult | NudgeDryRunResult:
    config = load_nudges(settings.nudges_config_path)
    timezone = _timezone(config, settings)
    now = (now or datetime.now(timezone)).astimezone(timezone)
    if dry_run:
        return _dry_run(settings, config, timezone, now)
    # Reading which slots are owed and sending them has to be one critical section. A per-minute
    # timer can overlap itself whenever a send is slow, and two runs that each read the history
    # before either wrote to it would both find the same slot unsent and both deliver it.
    with ProcessLock(settings.lock_path):
        database = Database(settings.database_path)
        try:
            slots = due_slots(config, database.delivered_counts(), database.slot_states(now.date()), now)
            return _dispatch(settings, database, config, slots)
        finally:
            database.close()


def _dry_run(settings: Settings, config: NudgesConfig, timezone: ZoneInfo, now: datetime) -> NudgeDryRunResult:
    """What a real run would send, without creating or writing to anything."""
    with snapshot(settings.database_path) as database:
        counts = database.delivered_counts() if database else {}
        states = database.slot_states(now.date()) if database else {}
    slots = due_slots(config, counts, states, now)
    return NudgeDryRunResult(
        timezone=str(timezone),
        due=[f"{slot.nudge.id}@{slot.slot_time.isoformat('minutes')}" for slot in slots],
    )


def _dispatch(settings: Settings, database: Database, config: NudgesConfig, slots: list[DueSlot]) -> NudgeRunResult:
    sent = 0
    message_ids: list[str] = []
    failed_by_error_code: dict[str, int] = {}
    delivered_ids_by_nudge: list[str] = []
    for slot in slots:
        delivered_ids, error_code = _send_slot(settings, database, slot)
        if error_code is None:
            sent += 1
            message_ids.extend(delivered_ids)
            delivered_ids_by_nudge.append(slot.nudge.id)
        else:
            failed_by_error_code[error_code] = failed_by_error_code.get(error_code, 0) + 1
    # Only what this run finished. Listing every already-finished nudge would repeat the same names
    # in every result for as long as they stay in the configuration.
    counts = database.delivered_counts()
    totals = {nudge.id: nudge.total_sends for nudge in config.nudges}
    completed = [nudge_id for nudge_id in delivered_ids_by_nudge if counts.get(nudge_id, 0) >= totals.get(nudge_id, 0)]
    failed = sum(failed_by_error_code.values())
    return NudgeRunResult(
        status="failed" if failed and not sent else "partial" if failed else "ok",
        sent=sent,
        failed=failed,
        completed=completed,
        discord_message_ids=message_ids,
        failed_by_error_code=failed_by_error_code,
    )


def status(settings: Settings, *, now: datetime | None = None) -> NudgeStatusResult:
    config = load_nudges(settings.nudges_config_path)
    timezone = _timezone(config, settings)
    now = (now or datetime.now(timezone)).astimezone(timezone)
    counts: dict[str, int] = {}
    states: dict[tuple[str, str], tuple[str, int]] = {}
    last_delivered: dict[str, str | None] = {}
    # The same three inputs a run works from, so the report and the run cannot disagree.
    with snapshot(settings.database_path) as database:
        if database is not None:
            counts = database.delivered_counts()
            states = database.slot_states(now.date())
            last_delivered = {nudge.id: database.last_delivery(nudge.id) for nudge in config.nudges}
    views: list[NudgeView] = []
    for nudge in config.nudges:
        delivered = counts.get(nudge.id, 0)
        done = delivered >= nudge.total_sends
        next_slot: datetime | None = None
        overdue = False
        if not done and nudge.enabled:
            next_slot, overdue = _next_slot(nudge, counts, states, now)
        views.append(
            NudgeView(
                id=nudge.id,
                enabled=nudge.enabled,
                delivered=delivered,
                total_sends=nudge.total_sends,
                remaining=max(nudge.total_sends - delivered, 0),
                at=[moment.isoformat("minutes") for moment in nudge.at],
                next_slot=next_slot,
                overdue=overdue,
                last_delivered_at=last_delivered.get(nudge.id),
                done=done,
            )
        )
    return NudgeStatusResult(timezone=str(timezone), nudges=views)


def reset(settings: Settings, nudge_id: str) -> NudgeResetResult:
    """Clear one nudge's history, whether or not it is still in the configuration.

    Deleting a nudge from the YAML leaves its rows behind, and refusing to clear them because the
    configuration no longer mentions the id would make those rows unreachable: re-adding the same
    id later would silently inherit a countdown that was already spent.
    """
    config = load_nudges(settings.nudges_config_path)
    with ProcessLock(settings.lock_path):
        database = Database(settings.database_path)
        try:
            cleared = database.reset_nudge(nudge_id)
            if not cleared and nudge_id not in {nudge.id for nudge in config.nudges}:
                raise ValueError(f"unknown nudge id: {nudge_id}")
        finally:
            database.close()
    return NudgeResetResult(nudge_id=nudge_id, cleared=cleared)
