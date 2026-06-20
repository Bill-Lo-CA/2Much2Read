import json
from pathlib import Path

from typer.testing import CliRunner

from newsletter_digest import cli
from newsletter_digest.config import Settings


def test_run_help_uses_clear_delivery_flags() -> None:
    result = CliRunner().invoke(cli.app, ["run", "--help"])

    assert result.exit_code == 0
    assert "--deliver" in result.stdout
    assert "--no-deliver" in result.stdout
    assert "--no-no-deliver" not in result.stdout


def test_discover_source_list_reads_config_without_gmail(tmp_path: Path, monkeypatch: object) -> None:
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text(
        "sources:\n  - id: alphasignal\n    name: AlphaSignal\n    enabled: false\n"
        "    gmail_query: 'label:newsletter-alphasignal'\n",
        encoding="utf-8",
    )
    settings = Settings(sources_config_path=sources_path)
    monkeypatch.setattr(cli, "Settings", lambda: settings)  # type: ignore[attr-defined]

    result = CliRunner().invoke(cli.app, ["discover", "--source", "list"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload == {"status": "ok", "source_ids": ["alphasignal"]}
