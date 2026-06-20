from typer.testing import CliRunner

from newsletter_digest.cli import app


def test_run_help_uses_clear_delivery_flags() -> None:
    result = CliRunner().invoke(app, ["run", "--help"])

    assert result.exit_code == 0
    assert "--deliver" in result.stdout
    assert "--no-deliver" in result.stdout
    assert "--no-no-deliver" not in result.stdout
