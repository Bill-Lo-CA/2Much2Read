from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from two_bored_one_made import pipeline
from two_bored_one_made.config import NudgeConfig, NudgesConfig, Settings, load_nudges
from two_bored_one_made.storage import Database
from two_read_runtime.discord import DiscordDeliveryError

MONTREAL = ZoneInfo("America/Montreal")
WEBHOOK = "https://discord.com/api/webhooks/123456789012345678/test-webhook-token"


def nudge(**overrides: object) -> NudgeConfig:
    values: dict[str, object] = {
        "id": "stretch",
        "message": "起來動一動",
        "at": [time(9), time(14), time(21)],
        "total_sends": 3,
    }
    values.update(overrides)
    return NudgeConfig.model_validate(values)


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, 21, hour, minute, tzinfo=MONTREAL)


def settings(tmp_path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "discord_webhook_url": WEBHOOK,
        "database_path": tmp_path / "2bored1made.sqlite3",
        "lock_path": tmp_path / "2bored1made.lock",
        "nudges_config_path": tmp_path / "nudges.yaml",
    }
    values.update(overrides)
    return Settings(**values)


def write_nudges(tmp_path, body: str) -> None:
    (tmp_path / "nudges.yaml").write_text(body, encoding="utf-8")


class TestDueSlots:
    def test_a_slot_is_due_once_its_time_has_passed(self) -> None:
        config = NudgesConfig(nudges=[nudge()])

        assert pipeline.due_slots(config, {}, {}, at(8, 59)) == []
        assert [slot.slot_time for slot in pipeline.due_slots(config, {}, {}, at(9, 0))] == [time(9)]

    def test_only_one_slot_fires_per_run_so_a_backlog_trickles(self) -> None:
        # A machine switched off all day reaches 21:05 with three slots owed. Sending all three at
        # once is a burst of identical messages; the per-minute timer drains them one at a time.
        config = NudgesConfig(nudges=[nudge()])

        due = pipeline.due_slots(config, {}, {}, at(21, 5))

        assert [slot.slot_time for slot in due] == [time(9)]

    def test_a_delivered_slot_is_not_sent_again(self) -> None:
        config = NudgesConfig(nudges=[nudge()])
        states = {("stretch", "09:00"): ("delivered", 1)}

        due = pipeline.due_slots(config, {"stretch": 1}, states, at(14, 30))

        assert [slot.slot_time for slot in due] == [time(14)]

    def test_a_failed_slot_is_retried_but_not_forever(self) -> None:
        config = NudgesConfig(nudges=[nudge()])

        retried = pipeline.due_slots(config, {}, {("stretch", "09:00"): ("failed", 1)}, at(9, 30))
        assert [slot.slot_time for slot in retried] == [time(9)]

        exhausted = pipeline.due_slots(config, {}, {("stretch", "09:00"): ("failed", pipeline.MAX_SLOT_ATTEMPTS)}, at(9, 30))
        assert exhausted == []

    def test_a_finished_nudge_stops(self) -> None:
        config = NudgesConfig(nudges=[nudge(total_sends=3)])

        assert pipeline.due_slots(config, {"stretch": 3}, {}, at(21, 0)) == []

    def test_a_disabled_nudge_never_fires(self) -> None:
        config = NudgesConfig(nudges=[nudge(enabled=False)])

        assert pipeline.due_slots(config, {}, {}, at(21, 0)) == []

    def test_yesterdays_missed_slot_is_dropped_rather_than_delivered_late(self) -> None:
        # slot_states is keyed on today only, so a slot missed yesterday leaves no trace; what
        # matters is that nothing from yesterday appears in the result.
        config = NudgesConfig(nudges=[nudge(at=[time(9)])])

        due = pipeline.due_slots(config, {}, {}, at(2, 0))

        assert due == []


class TestDelivery:
    def test_a_failed_send_does_not_spend_one_of_the_sends(self, tmp_path, monkeypatch) -> None:
        write_nudges(tmp_path, "nudges:\n  - id: stretch\n    message: hi\n    at: ['09:00']\n    total_sends: 3\n")
        monkeypatch.setattr(pipeline, "deliver", lambda *args, **kwargs: (_ for _ in ()).throw(DiscordDeliveryError()))

        result = pipeline.run(settings(tmp_path), False, now=at(9, 30))

        assert (result.status, result.sent, result.failed) == ("failed", 0, 1)
        database = Database(tmp_path / "2bored1made.sqlite3")
        try:
            assert database.delivered_counts() == {}
        finally:
            database.close()

    def test_a_delivered_send_counts_down(self, tmp_path, monkeypatch) -> None:
        write_nudges(tmp_path, "nudges:\n  - id: stretch\n    message: hi\n    at: ['09:00']\n    total_sends: 2\n")
        monkeypatch.setattr(pipeline, "deliver", lambda *args, **kwargs: ["message-id"])

        result = pipeline.run(settings(tmp_path), False, now=at(9, 30))

        assert (result.status, result.sent, result.completed) == ("ok", 1, [])
        assert pipeline.status(settings(tmp_path), now=at(9, 30)).nudges[0].remaining == 1

    def test_the_last_send_reports_the_nudge_complete(self, tmp_path, monkeypatch) -> None:
        write_nudges(tmp_path, "nudges:\n  - id: stretch\n    message: hi\n    at: ['09:00']\n    total_sends: 1\n")
        monkeypatch.setattr(pipeline, "deliver", lambda *args, **kwargs: ["message-id"])

        assert pipeline.run(settings(tmp_path), False, now=at(9, 30)).completed == ["stretch"]
        # A second run the same day has nothing left to send.
        assert pipeline.run(settings(tmp_path), False, now=at(9, 31)).sent == 0

    def test_running_twice_in_one_slot_sends_once(self, tmp_path, monkeypatch) -> None:
        write_nudges(tmp_path, "nudges:\n  - id: stretch\n    message: hi\n    at: ['09:00']\n    total_sends: 5\n")
        sends: list[str] = []
        monkeypatch.setattr(pipeline, "deliver", lambda *args, **kwargs: sends.append("x") or ["message-id"])

        pipeline.run(settings(tmp_path), False, now=at(9, 30))
        pipeline.run(settings(tmp_path), False, now=at(9, 31))

        assert sends == ["x"]

    def test_a_mention_outside_the_allowlist_fails_instead_of_being_sent(self, tmp_path, monkeypatch) -> None:
        write_nudges(
            tmp_path,
            "nudges:\n  - id: stretch\n    message: hi\n    at: ['09:00']\n    total_sends: 3\n    user_id: '456'\n",
        )
        monkeypatch.setattr(pipeline, "deliver", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("sent")))

        result = pipeline.run(settings(tmp_path, discord_allowed_mention_ids="123"), False, now=at(9, 30))

        assert result.failed_by_error_code == {pipeline.MENTION_NOT_ALLOWED: 1}

    def test_an_allowed_mention_reaches_discord_as_a_mention(self, tmp_path, monkeypatch) -> None:
        write_nudges(
            tmp_path,
            "nudges:\n  - id: stretch\n    message: hi\n    at: ['09:00']\n    total_sends: 3\n    user_id: '123'\n",
        )
        calls: list[dict[str, object]] = []

        def fake_deliver(destination, content, username, **kwargs):  # type: ignore[no-untyped-def]
            calls.append({"content": content, **kwargs})
            return ["message-id"]

        monkeypatch.setattr(pipeline, "deliver", fake_deliver)

        pipeline.run(settings(tmp_path, discord_allowed_mention_ids="123"), False, now=at(9, 30))

        assert calls[0]["allowed_user_ids"] == ["123"]
        assert calls[0]["mention_user_ids"] == ["123"]


class TestDestination:
    def test_a_nudge_webhook_overrides_the_shared_one(self, tmp_path) -> None:
        other = "https://discord.com/api/webhooks/876543210987654321/other-token"

        destination = pipeline.nudge_destination(settings(tmp_path), nudge(webhook_url=other))

        assert destination.webhook_url == other

    def test_without_its_own_webhook_a_nudge_uses_the_configured_one(self, tmp_path) -> None:
        assert pipeline.nudge_destination(settings(tmp_path), nudge()).webhook_url == WEBHOOK

    def test_both_mode_still_resolves_to_a_single_destination(self, tmp_path) -> None:
        # A nudge counts sends, so it cannot fan out: a run that reached one destination and not
        # the other could not say whether it had spent one of the remaining sends.
        configured = settings(tmp_path, discord_delivery_mode="both", discord_bot_token="token", discord_bot_channel_id="123")

        assert pipeline.nudge_destination(configured, nudge()).transport == "webhook"


class TestContent:
    def test_the_message_is_sent_verbatim_apart_from_at_signs(self) -> None:
        assert pipeline.nudge_content(nudge(message="ping @everyone")) == "ping @​everyone"

    def test_no_progress_counter_is_appended_to_the_operators_words(self) -> None:
        assert pipeline.nudge_content(nudge(message="起來動一動")) == "起來動一動"


class TestConfig:
    def test_times_are_sorted_and_must_be_unique(self, tmp_path) -> None:
        assert nudge(at=[time(21), time(9)]).at == [time(9), time(21)]

        with pytest.raises(ValueError, match="unique"):
            nudge(at=[time(9), time(9)])

    def test_a_blank_message_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="blank"):
            nudge(message="   ")

    def test_total_sends_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            nudge(total_sends=0)

    def test_a_non_numeric_user_id_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="numeric"):
            nudge(user_id="someone")

    def test_a_webhook_that_is_not_discord_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="webhook_url"):
            nudge(webhook_url="https://example.com/api/webhooks/1/token")

    def test_duplicate_ids_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="unique"):
            NudgesConfig(nudges=[nudge(), nudge()])

    def test_an_unknown_field_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            NudgeConfig.model_validate({"id": "a", "message": "b", "at": ["09:00"], "total_sends": 1, "evry": "2h"})

    def test_a_missing_file_names_itself(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="not found"):
            load_nudges(tmp_path / "absent.yaml")

    def test_an_empty_file_loads_as_no_nudges(self, tmp_path) -> None:
        (tmp_path / "nudges.yaml").write_text("", encoding="utf-8")

        assert load_nudges(tmp_path / "nudges.yaml").nudges == []


class TestReset:
    def test_reset_restarts_the_count(self, tmp_path, monkeypatch) -> None:
        write_nudges(tmp_path, "nudges:\n  - id: stretch\n    message: hi\n    at: ['09:00']\n    total_sends: 1\n")
        monkeypatch.setattr(pipeline, "deliver", lambda *args, **kwargs: ["message-id"])
        pipeline.run(settings(tmp_path), False, now=at(9, 30))

        result = pipeline.reset(settings(tmp_path), "stretch")

        assert result.cleared == 1
        assert pipeline.status(settings(tmp_path), now=at(9, 30)).nudges[0].delivered == 0

    def test_resetting_an_unknown_nudge_is_an_error(self, tmp_path) -> None:
        write_nudges(tmp_path, "nudges: []\n")

        with pytest.raises(ValueError, match="unknown nudge id"):
            pipeline.reset(settings(tmp_path), "ghost")


class TestStatus:
    def test_next_slot_rolls_over_to_tomorrow_after_the_last_one(self, tmp_path) -> None:
        write_nudges(tmp_path, "nudges:\n  - id: stretch\n    message: hi\n    at: ['09:00']\n    total_sends: 5\n")

        view = pipeline.status(settings(tmp_path), now=at(23, 0)).nudges[0]

        assert view.next_slot is not None
        assert view.next_slot.date() == at(9, 0).date().replace(day=22)

    def test_a_finished_nudge_reports_no_next_slot(self, tmp_path, monkeypatch) -> None:
        write_nudges(tmp_path, "nudges:\n  - id: stretch\n    message: hi\n    at: ['09:00']\n    total_sends: 1\n")
        monkeypatch.setattr(pipeline, "deliver", lambda *args, **kwargs: ["message-id"])
        pipeline.run(settings(tmp_path), False, now=at(9, 30))

        view = pipeline.status(settings(tmp_path), now=at(9, 30)).nudges[0]

        assert (view.done, view.next_slot, view.remaining) == (True, None, 0)


class TestDryRun:
    def test_a_dry_run_reports_what_would_be_sent_without_sending(self, tmp_path, monkeypatch) -> None:
        write_nudges(tmp_path, "nudges:\n  - id: stretch\n    message: hi\n    at: ['09:00']\n    total_sends: 3\n")
        monkeypatch.setattr(pipeline, "deliver", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("sent")))

        result = pipeline.run(settings(tmp_path), True, now=at(9, 30))

        assert result.due == ["stretch@09:00"]
