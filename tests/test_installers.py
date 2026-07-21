import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("script", "timer", "service", "secret_option", "secret_name"),
    [
        (
            "install-2much2read-user-service.sh",
            "2much2read-runtime.timer",
            "2much2read-runtime.service",
            "--gmail-client-secret",
            "gmail-client-secret.json",
        ),
        (
            "install-2busy1miss-user-service.sh",
            "2busy1miss-runtime.timer",
            "2busy1miss-runtime.service",
            "--calendar-client-secret",
            "calendar-client-secret.json",
        ),
    ],
)
def test_installers_leave_timers_disabled(
    tmp_path: Path, script: str, timer: str, service: str, secret_option: str, secret_name: str
) -> None:
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
    client_secret = tmp_path / "client-secret.json"
    client_secret.write_text("client secret", encoding="utf-8")

    result = subprocess.run(
        ["sh", f"scripts/{script}", secret_option, str(client_secret)],
        cwd=root,
        env=environment,
        check=True,
        text=True,
        capture_output=True,
    )

    calls = log.read_text(encoding="utf-8")
    assert f"disable --now {timer}" in calls
    assert f"is-active --quiet {service}" in calls
    assert "daemon-reload" in calls
    assert "enable --now" not in calls
    installed_secret = tmp_path / "home" / ".config" / "2much2read-runtime" / secret_name
    assert installed_secret.read_text(encoding="utf-8") == "client secret"
    assert installed_secret.stat().st_mode & 0o777 == 0o600
    if script == "install-2busy1miss-user-service.sh":
        assert "disable --now 2busy1miss-runtime.timer 2busy1miss-runtime-agenda.timer" in calls
        assert "Enable reminders when ready: systemctl --user enable --now 2busy1miss-runtime.timer" in result.stdout
        assert "Enable agenda when ready: systemctl --user enable --now 2busy1miss-runtime-agenda.timer" in result.stdout
        agenda_timer = tmp_path / "home" / ".config" / "systemd" / "user" / "2busy1miss-runtime-agenda.timer"
        assert "OnCalendar=*-*-* 21:00:00" in agenda_timer.read_text(encoding="utf-8")
        (tmp_path / "home" / ".config" / "2much2read-runtime" / ".2busy1miss.env").write_text(
            "AGENDA_SCHEDULE_TIME=20:30\n", encoding="utf-8"
        )
        subprocess.run(
            ["sh", f"scripts/{script}", secret_option, str(client_secret)],
            cwd=root,
            env=environment,
            check=True,
            text=True,
            capture_output=True,
        )
        assert "OnCalendar=*-*-* 20:30:00" in agenda_timer.read_text(encoding="utf-8")
        (tmp_path / "home" / ".config" / "2much2read-runtime" / ".2busy1miss.env").write_text(
            "DISCORD_WEBHOOK_URL=\n", encoding="utf-8"
        )
        subprocess.run(
            ["sh", f"scripts/{script}", secret_option, str(client_secret)],
            cwd=root,
            env=environment,
            check=True,
            text=True,
            capture_output=True,
        )
        assert "OnCalendar=*-*-* 21:00:00" in agenda_timer.read_text(encoding="utf-8")
    else:
        assert f"Enable when ready: systemctl --user enable --now {timer}" in result.stdout


def test_2busy1miss_agenda_timer_is_an_installer_template() -> None:
    root = Path(__file__).parents[1]
    timer = (root / "deploy/systemd/2busy1miss-runtime-agenda.timer").read_text(encoding="utf-8")
    service = (root / "deploy/systemd/2busy1miss-runtime-agenda.service").read_text(encoding="utf-8")

    assert "OnCalendar=*-*-* __AGENDA_SCHEDULE_TIME__:00" in timer
    assert "Persistent=true" in timer
    assert "ExecStart=__EXECUTABLE__ agenda-next-day --scheduled" in service


def test_2busy1miss_dispatcher_runs_every_minute() -> None:
    timer = (Path(__file__).parents[1] / "deploy/systemd/2busy1miss-runtime.timer").read_text(encoding="utf-8")

    assert "OnCalendar=*-*-* *:*:00" in timer
    assert "RandomizedDelaySec" not in timer


def test_legacy_cleanup_is_idempotent_and_preserves_new_runtime(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    checkout = tmp_path / "checkout"
    script_dir = checkout / "scripts"
    script_dir.mkdir(parents=True)
    cleanup = script_dir / "legacy_cleanup.sh"
    shutil.copy(root / "scripts/legacy_cleanup.sh", cleanup)

    home = tmp_path / "home"
    legacy_roots = [
        home / ".config" / "2Much2Read",
        home / ".config" / "2much2read",
        home / ".config" / "newsletter-digest",
        home / ".config" / "2busy1miss",
        home / ".local" / "share" / "2Much2Read",
        home / ".local" / "share" / "2much2read",
        home / ".local" / "share" / "newsletter-digest",
        home / ".local" / "share" / "2busy1miss",
    ]
    for directory in legacy_roots:
        directory.mkdir(parents=True)
        (directory / "state").write_text("legacy", encoding="utf-8")
    (checkout / ".env").write_text("legacy", encoding="utf-8")

    systemd_dir = home / ".config" / "systemd" / "user"
    systemd_dir.mkdir(parents=True)
    legacy_units = [
        "newsletter-digest.timer",
        "newsletter-digest.service",
        "2much2read.timer",
        "2much2read.service",
        "2busy1miss.timer",
        "2busy1miss.service",
        "2busy1miss-agenda.timer",
        "2busy1miss-agenda.service",
    ]
    for unit in legacy_units:
        (systemd_dir / unit).write_text("legacy", encoding="utf-8")

    runtime_config = home / ".config" / "2much2read-runtime"
    runtime_data = home / ".local" / "share" / "2much2read-runtime"
    runtime_config.mkdir(parents=True)
    runtime_data.mkdir(parents=True)
    (runtime_config / "state").write_text("new", encoding="utf-8")
    (runtime_data / "state").write_text("new", encoding="utf-8")
    runtime_unit = systemd_dir / "2much2read-runtime.timer"
    runtime_unit.write_text("new", encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "systemctl.log"
    systemctl = fake_bin / "systemctl"
    systemctl.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$SYSTEMCTL_LOG"\n', encoding="utf-8")
    systemctl.chmod(0o755)
    environment = os.environ | {
        "HOME": str(home),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "SYSTEMCTL_LOG": str(log),
    }

    for _ in range(2):
        subprocess.run(["sh", str(cleanup)], cwd=checkout, env=environment, check=True)

    assert all(not directory.exists() for directory in legacy_roots)
    assert not (checkout / ".env").exists()
    assert (runtime_config / "state").read_text(encoding="utf-8") == "new"
    assert (runtime_data / "state").read_text(encoding="utf-8") == "new"
    assert runtime_unit.read_text(encoding="utf-8") == "new"
    calls = log.read_text(encoding="utf-8")
    for unit in legacy_units:
        assert calls.count(f"--user disable --now {unit}") == 2
    assert calls.count("--user daemon-reload") == 2
