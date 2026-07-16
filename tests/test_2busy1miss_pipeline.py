from datetime import timedelta

from two_busy_one_miss.config import EventMatch, RemindersConfig, ReminderSpec, RuleConfig
from two_busy_one_miss.pipeline import event_query_lookahead


def test_event_query_lookahead_covers_longest_reminder() -> None:
    config = RemindersConfig(
        calendars=[{"id": "primary"}],
        default_rules=[ReminderSpec(before="5m")],
        rules=[RuleConfig(id="long-reminder", match=EventMatch(), reminders=[ReminderSpec(before="10d")])],
    )

    assert event_query_lookahead(config, 7) == timedelta(days=10)
