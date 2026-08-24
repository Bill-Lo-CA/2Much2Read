"""Static checks on the shell the installers are written in.

POSIX shell fails quietly: an unquoted expansion, a trailing `&&` that turns a function's exit
status non-zero under `set -e`, a misparsed assignment. Those have each cost a review round in this
repository, so they are checked here rather than found by reading.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def shell_scripts() -> list[Path]:
    return sorted([*ROOT.glob("scripts/*.sh"), *ROOT.glob("scripts/lib/*.sh")])


def test_there_are_scripts_to_check() -> None:
    assert shell_scripts(), "the glob must not silently match nothing"


@pytest.mark.parametrize("script", shell_scripts(), ids=lambda path: path.name)
def test_the_script_parses_as_posix_shell(script: Path) -> None:
    result = subprocess.run(["sh", "-n", str(script)], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not installed")
def test_shellcheck_is_clean() -> None:
    result = subprocess.run(
        ["shellcheck", "-s", "sh", *[str(path) for path in shell_scripts()]],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout
