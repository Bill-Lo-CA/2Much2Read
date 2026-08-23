"""The installers' unit-replacement transaction.

An upgrade stops a running timer and overwrites live unit files. Every failure between those two
points used to leave the schedule stopped, the unit files truncated, or both, while the script
reported success. These tests drive the state machine rather than one regression each: a fake
`systemctl` keeps real enabled/active state, so a run's outcome is read back as state, not as a
guess from the sequence of calls.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
TIMER = "2bored1made-runtime.timer"
SERVICE = "2bored1made-runtime.service"
STATES = [
    ("enabled", "active"),
    ("enabled", "inactive"),
    ("disabled", "active"),
    ("disabled", "inactive"),
]

FAKE_SYSTEMCTL = """#!/bin/sh
printf '%s\\n' "$*" >> "{log}"
state="{state}"
case "$*" in
  *is-enabled*) [ "$(cat "$state/enabled")" = enabled ] && exit 0 || exit 1 ;;
  *is-active*)  [ "$(cat "$state/active")" = active ] && exit 0 || exit 3 ;;
  *show*Version*) printf '255\\n'; exit 0 ;;
  *show*ActiveState*) printf 'inactive\\n'; exit 0 ;;
  *"disable --now"*) printf 'disabled\\n' > "$state/enabled"; printf 'inactive\\n' > "$state/active"; exit 0 ;;
  *"enable --now"*) printf 'enabled\\n' > "$state/enabled"; printf 'active\\n' > "$state/active"; exit 0 ;;
  *daemon-reload*)
    if [ -f "$state/signal" ]; then
      signal=$(cat "$state/signal")
      # Fire once: the rollback reloads too, and a second signal would kill the shell mid-restore.
      rm -f "$state/signal"
      kill -"$signal" "$PPID"
      sleep 1
      exit 0
    fi
    [ -f "$state/fail-reload" ] && exit 1
    exit 0 ;;
  *enable*) printf 'enabled\\n' > "$state/enabled"; exit 0 ;;
  *start*) printf 'active\\n' > "$state/active"; exit 0 ;;
  *stop*) printf 'inactive\\n' > "$state/active"; exit 0 ;;
esac
exit 0
"""


class Harness:
    def __init__(self, tmp_path: Path, enabled: str, active: str) -> None:
        self.home = tmp_path / "home"
        self.systemd = self.home / ".config" / "systemd" / "user"
        self.systemd.mkdir(parents=True)
        (self.home / ".config" / "2much2read-runtime").mkdir(parents=True)
        self.state = tmp_path / "state"
        self.state.mkdir()
        (self.state / "enabled").write_text(f"{enabled}\n", encoding="utf-8")
        (self.state / "active").write_text(f"{active}\n", encoding="utf-8")
        self.log = tmp_path / "systemctl.log"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        systemctl = bin_dir / "systemctl"
        systemctl.write_text(FAKE_SYSTEMCTL.format(log=self.log, state=self.state), encoding="utf-8")
        systemctl.chmod(0o755)
        self.path = f"{bin_dir}:{os.environ['PATH']}"

    def fail_reload(self) -> None:
        (self.state / "fail-reload").touch()

    def signal_during_reload(self, signal: str) -> None:
        (self.state / "signal").write_text(signal, encoding="utf-8")

    def run(self, answer: str = "\n", cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["sh", "scripts/install-2bored1made.sh"],
            cwd=cwd,
            env=os.environ | {"HOME": str(self.home), "PATH": self.path},
            text=True,
            capture_output=True,
            input=answer,
        )

    @property
    def timer_state(self) -> tuple[str, str]:
        return (
            (self.state / "enabled").read_text(encoding="utf-8").strip(),
            (self.state / "active").read_text(encoding="utf-8").strip(),
        )

    def unit(self, name: str) -> str:
        path = self.systemd / name
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def seed_units(self, marker: str) -> None:
        for name in (SERVICE, TIMER):
            (self.systemd / name).write_text(f"# {marker} {name}\n", encoding="utf-8")

    def leftover_scratch(self) -> list[Path]:
        return [entry for entry in self.systemd.iterdir() if entry.is_dir()]


@pytest.mark.parametrize(("enabled", "active"), STATES)
def test_a_failed_upgrade_leaves_the_timer_exactly_as_it_found_it(tmp_path: Path, enabled: str, active: str) -> None:
    harness = Harness(tmp_path, enabled, active)
    harness.fail_reload()

    result = harness.run()

    assert result.returncode != 0
    assert harness.timer_state == (enabled, active)


@pytest.mark.parametrize(("enabled", "active"), STATES)
def test_a_failed_upgrade_leaves_the_previous_unit_files_intact(tmp_path: Path, enabled: str, active: str) -> None:
    # Writing straight to the live path truncated the working unit before the replacement existed.
    harness = Harness(tmp_path, enabled, active)
    harness.seed_units("previous")
    harness.fail_reload()

    result = harness.run()

    assert result.returncode != 0
    assert harness.unit(SERVICE) == f"# previous {SERVICE}\n"
    assert harness.unit(TIMER) == f"# previous {TIMER}\n"


@pytest.mark.parametrize(("enabled", "active"), STATES)
def test_a_failed_first_installation_leaves_no_unit_files_behind(tmp_path: Path, enabled: str, active: str) -> None:
    harness = Harness(tmp_path, enabled, active)
    harness.fail_reload()

    assert harness.run().returncode != 0
    assert harness.unit(SERVICE) == ""
    assert harness.unit(TIMER) == ""
    assert harness.leftover_scratch() == []


@pytest.mark.parametrize("signal", ["INT", "TERM", "HUP"])
@pytest.mark.parametrize(("enabled", "active"), STATES)
def test_a_signal_after_the_timer_is_stopped_restores_it(tmp_path: Path, signal: str, enabled: str, active: str) -> None:
    # "$?" inside a signal handler is the status of the last completed command, which after a
    # successful step is 0. A handler that reads it decides the run succeeded and skips the restore.
    harness = Harness(tmp_path, enabled, active)
    harness.signal_during_reload(signal)

    result = harness.run()

    assert result.returncode != 0, "an interrupted installation must not report success"
    assert harness.timer_state == (enabled, active)


@pytest.mark.parametrize(("enabled", "active"), STATES)
def test_a_successful_upgrade_reaches_the_answered_state(tmp_path: Path, enabled: str, active: str) -> None:
    harness = Harness(tmp_path, enabled, active)

    result = harness.run(answer="y\n")

    assert result.returncode == 0
    # Answering yes enables the timer, and starts it only if it was not deliberately stopped.
    expected_active = active if enabled == "enabled" else "active"
    assert harness.timer_state == ("enabled", expected_active)


@pytest.mark.parametrize(("enabled", "active"), STATES)
def test_declining_leaves_the_timer_disabled(tmp_path: Path, enabled: str, active: str) -> None:
    harness = Harness(tmp_path, enabled, active)

    result = harness.run(answer="n\n")

    assert result.returncode == 0
    assert harness.timer_state == ("disabled", "inactive")


@pytest.mark.parametrize(("enabled", "active"), STATES)
def test_a_blank_answer_changes_nothing_at_all(tmp_path: Path, enabled: str, active: str) -> None:
    # Including a timer started without being enabled: that is still a schedule the operator is
    # running, and an upgrade nobody answered must not be what stops it.
    harness = Harness(tmp_path, enabled, active)

    result = harness.run(answer="\n")

    assert result.returncode == 0
    assert harness.timer_state == (enabled, active)


def test_a_refusal_before_the_timer_is_touched_never_stops_it(tmp_path: Path) -> None:
    harness = Harness(tmp_path, "enabled", "active")
    (harness.systemd / SERVICE).symlink_to(tmp_path / "outside.service")

    result = harness.run()

    assert result.returncode == 1
    assert "symbolic link" in result.stderr
    assert harness.timer_state == ("enabled", "active")
    assert "disable" not in harness.log.read_text(encoding="utf-8")


@pytest.mark.parametrize("fragment", ["with space", "with&ampersand", "with|pipe"])
def test_a_repository_path_needing_quoting_renders_a_correct_execstart(tmp_path: Path, fragment: str) -> None:
    # sed would expand "&" to the whole match and treat "|" as the end of the expression, and
    # systemd splits an unquoted command line on whitespace.
    repo = tmp_path / f"repo {fragment}"
    for directory in ("scripts", "deploy", "config"):
        shutil.copytree(ROOT / directory, repo / directory)
    executable = repo / ".venv" / "bin" / "2bored1made"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    harness = Harness(tmp_path, "disabled", "inactive")

    result = harness.run(answer="n\n", cwd=repo)

    assert result.returncode == 0, result.stderr
    assert f'ExecStart="{executable}" run' in harness.unit(SERVICE)


def test_an_executable_path_that_cannot_be_quoted_is_refused(tmp_path: Path) -> None:
    repo = tmp_path / 'repo "quote"'
    for directory in ("scripts", "deploy", "config"):
        shutil.copytree(ROOT / directory, repo / directory)
    executable = repo / ".venv" / "bin" / "2bored1made"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    harness = Harness(tmp_path, "enabled", "active")

    result = harness.run(answer="n\n", cwd=repo)

    assert result.returncode != 0
    assert "must not contain quotes" in result.stderr
    assert harness.timer_state == ("enabled", "active")
