from pathlib import Path

import pytest
from pydantic import ValidationError

from two_much_two_read import config
from two_much_two_read.config import (
    ExcludedSubscription,
    GmailSource,
    HackerNewsSource,
    Settings,
    Source,
    load_sources,
    update_subscription_files,
)


def test_source_variants_are_discriminated_by_type(tmp_path: Path) -> None:
    path = tmp_path / "sources.yaml"
    path.write_text(
        """sources:
  - type: gmail
    id: newsletter
    name: Newsletter
    gmail_query: from:newsletter@example.com
  - type: hackernews
    id: hn-best
    name: Hacker News Best
    max_story_candidates: 12
""",
        encoding="utf-8",
    )

    sources = load_sources(path).sources

    assert isinstance(sources[0], GmailSource)
    assert sources[0].type == "gmail"
    assert isinstance(sources[1], HackerNewsSource)
    assert sources[1].max_story_candidates == 12


@pytest.mark.parametrize(
    "source",
    [
        "{type: hackernews, id: hn, name: HN, gmail_query: from:news@example.com}",
        "{type: gmail, id: mail, name: Mail, gmail_query: from:mail@example.com, feed: beststories}",
        "{type: hackernews, id: hn, name: HN, max_articles_per_run: 31}",
        "{type: hackernews, id: hn, name: HN, require_full_text: false}",
    ],
)
def test_rejects_mixed_or_invalid_source_variants(tmp_path: Path, source: str) -> None:
    path = tmp_path / "sources.yaml"
    path.write_text(f"sources:\n  - {source}\n", encoding="utf-8")

    with pytest.raises(ValueError):
        load_sources(path)


def test_rejects_duplicate_source_ids(tmp_path: Path) -> None:
    config = tmp_path / "sources.yaml"
    config.write_text(
        "sources:\n  - {type: gmail, id: news, name: One, gmail_query: one}\n"
        "  - {type: gmail, id: news, name: Two, gmail_query: two}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unique"):
        load_sources(config)


def test_rejects_reserved_list_source_id(tmp_path: Path) -> None:
    config = tmp_path / "sources.yaml"
    config.write_text(
        "sources:\n  - {type: gmail, id: list, name: Reserved, gmail_query: 'label:reserved'}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="reserved for the CLI"):
        load_sources(config)


def test_accepts_gmail_filter_criteria_dict(tmp_path: Path) -> None:
    config = tmp_path / "sources.yaml"
    config.write_text(
        """sources:
  - type: gmail
    id: news
    name: News
    gmail_query: 'label:"Newsletters/News"'
    gmail_filter:
      label: Newsletters/News
      criteria: {from: news@example.com, subject: Daily}
""",
        encoding="utf-8",
    )

    source = load_sources(config).sources[0]
    assert source.gmail_filter is not None
    assert source.gmail_filter.criteria == {"from": "news@example.com", "subject": "Daily"}


def test_rejects_unknown_gmail_filter_criteria(tmp_path: Path) -> None:
    config = tmp_path / "sources.yaml"
    config.write_text(
        """sources:
  - type: gmail
    id: news
    name: News
    gmail_query: news
    gmail_filter:
      label: Newsletters/News
      criteria: {dangerousAction: true}
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsupported gmail filter criteria"):
        load_sources(config)


def test_rejects_unknown_source_fields(tmp_path: Path) -> None:
    config = tmp_path / "sources.yaml"
    config.write_text(
        "sources:\n  - {type: gmail, id: news, name: News, gmail_query: news, typo: true}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="typo"):
        load_sources(config)


def test_settings_ignore_repo_dotenv_and_use_private_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    app_config = home / ".config" / "2much2read-runtime"
    app_config.mkdir(parents=True)
    (tmp_path / ".env").write_text(
        "DISCORD_WEBHOOK_URL=https://ignored.example/webhook\nDATABASE_PATH=ignored.sqlite3\n",
        encoding="utf-8",
    )
    (app_config / ".2much2read.env").write_text(
        "DISCORD_WEBHOOK_URL=https://digest.example/webhook\nDATABASE_PATH=/tmp/2much2read.sqlite3\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("DATABASE_PATH", raising=False)

    settings = Settings()

    assert settings.discord_webhook_url == "https://digest.example/webhook"
    assert settings.database_path == Path("/tmp/2much2read.sqlite3")


def test_default_runtime_paths_are_application_scoped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("DATABASE_PATH", raising=False)

    settings = Settings()

    assert settings.gmail_credentials_path == home / ".config/2much2read-runtime/gmail-client-secret.json"
    assert settings.gmail_token_path == home / ".config/2much2read-runtime/2much2read/gmail-token.json"
    assert settings.sources_config_path == home / ".config/2much2read-runtime/sources.yaml"
    assert settings.database_path == home / ".local/share/2much2read-runtime/2much2read/2much2read.sqlite3"
    assert settings.lock_path == home / ".local/share/2much2read-runtime/2much2read/2much2read.lock"


def test_files_at_pre_scoping_locations_are_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    config_root = home / ".config/2much2read-runtime"
    data_root = home / ".local/share/2much2read-runtime"
    config_root.mkdir(parents=True)
    data_root.mkdir(parents=True)
    # Files at the pre-scoping locations must no longer be picked up.
    for path in (
        config_root / "gmail-token.json",
        data_root / "2much2read.sqlite3",
        data_root / "2much2read.lock",
    ):
        path.touch()
    monkeypatch.setenv("HOME", str(home))
    for variable in ("GMAIL_TOKEN_PATH", "DATABASE_PATH", "LOCK_PATH"):
        monkeypatch.delenv(variable, raising=False)

    settings = Settings()

    assert settings.gmail_token_path == config_root / "2much2read/gmail-token.json"
    assert settings.database_path == data_root / "2much2read/2much2read.sqlite3"
    assert settings.lock_path == data_root / "2much2read/2much2read.lock"


def test_bot_delivery_settings_require_a_numeric_channel() -> None:
    destination = Settings(
        discord_delivery_mode="bot", discord_bot_token="secret", discord_bot_channel_id="123"
    ).discord_destination()

    assert (destination.transport, destination.bot_channel_id) == ("bot", "123")
    with pytest.raises(ValueError, match="DISCORD_CONFIG_INVALID"):
        Settings(discord_delivery_mode="bot", discord_bot_token="secret", discord_bot_channel_id="bad").discord_destination()


def test_subscription_file_update_restores_both_files_when_second_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources_path = tmp_path / "sources.yaml"
    excluded_path = tmp_path / "excluded-subscriptions.yaml"
    original_sources = (
        "sources:\n  - type: gmail\n    id: existing\n    name: Existing\n    gmail_query: from:existing@example.com\n"
    )
    original_excluded = (
        "excluded_subscriptions:\n  - key: existing\n    id: existing\n    name: Existing\n    sender: existing@example.com\n"
    )
    sources_path.write_text(original_sources, encoding="utf-8")
    excluded_path.write_text(original_excluded, encoding="utf-8")
    original_replace = config.os.replace

    def fail_excluded_replace(source: str | Path, destination: str | Path) -> None:
        if Path(destination) == excluded_path:
            raise OSError("simulated replace failure")
        original_replace(source, destination)

    monkeypatch.setattr(config.os, "replace", fail_excluded_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        update_subscription_files(
            sources_path,
            [Source(id="new", name="New", gmail_query="from:new@example.com")],
            excluded_path,
            [ExcludedSubscription(key="new", id="new", name="New", sender="new@example.com")],
        )

    assert sources_path.read_text(encoding="utf-8") == original_sources
    assert excluded_path.read_text(encoding="utf-8") == original_excluded


def test_subscription_file_updates_emit_explicit_gmail_types(tmp_path: Path) -> None:
    sources_path = tmp_path / "sources.yaml"
    excluded_path = tmp_path / "excluded-subscriptions.yaml"
    sources_path.write_text("sources:\n", encoding="utf-8")
    excluded_path.write_text("excluded_subscriptions: []\n", encoding="utf-8")

    update_subscription_files(
        sources_path,
        [Source(id="new", name="New", gmail_query="from:new@example.com")],
        excluded_path,
        [],
    )

    assert "type: gmail" in sources_path.read_text(encoding="utf-8")


def test_only_the_measured_digest_languages_are_accepted() -> None:
    for language in ("zh-TW", "zh-Hant", "zh-CN", "en", "en-US"):
        assert Settings(digest_language=language).digest_language == language

    for language in ("fr", "ja", "de"):
        with pytest.raises(ValidationError, match="is not supported"):
            Settings(digest_language=language)
