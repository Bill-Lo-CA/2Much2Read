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
# Every systemctl call this installer makes names one unit last, so state is kept per timer and a
# two-timer installer can be driven into mixed states.
for argument in $*; do unit="$argument"; done
case "$*" in
  *is-enabled*) [ "$(cat "$state/$unit.enabled")" = enabled ] && exit 0 || exit 1 ;;
  *is-active*)  [ "$(cat "$state/$unit.active")" = active ] && exit 0 || exit 3 ;;
  *show*Version*) printf '255\\n'; exit 0 ;;
  *show*ActiveState*) printf 'inactive\\n'; exit 0 ;;
  *"disable --now"*) printf 'disabled\\n' > "$state/$unit.enabled"; printf 'inactive\\n' > "$state/$unit.active"; exit 0 ;;
  *"enable --now"*)
    # One timer of two can be made to refuse, which is how a post-commit failure is reached with
    # nothing for the commit itself to have stopped.
    [ -f "$state/fail-enable.$unit" ] && exit 1
    printf 'enabled\\n' > "$state/$unit.enabled"; printf 'active\\n' > "$state/$unit.active"; exit 0 ;;
  *daemon-reload*)
    if [ -f "$state/signal" ]; then
      signal=$(cat "$state/signal")
      # Fire once: the rollback reloads too, and a second signal would kill the shell mid-restore.
      rm -f "$state/signal"
      kill -"$signal" "$PPID"
      sleep 1
      exit 0
    fi
    if [ -f "$state/wreck-backups" ]; then
      # Make the backups unreadable, so the rollback this failure triggers cannot complete.
      chmod 000 "$state/../home/.config/systemd/user"/.install.*/backup.* 2>/dev/null || true
      exit 1
    fi
    if [ -f "$state/truncate-state" ]; then
      # Leave one word where a timer's recorded state should be, so the restore has nothing to
      # apply. "read" still succeeds on a line like this, which is what made it look restored.
      for recorded in "$state/../home/.config/systemd/user"/.install.*/state.*; do
        printf 'enabled\\n' > "$recorded"
      done
      exit 1
    fi
    [ -f "$state/fail-reload" ] && exit 1
    exit 0 ;;
  *disable*)
    # Records whether the unit file was still on disk when the timer was disabled. A rollback that
    # deleted the units first would leave nothing to disable, and this is how that ordering is
    # measured without asserting what real systemd does with a missing unit.
    [ -f "$HOME/.config/systemd/user/$unit" ] && printf 'unit-present %s\\n' "$unit" >> "{log}"
    printf 'disabled\\n' > "$state/$unit.enabled"; exit 0 ;;
  *enable*) printf 'enabled\\n' > "$state/$unit.enabled"; exit 0 ;;
  *start*) printf 'active\\n' > "$state/$unit.active"; exit 0 ;;
  *stop*) printf 'inactive\\n' > "$state/$unit.active"; exit 0 ;;
esac
exit 0
"""


class Harness:
    """Drives one installer against a fake systemctl that keeps real per-timer state.

    The full matrix stays on 2bored1made, which is the shared helper's contract. The other two are
    pointed at the same harness only for the cases that exercise their own prompt and reporting.
    """

    def __init__(
        self,
        tmp_path: Path,
        enabled: str,
        active: str,
        *,
        script: str = "install-2bored1made.sh",
        timers: tuple[str, ...] = (TIMER,),
        units: tuple[str, ...] = (SERVICE, TIMER),
    ) -> None:
        self.script = script
        self.timers = timers
        self.units = units
        self.home = tmp_path / "home"
        self.systemd = self.home / ".config" / "systemd" / "user"
        self.systemd.mkdir(parents=True)
        (self.home / ".config" / "2much2read-runtime").mkdir(parents=True)
        self.state = tmp_path / "state"
        self.state.mkdir()
        for timer in timers:
            self.set_timer(timer, enabled, active)
        self.log = tmp_path / "systemctl.log"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        systemctl = bin_dir / "systemctl"
        systemctl.write_text(FAKE_SYSTEMCTL.format(log=self.log, state=self.state), encoding="utf-8")
        systemctl.chmod(0o755)
        self.path = f"{bin_dir}:{os.environ['PATH']}"

    def set_timer(self, timer: str, enabled: str, active: str) -> None:
        (self.state / f"{timer}.enabled").write_text(f"{enabled}\n", encoding="utf-8")
        (self.state / f"{timer}.active").write_text(f"{active}\n", encoding="utf-8")

    def fail_reload(self) -> None:
        (self.state / "fail-reload").touch()

    def wreck_backups_then_fail_reload(self) -> None:
        (self.state / "wreck-backups").touch()

    def truncate_recorded_state_then_fail_reload(self) -> None:
        (self.state / "truncate-state").touch()

    def fail_enable(self, timer: str) -> None:
        (self.state / f"fail-enable.{timer}").touch()

    def signal_during_reload(self, signal: str) -> None:
        (self.state / "signal").write_text(signal, encoding="utf-8")

    def run(self, answer: str = "\n", cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["sh", f"scripts/{self.script}"],
            cwd=cwd,
            env=os.environ | {"HOME": str(self.home), "PATH": self.path},
            text=True,
            capture_output=True,
            input=answer,
        )

    def state_of(self, timer: str) -> tuple[str, str]:
        return (
            (self.state / f"{timer}.enabled").read_text(encoding="utf-8").strip(),
            (self.state / f"{timer}.active").read_text(encoding="utf-8").strip(),
        )

    @property
    def timer_state(self) -> tuple[str, str]:
        return self.state_of(self.timers[0])

    def unit(self, name: str) -> str:
        path = self.systemd / name
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def seed_units(self, marker: str) -> None:
        for name in self.units:
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
    repo = _repo_copy(tmp_path, f"repo {fragment}")
    executable = repo / ".venv" / "bin" / "2bored1made"
    harness = Harness(tmp_path, "disabled", "inactive")

    result = harness.run(answer="n\n", cwd=repo)

    assert result.returncode == 0, result.stderr
    assert f'ExecStart="{executable}" run' in harness.unit(SERVICE)


def test_an_executable_path_that_cannot_be_quoted_is_refused(tmp_path: Path) -> None:
    repo = _repo_copy(tmp_path, 'repo "quote"')
    harness = Harness(tmp_path, "enabled", "active")

    result = harness.run(answer="n\n", cwd=repo)

    assert result.returncode != 0
    assert "must not contain quotes" in result.stderr
    assert harness.timer_state == ("enabled", "active")


def _staging_directories(harness: Harness) -> list[Path]:
    return [entry for entry in harness.systemd.iterdir() if entry.is_dir() and entry.name.startswith(".install.")]


def test_a_rollback_that_could_not_finish_says_so_and_keeps_the_backups(tmp_path: Path) -> None:
    # Reporting a restore that did not happen and then deleting the staging directory would take
    # the backups with it, and after a failed restore those are the only copies of the working unit.
    harness = Harness(tmp_path, "enabled", "active")
    harness.seed_units("previous")
    harness.wreck_backups_then_fail_reload()

    result = harness.run()

    assert result.returncode != 0
    assert "the previous unit files were restored" not in result.stderr
    assert "could NOT be restored" in result.stderr
    kept = _staging_directories(harness)
    assert kept, "the staging directory must survive an incomplete rollback"
    assert str(kept[0]) in result.stderr, "the recovery path has to be named"
    for directory in kept:
        directory.chmod(0o700)
        for entry in directory.iterdir():
            entry.chmod(0o600)
    backups = [entry for directory in kept for entry in directory.iterdir() if entry.name.startswith("backup.")]
    assert backups, "the backups must still be there to recover from"
    assert any(entry.read_text(encoding="utf-8").startswith("# previous") for entry in backups)


def test_a_timer_state_that_cannot_be_read_is_not_called_restored(tmp_path: Path) -> None:
    # A recorded state holding one word reads successfully with the second half empty, and applying
    # that pair is a no-op that returns success. The timer stays stopped and the run says it came
    # back, which is the false report the whole rollback exists to prevent.
    harness = Harness(tmp_path, "enabled", "active")
    harness.seed_units("previous")
    harness.truncate_recorded_state_then_fail_reload()

    result = harness.run()

    assert result.returncode != 0
    assert "the previous timer state was restored" not in result.stderr
    assert f"could NOT be restored: {TIMER}" in result.stderr
    assert harness.timer_state == ("disabled", "inactive"), "the timer really is still stopped"


def test_a_rollback_that_finished_reports_it_and_cleans_up(tmp_path: Path) -> None:
    harness = Harness(tmp_path, "enabled", "active")
    harness.seed_units("previous")
    harness.fail_reload()

    result = harness.run()

    assert result.returncode != 0
    assert "the previous unit files were restored" in result.stderr
    assert "could NOT be restored" not in result.stderr
    assert _staging_directories(harness) == [], "a completed rollback leaves nothing behind"
    assert harness.unit(SERVICE) == f"# previous {SERVICE}\n"


def _repo_copy(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    for directory in ("scripts", "deploy", "config"):
        shutil.copytree(ROOT / directory, repo / directory)
    executable = repo / ".venv" / "bin" / "2bored1made"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    return repo


def test_a_malformed_timer_is_refused_before_the_live_units_are_touched(tmp_path: Path) -> None:
    # Verifying only the services would install a timer systemd then refuses to load, and by then
    # the working files are already gone.
    repo = _repo_copy(tmp_path)
    (repo / "deploy" / "systemd" / "2bored1made-runtime.timer").write_text(
        "[Unit]\nDescription=Broken\n\n[Timer]\nOnCalendar=not-a-real-calendar-spec\n\n[Install]\nWantedBy=timers.target\n",
        encoding="utf-8",
    )
    harness = Harness(tmp_path, "enabled", "active")
    harness.seed_units("previous")

    result = harness.run(cwd=repo)

    assert result.returncode != 0
    assert "not a valid unit file" in result.stderr
    assert harness.timer_state == ("enabled", "active"), "a refusal must not stop the schedule"
    assert harness.unit(TIMER) == f"# previous {TIMER}\n"
    assert harness.unit(SERVICE) == f"# previous {SERVICE}\n"


NEWSLETTER_TIMER = "2much2read-runtime.timer"
NEWSLETTER = {
    "script": "install-2much2read-user-service.sh",
    "timers": (NEWSLETTER_TIMER,),
    "units": ("2much2read-runtime.service", NEWSLETTER_TIMER),
}
REMINDER_TIMER = "2busy1miss-runtime.timer"
AGENDA_TIMER = "2busy1miss-runtime-agenda.timer"
CALENDAR = {
    "script": "install-2busy1miss-user-service.sh",
    "timers": (REMINDER_TIMER, AGENDA_TIMER),
    "units": (
        "2busy1miss-runtime.service",
        REMINDER_TIMER,
        "2busy1miss-runtime-agenda.service",
        AGENDA_TIMER,
    ),
}


@pytest.mark.parametrize(("enabled", "active"), [("enabled", "inactive"), ("disabled", "active")])
def test_the_newsletter_installer_keeps_the_state_a_blank_answer_did_not_change(
    tmp_path: Path, enabled: str, active: str
) -> None:
    # The shared helper is covered by the matrix above; this is 2much2read's own prompt wiring.
    harness = Harness(tmp_path, enabled, active, **NEWSLETTER)

    result = harness.run(answer="\n")

    assert result.returncode == 0, result.stderr
    assert harness.timer_state == (enabled, active)


def test_the_calendar_installer_keeps_two_timers_in_different_states(tmp_path: Path) -> None:
    # One schedule paused for maintenance and one started for the session only. A blank answer must
    # leave each exactly as it was, rather than deciding for both from whichever it looked at.
    harness = Harness(tmp_path, "enabled", "active", **CALENDAR)
    harness.set_timer(REMINDER_TIMER, "enabled", "inactive")
    harness.set_timer(AGENDA_TIMER, "disabled", "active")

    result = harness.run(answer="\n")

    assert result.returncode == 0, result.stderr
    assert harness.state_of(REMINDER_TIMER) == ("enabled", "inactive")
    assert harness.state_of(AGENDA_TIMER) == ("disabled", "active")


def test_the_calendar_installer_reports_the_state_it_actually_left(tmp_path: Path) -> None:
    # Deriving the closing report from the answer told an operator whose timers had just been
    # restored that they remained disabled.
    harness = Harness(tmp_path, "enabled", "active", **CALENDAR)
    harness.set_timer(REMINDER_TIMER, "enabled", "active")
    harness.set_timer(AGENDA_TIMER, "disabled", "inactive")

    result = harness.run(answer="\n")

    assert result.returncode == 0, result.stderr
    assert harness.state_of(REMINDER_TIMER) == ("enabled", "active")
    assert harness.state_of(AGENDA_TIMER) == ("disabled", "inactive")
    assert "Reminder timer: enabled, active" in result.stdout
    assert "Agenda timer: disabled, inactive" in result.stdout
    assert "Timers remain disabled" not in result.stdout


def test_a_timer_enabled_after_the_commit_is_brought_back_down(tmp_path: Path) -> None:
    # Both timers start disabled, so the commit has nothing to stop. The operator answers yes, the
    # first timer is enabled, the second refuses, and the installation fails - leaving a timer
    # enabled and running against unit files the rollback is about to delete.
    #
    # Three separate defects had to hold for this to come back: the restore was guarded on whether
    # the commit had stopped a timer, applying a state could only ever move a timer up, and the
    # units were deleted before the timers were put right.
    harness = Harness(tmp_path, "disabled", "inactive", **CALENDAR)
    harness.fail_enable(AGENDA_TIMER)

    result = harness.run(answer="y\n")

    assert result.returncode != 0, "an installation that could not finish must not report success"
    assert harness.state_of(REMINDER_TIMER) == ("disabled", "inactive")
    assert harness.state_of(AGENDA_TIMER) == ("disabled", "inactive")
    log = harness.log.read_text(encoding="utf-8")
    assert f"unit-present {REMINDER_TIMER}" in log, "the timer has to come down while its unit is still there"
    assert "could NOT be restored" not in result.stderr


def test_the_calendar_installer_enables_both_when_confirmed(tmp_path: Path) -> None:
    harness = Harness(tmp_path, "disabled", "inactive", **CALENDAR)

    result = harness.run(answer="y\n")

    assert result.returncode == 0, result.stderr
    assert harness.state_of(REMINDER_TIMER) == ("enabled", "active")
    assert harness.state_of(AGENDA_TIMER) == ("enabled", "active")
    assert "Reminder timer: enabled, active" in result.stdout
    assert "Agenda timer: enabled, active" in result.stdout
