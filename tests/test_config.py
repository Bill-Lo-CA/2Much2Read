from pathlib import Path

import pytest

from two_much_two_read.config import Settings, load_sources


def test_rejects_duplicate_source_ids(tmp_path: Path) -> None:
    config = tmp_path / "sources.yaml"
    config.write_text(
        "sources:\n  - {id: news, name: One, gmail_query: one}\n  - {id: news, name: Two, gmail_query: two}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unique"):
        load_sources(config)


def test_rejects_reserved_list_source_id(tmp_path: Path) -> None:
    config = tmp_path / "sources.yaml"
    config.write_text(
        "sources:\n  - {id: list, name: Reserved, gmail_query: 'label:reserved'}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="reserved for the CLI"):
        load_sources(config)


def test_accepts_gmail_filter_criteria_dict(tmp_path: Path) -> None:
    config = tmp_path / "sources.yaml"
    config.write_text(
        """sources:
  - id: news
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
  - id: news
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
        "sources:\n  - {id: news, name: News, gmail_query: news, typo: true}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="typo"):
        load_sources(config)


def test_settings_ignore_repo_dotenv_and_use_private_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    app_config = home / ".config" / "2much2read"
    app_config.mkdir(parents=True)
    (tmp_path / ".env").write_text(
        "DISCORD_WEBHOOK_URL=https://legacy.example/webhook\nDATABASE_PATH=legacy.sqlite3\n",
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
