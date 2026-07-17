import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("script", "timer", "service"),
    [
        ("install-2much2read-user-service.sh", "2much2read.timer", "2much2read.service"),
        ("install-2busy1miss-user-service.sh", "2busy1miss.timer", "2busy1miss.service"),
    ],
)
def test_installers_leave_timers_disabled(tmp_path: Path, script: str, timer: str, service: str) -> None:
    root = Path(__file__).parents[1]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "systemctl.log"
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$SYSTEMCTL_LOG"\n[ "$2" = "is-active" ] && exit 3\nexit 0\n',
        encoding="utf-8",
    )
    systemctl.chmod(0o755)
    environment = os.environ | {
        "HOME": str(tmp_path / "home"),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "SYSTEMCTL_LOG": str(log),
    }

    result = subprocess.run(["sh", f"scripts/{script}"], cwd=root, env=environment, check=True, text=True, capture_output=True)

    calls = log.read_text(encoding="utf-8")
    assert f"disable --now {timer}" in calls
    assert f"is-active --quiet {service}" in calls
    assert "daemon-reload" in calls
    assert "enable --now" not in calls
    if script == "install-2busy1miss-user-service.sh":
        assert "disable --now 2busy1miss.timer 2busy1miss-agenda.timer" in calls
        assert "Enable reminders when ready: systemctl --user enable --now 2busy1miss.timer" in result.stdout
        assert "Enable agenda when ready: systemctl --user enable --now 2busy1miss-agenda.timer" in result.stdout
    else:
        assert f"Enable when ready: systemctl --user enable --now {timer}" in result.stdout


def test_2busy1miss_agenda_timer_runs_at_local_2100() -> None:
    root = Path(__file__).parents[1]
    timer = (root / "deploy/systemd/2busy1miss-agenda.timer").read_text(encoding="utf-8")

    assert "OnCalendar=*-*-* 21:00:00" in timer
