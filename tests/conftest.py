from pathlib import Path

import pytest

from two_much_two_read.config import Settings


@pytest.fixture
def newsletter_settings(tmp_path: Path) -> Settings:
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text("sources: []\n", encoding="utf-8")
    return Settings(sources_config_path=sources_path)
