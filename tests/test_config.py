from pathlib import Path

import pytest

from newsletter_digest.config import load_sources


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
