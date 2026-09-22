import fcntl
import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("script", "env_name"),
    [
        ("install-2much2read-user-service.sh", ".2much2read.env"),
        ("install-2busy1miss-user-service.sh", ".2busy1miss.env"),
    ],
)
def test_installers_refuse_managed_env_symlinks(tmp_path: Path, script: str, env_name: str) -> None:
    root = Path(__file__).parents[1]
    home = tmp_path / "home"
    config_root = home / ".config" / "2much2read-runtime"
    config_root.mkdir(parents=True)
    target = tmp_path / "outside.env"
    target.write_text("secret", encoding="utf-8")
    target.chmod(0o644)
    (config_root / env_name).symlink_to(target)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        '#!/bin/sh\n[ "$2" = "is-active" ] && exit 3\n[ "$2" = "show" ] && printf "inactive\\n"\nexit 0\n',
        encoding="utf-8",
    )
    systemctl.chmod(0o755)

    result = subprocess.run(
        ["sh", f"scripts/{script}"],
        cwd=root,
        env=os.environ | {"HOME": str(home), "PATH": f"{fake_bin}:{os.environ['PATH']}"},
        text=True,
        capture_output=True,
        input="\n",
    )

    assert result.returncode == 1
    assert "symbolic link" in result.stderr
    assert target.stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize(
    ("script", "unit", "env_name", "env_contents"),
    [
        (
            "install-2much2read-user-service.sh",
            "2much2read-runtime.service",
            ".2much2read.env",
            "DIGEST_SCHEDULE_TIME=08:00\nDIGEST_SCHEDULE_TIMEZONE=America/Montreal\n",
        ),
        (
            "install-2busy1miss-user-service.sh",
            "2busy1miss-runtime.service",
            ".2busy1miss.env",
            "AGENDA_SCHEDULE_TIME=21:00\n",
        ),
    ],
)
def test_service_installers_refuse_unit_symlinks(
    tmp_path: Path, script: str, unit: str, env_name: str, env_contents: str
) -> None:
    root = Path(__file__).parents[1]
    home = tmp_path / "home"
    config_root = home / ".config/2much2read-runtime"
    systemd_root = home / ".config/systemd/user"
    config_root.mkdir(parents=True)
    systemd_root.mkdir(parents=True)
    (config_root / env_name).write_text(env_contents, encoding="utf-8")
    target = tmp_path / "outside.service"
    target.write_text("preserve", encoding="utf-8")
    (systemd_root / unit).symlink_to(target)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        '#!/bin/sh\n[ "$2" = "is-active" ] && exit 3\n[ "$2" = "show" ] && printf "inactive\\n"\nexit 0\n',
        encoding="utf-8",
    )
    systemctl.chmod(0o755)

    result = subprocess.run(
        ["sh", f"scripts/{script}"],
        cwd=root,
        env=os.environ | {"HOME": str(home), "PATH": f"{fake_bin}:{os.environ['PATH']}"},
        text=True,
        capture_output=True,
        input="\n",
    )

    assert result.returncode == 1
    assert "symbolic link" in result.stderr
    assert target.read_text(encoding="utf-8") == "preserve"


@pytest.mark.parametrize(
    ("script", "timer", "service", "secret_option", "secret_name", "answer", "starts"),
    [
        (
            "install-2much2read-user-service.sh",
            "2much2read-runtime.timer",
            "2much2read-runtime.service",
            "--gmail-client-secret",
            "gmail-client-secret.json",
            "",
            False,
        ),
        (
            "install-2much2read-user-service.sh",
            "2much2read-runtime.timer",
            "2much2read-runtime.service",
            "--gmail-client-secret",
            "gmail-client-secret.json",
            "y\n",
            True,
        ),
        (
            "install-2busy1miss-user-service.sh",
            "2busy1miss-runtime.timer",
            "2busy1miss-runtime.service",
            "--calendar-client-secret",
            "calendar-client-secret.json",
            "",
            False,
        ),
        (
            "install-2busy1miss-user-service.sh",
            "2busy1miss-runtime.timer",
            "2busy1miss-runtime.service",
            "--calendar-client-secret",
            "calendar-client-secret.json",
            "y\n",
            True,
        ),
    ],
)
def test_installers_only_start_timers_when_confirmed(
    tmp_path: Path,
    script: str,
    timer: str,
    service: str,
    secret_option: str,
    secret_name: str,
    answer: str,
    starts: bool,
) -> None:
    root = Path(__file__).parents[1]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "systemctl.log"
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$SYSTEMCTL_LOG"\n[ "$2" = "is-active" ] && exit 3\n'
        '[ "$2" = "show" ] && printf "inactive\\n"\nexit 0\n',
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
        input=answer,
    )

    calls = log.read_text(encoding="utf-8")
    disable_call = f"disable --now {timer}"
    state_call = f"show --property=ActiveState --value {service}"
    assert disable_call in calls
    assert state_call in calls
    assert calls.index(disable_call) < calls.index(state_call)
    assert "daemon-reload" in calls
    assert ("enable --now" in calls) is starts
    installed_secret = tmp_path / "home" / ".config" / "2much2read-runtime" / secret_name
    assert installed_secret.read_text(encoding="utf-8") == "client secret"
    assert installed_secret.stat().st_mode & 0o777 == 0o600
    if script == "install-2busy1miss-user-service.sh":
        assert "disable --now 2busy1miss-runtime-agenda.timer" in calls
        if starts:
            assert "enable --now 2busy1miss-runtime.timer 2busy1miss-runtime-agenda.timer" in calls
            assert "Timers enabled." in result.stdout
        else:
            assert (
                "Timers remain disabled. Enable reminders when ready: systemctl --user enable --now 2busy1miss-runtime.timer"
                in result.stdout
            )
            assert "Enable agenda when ready: systemctl --user enable --now 2busy1miss-runtime-agenda.timer" in result.stdout
        agenda_timer = tmp_path / "home" / ".config" / "systemd" / "user" / "2busy1miss-runtime-agenda.timer"
        assert "OnCalendar=*-*-* 21:00:00 America/Montreal" in agenda_timer.read_text(encoding="utf-8")
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
            input=answer,
        )
        assert "OnCalendar=*-*-* 20:30:00 America/Montreal" in agenda_timer.read_text(encoding="utf-8")
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
            input=answer,
        )
        assert "OnCalendar=*-*-* 21:00:00 America/Montreal" in agenda_timer.read_text(encoding="utf-8")
    else:
        newsletter_timer = tmp_path / "home" / ".config" / "systemd" / "user" / "2much2read-runtime.timer"
        assert "OnCalendar=*-*-* 08:00:00 America/Montreal" in newsletter_timer.read_text(encoding="utf-8")
        newsletter_env = tmp_path / "home" / ".config" / "2much2read-runtime" / ".2much2read.env"
        newsletter_env.write_text("DIGEST_SCHEDULE_TIME=09:45\nDIGEST_SCHEDULE_TIMEZONE=America/Toronto\n", encoding="utf-8")
        subprocess.run(
            ["sh", f"scripts/{script}", secret_option, str(client_secret)],
            cwd=root,
            env=environment,
            check=True,
            text=True,
            capture_output=True,
            input=answer,
        )
        assert "OnCalendar=*-*-* 09:45:00 America/Toronto" in newsletter_timer.read_text(encoding="utf-8")
        if starts:
            assert f"enable --now {timer}" in calls
        expected = (
            "Timer enabled." if starts else f"Timer remains disabled. Enable when ready: systemctl --user enable --now {timer}"
        )
        assert expected in result.stdout


@pytest.mark.parametrize(
    ("setting", "message"),
    [
        ("DIGEST_SCHEDULE_TIME=25:00\n", "DIGEST_SCHEDULE_TIME must use HH:MM"),
        ("DIGEST_SCHEDULE_TIMEZONE=Invalid/Timezone\n", "DIGEST_SCHEDULE_TIMEZONE must name a system timezone"),
    ],
)
def test_newsletter_installer_rejects_invalid_schedule(tmp_path: Path, setting: str, message: str) -> None:
    root = Path(__file__).parents[1]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        '#!/bin/sh\n[ "$2" = "is-active" ] && exit 3\n[ "$2" = "show" ] && printf "inactive\\n"\nexit 0\n',
        encoding="utf-8",
    )
    systemctl.chmod(0o755)
    home = tmp_path / "home"
    config_dir = home / ".config" / "2much2read-runtime"
    config_dir.mkdir(parents=True)
    (config_dir / ".2much2read.env").write_text(setting, encoding="utf-8")
    client_secret = tmp_path / "client-secret.json"
    client_secret.write_text("client secret", encoding="utf-8")

    result = subprocess.run(
        ["sh", "scripts/install-2much2read-user-service.sh", "--gmail-client-secret", str(client_secret)],
        cwd=root,
        env=os.environ | {"HOME": str(home), "PATH": f"{fake_bin}:{os.environ['PATH']}"},
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    assert message in result.stderr
    assert not (home / ".config" / "systemd" / "user" / "2much2read-runtime.timer").exists()


@pytest.mark.parametrize(
    ("script", "units", "disable_call", "stop_call"),
    [
        (
            "uninstall-2much2read-user-service.sh",
            ["2much2read-runtime.service", "2much2read-runtime.timer"],
            "disable --now 2much2read-runtime.timer",
            "stop 2much2read-runtime.service",
        ),
        (
            "uninstall-2busy1miss-user-service.sh",
            [
                "2busy1miss-runtime.service",
                "2busy1miss-runtime.timer",
                "2busy1miss-runtime-agenda.service",
                "2busy1miss-runtime-agenda.timer",
            ],
            "disable --now 2busy1miss-runtime.timer 2busy1miss-runtime-agenda.timer",
            "stop 2busy1miss-runtime.service 2busy1miss-runtime-agenda.service",
        ),
    ],
)
def test_uninstallers_remove_only_their_unit_files(
    tmp_path: Path, script: str, units: list[str], disable_call: str, stop_call: str | None
) -> None:
    root = Path(__file__).parents[1]
    systemd_dir = tmp_path / "home" / ".config" / "systemd" / "user"
    systemd_dir.mkdir(parents=True)
    for unit in units:
        (systemd_dir / unit).write_text("owned", encoding="utf-8")
    preserved = systemd_dir / "unrelated.timer"
    preserved.write_text("keep", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "systemctl.log"
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$SYSTEMCTL_LOG"\n[ "$2" = "is-active" ] && exit 3\nexit 0\n',
        encoding="utf-8",
    )
    systemctl.chmod(0o755)

    subprocess.run(
        ["sh", f"scripts/{script}"],
        cwd=root,
        env=os.environ | {"HOME": str(tmp_path / "home"), "PATH": f"{fake_bin}:{os.environ['PATH']}", "SYSTEMCTL_LOG": str(log)},
        check=True,
        text=True,
        capture_output=True,
    )

    assert all(not (systemd_dir / unit).exists() for unit in units)
    assert preserved.read_text(encoding="utf-8") == "keep"
    calls = log.read_text(encoding="utf-8")
    assert disable_call in calls
    if stop_call is not None:
        assert stop_call in calls
    assert "daemon-reload" in calls


@pytest.mark.parametrize(
    ("script", "units"),
    [
        (
            "uninstall-2much2read-user-service.sh",
            ["2much2read-runtime.service", "2much2read-runtime.timer"],
        ),
        (
            "uninstall-2busy1miss-user-service.sh",
            [
                "2busy1miss-runtime.service",
                "2busy1miss-runtime.timer",
                "2busy1miss-runtime-agenda.service",
                "2busy1miss-runtime-agenda.timer",
            ],
        ),
    ],
)
def test_uninstallers_keep_unit_files_when_a_service_will_not_stop(tmp_path: Path, script: str, units: list[str]) -> None:
    root = Path(__file__).parents[1]
    systemd_dir = tmp_path / "home" / ".config" / "systemd" / "user"
    systemd_dir.mkdir(parents=True)
    for unit in units:
        (systemd_dir / unit).write_text("owned", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        '#!/bin/sh\n[ "$2" = "stop" ] && exit 1\n[ "$2" = "is-active" ] && exit 0\nexit 0\n',
        encoding="utf-8",
    )
    systemctl.chmod(0o755)

    result = subprocess.run(
        ["sh", f"scripts/{script}"],
        cwd=root,
        env=os.environ | {"HOME": str(tmp_path / "home"), "PATH": f"{fake_bin}:{os.environ['PATH']}"},
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    assert "active" in result.stderr
    assert all((systemd_dir / unit).exists() for unit in units)


@pytest.mark.parametrize(
    ("script", "app", "env_name", "yaml_name", "secret_name", "token_name", "sqlite_name", "lock_name"),
    [
        (
            "install-2much2read-user-service.sh",
            "2much2read",
            ".2much2read.env",
            "sources.yaml",
            "gmail-client-secret.json",
            "gmail-token.json",
            "2much2read.sqlite3",
            "2much2read.lock",
        ),
        (
            "install-2busy1miss-user-service.sh",
            "2busy1miss",
            ".2busy1miss.env",
            "reminders.yaml",
            "calendar-client-secret.json",
            "calendar-token.json",
            "2busy1miss.sqlite3",
            "2busy1miss.lock",
        ),
    ],
)
def test_installers_migrate_legacy_files_and_repair_modes(
    tmp_path: Path,
    script: str,
    app: str,
    env_name: str,
    yaml_name: str,
    secret_name: str,
    token_name: str,
    sqlite_name: str,
    lock_name: str,
) -> None:
    root = Path(__file__).parents[1]
    home = tmp_path / "home"
    config_root = home / ".config" / "2much2read-runtime"
    data_root = home / ".local" / "share" / "2much2read-runtime"
    config_root.mkdir(parents=True)
    data_root.mkdir(parents=True)
    legacy_files = {
        env_name: "DIGEST_SCHEDULE_TIME=08:00\nDIGEST_SCHEDULE_TIMEZONE=America/Montreal\n"
        if app == "2much2read"
        else "AGENDA_SCHEDULE_TIME=21:00\n",
        yaml_name: "legacy yaml\n",
        secret_name: "legacy client secret\n",
        token_name: "legacy token\n",
        sqlite_name: "legacy database\n",
        f"{sqlite_name}-wal": "legacy wal\n",
        f"{sqlite_name}-shm": "legacy shm\n",
        f"{sqlite_name}-journal": "legacy journal\n",
    }
    for name, contents in legacy_files.items():
        parent = config_root if name in {env_name, yaml_name, secret_name, token_name} else data_root
        path = parent / name
        path.write_text(contents, encoding="utf-8")
        path.chmod(0o644)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        '#!/bin/sh\n[ "$2" = "is-active" ] && exit 3\n[ "$2" = "show" ] && printf "inactive\\n"\nexit 0\n',
        encoding="utf-8",
    )
    systemctl.chmod(0o755)

    subprocess.run(
        ["sh", f"scripts/{script}"],
        cwd=root,
        env=os.environ | {"HOME": str(home), "PATH": f"{fake_bin}:{os.environ['PATH']}"},
        check=True,
        text=True,
        capture_output=True,
        input="\n",
    )

    app_config = config_root / app
    app_data = data_root / app
    assert app_config.stat().st_mode & 0o777 == 0o700
    assert app_data.stat().st_mode & 0o777 == 0o700
    # The lock is recreated at the scoped path rather than migrated.
    assert (app_data / lock_name).stat().st_mode & 0o777 == 0o600
    managed_files: list[Path] = []
    for name, contents in legacy_files.items():
        if name == token_name:
            target_root = app_config
            assert not (config_root / name).exists()
        elif name in {env_name, yaml_name, secret_name}:
            target_root = config_root
            assert (config_root / name).exists()
        else:
            target_root = app_data
            assert not (data_root / name).exists()
        target = target_root / name
        if name != lock_name:
            assert target.read_text(encoding="utf-8") == contents
        assert target.stat().st_mode & 0o777 == 0o600
        managed_files.append(target)

    for path in managed_files:
        path.chmod(0o644)
    app_config.chmod(0o755)
    app_data.chmod(0o755)
    subprocess.run(
        ["sh", f"scripts/{script}"],
        cwd=root,
        env=os.environ | {"HOME": str(home), "PATH": f"{fake_bin}:{os.environ['PATH']}"},
        check=True,
        text=True,
        capture_output=True,
        input="\n",
    )
    assert app_config.stat().st_mode & 0o777 == 0o700
    assert app_data.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in managed_files)


@pytest.mark.parametrize(
    ("script", "app", "legacy_name", "target_name", "legacy_root", "target_root"),
    [
        (
            "install-2much2read-user-service.sh",
            "2much2read",
            "gmail-token.json",
            "gmail-token.json",
            "config",
            "config",
        ),
        (
            "install-2much2read-user-service.sh",
            "2much2read",
            "2much2read.sqlite3",
            "2much2read.sqlite3",
            "data",
            "data",
        ),
        (
            "install-2much2read-user-service.sh",
            "2much2read",
            "2much2read.sqlite3-wal",
            "2much2read.sqlite3",
            "data",
            "data",
        ),
        (
            "install-2busy1miss-user-service.sh",
            "2busy1miss",
            "calendar-token.json",
            "calendar-token.json",
            "config",
            "config",
        ),
        (
            "install-2busy1miss-user-service.sh",
            "2busy1miss",
            "2busy1miss.sqlite3",
            "2busy1miss.sqlite3",
            "data",
            "data",
        ),
        (
            "install-2busy1miss-user-service.sh",
            "2busy1miss",
            "2busy1miss.sqlite3-wal",
            "2busy1miss.sqlite3",
            "data",
            "data",
        ),
    ],
)
def test_installers_refuse_legacy_new_conflicts(
    tmp_path: Path,
    script: str,
    app: str,
    legacy_name: str,
    target_name: str,
    legacy_root: str,
    target_root: str,
) -> None:
    root = Path(__file__).parents[1]
    home = tmp_path / "home"
    old_root = home / (".config/2much2read-runtime" if legacy_root == "config" else ".local/share/2much2read-runtime")
    new_root = old_root / app
    old_root.mkdir(parents=True)
    new_root.mkdir()
    old_file = old_root / legacy_name
    new_file = new_root / target_name
    old_file.write_text("old", encoding="utf-8")
    new_file.write_text("new", encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        '#!/bin/sh\n[ "$2" = "is-active" ] && exit 3\n[ "$2" = "show" ] && printf "inactive\\n"\nexit 0\n',
        encoding="utf-8",
    )
    systemctl.chmod(0o755)

    result = subprocess.run(
        ["sh", f"scripts/{script}"],
        cwd=root,
        env=os.environ | {"HOME": str(home), "PATH": f"{fake_bin}:{os.environ['PATH']}"},
        text=True,
        capture_output=True,
        input="\n",
    )

    assert result.returncode == 1
    assert "old and new files both exist" in result.stderr
    assert old_file.read_text(encoding="utf-8") == "old"
    assert new_file.read_text(encoding="utf-8") == "new"


@pytest.mark.parametrize(
    ("script", "app", "sqlite_name", "lock_name"),
    [
        ("install-2much2read-user-service.sh", "2much2read", "2much2read.sqlite3", "2much2read.lock"),
        ("install-2busy1miss-user-service.sh", "2busy1miss", "2busy1miss.sqlite3", "2busy1miss.lock"),
    ],
)
def test_installers_do_not_migrate_while_runtime_lock_is_held(
    tmp_path: Path,
    script: str,
    app: str,
    sqlite_name: str,
    lock_name: str,
) -> None:
    root = Path(__file__).parents[1]
    home = tmp_path / "home"
    data_root = home / ".local" / "share" / "2much2read-runtime"
    app_data = data_root / app
    app_data.mkdir(parents=True)
    legacy_database = data_root / sqlite_name
    legacy_database.write_text("legacy database", encoding="utf-8")
    lock_path = app_data / lock_name
    lock_path.touch()

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        '#!/bin/sh\n[ "$2" = "is-active" ] && exit 3\n[ "$2" = "show" ] && printf "inactive\\n"\nexit 0\n',
        encoding="utf-8",
    )
    systemctl.chmod(0o755)

    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(
            ["sh", f"scripts/{script}"],
            cwd=root,
            env=os.environ | {"HOME": str(home), "PATH": f"{fake_bin}:{os.environ['PATH']}"},
            text=True,
            capture_output=True,
            input="\n",
        )

    assert result.returncode == 1
    assert "runtime lock is held" in result.stderr
    assert legacy_database.read_text(encoding="utf-8") == "legacy database"
    assert not (app_data / sqlite_name).exists()


@pytest.mark.parametrize(
    ("script", "app", "env_name", "yaml_name", "secret_name", "token_name", "sqlite_name", "lock_name"),
    [
        (
            "install-2much2read-user-service.sh",
            "2much2read",
            ".2much2read.env",
            "sources.yaml",
            "gmail-client-secret.json",
            "gmail-token.json",
            "2much2read.sqlite3",
            "2much2read.lock",
        ),
        (
            "install-2busy1miss-user-service.sh",
            "2busy1miss",
            ".2busy1miss.env",
            "reminders.yaml",
            "calendar-client-secret.json",
            "calendar-token.json",
            "2busy1miss.sqlite3",
            "2busy1miss.lock",
        ),
    ],
)
@pytest.mark.parametrize(
    ("systemctl_body", "message"),
    [
        ('[ "$2" = "is-active" ] && exit 0\n[ "$2" = "show" ] && printf "active\\n"\nexit 0\n', "stop"),
        ('[ "$2" = "is-active" ] && exit 3\n[ "$2" = "show" ] && printf "activating\\n"\nexit 0\n', "stop"),
        ('[ "$2" = "is-active" ] && exit 1\nexit 0\n', "cannot determine"),
        ('[ "$2" = "is-active" ] && exit 3\n[ "$2" = "disable" ] && exit 1\nexit 0\n', "failed to stop"),
        ('[ "$2" = "is-active" ] && exit 3\n[ "$2" = "show" ] && exit 1\nexit 0\n', "cannot determine"),
    ],
)
def test_installers_do_not_migrate_when_runtime_state_is_unsafe(
    tmp_path: Path,
    script: str,
    app: str,
    env_name: str,
    yaml_name: str,
    secret_name: str,
    token_name: str,
    sqlite_name: str,
    lock_name: str,
    systemctl_body: str,
    message: str,
) -> None:
    root = Path(__file__).parents[1]
    home = tmp_path / "home"
    config_root = home / ".config" / "2much2read-runtime"
    data_root = home / ".local" / "share" / "2much2read-runtime"
    config_root.mkdir(parents=True)
    data_root.mkdir(parents=True)
    (config_root / token_name).write_text("legacy token", encoding="utf-8")
    (data_root / sqlite_name).write_text("legacy database", encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(f"#!/bin/sh\n{systemctl_body}", encoding="utf-8")
    systemctl.chmod(0o755)

    result = subprocess.run(
        ["sh", f"scripts/{script}"],
        cwd=root,
        env=os.environ | {"HOME": str(home), "PATH": f"{fake_bin}:{os.environ['PATH']}"},
        text=True,
        capture_output=True,
        input="\n",
    )

    assert result.returncode == 1
    assert message in result.stderr
    assert (config_root / token_name).exists()
    assert (data_root / sqlite_name).exists()
    assert not (config_root / app / token_name).exists()
    assert not (data_root / app / sqlite_name).exists()


def test_agenda_timer_takes_the_timezone_reminders_yaml_actually_uses(tmp_path: Path) -> None:
    """reminders.yaml wins over REMINDER_TIMEZONE in the command, so the timer has to agree.

    Taking the environment file alone would put the timer in one zone and the command's
    before_schedule guard in another - the same disagreement, moved rather than removed.
    """
    root = Path(__file__).parents[1]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        '#!/bin/sh\n[ "$2" = "is-active" ] && exit 3\n[ "$2" = "show" ] && printf "inactive\\n"\nexit 0\n',
        encoding="utf-8",
    )
    systemctl.chmod(0o755)
    home = tmp_path / "home"
    config_root = home / ".config/2much2read-runtime"
    config_root.mkdir(parents=True)
    (config_root / ".2busy1miss.env").write_text(
        "AGENDA_SCHEDULE_TIME=21:00\nREMINDER_TIMEZONE=Europe/Berlin\n", encoding="utf-8"
    )
    (config_root / "reminders.yaml").write_text(
        "timezone: Asia/Taipei\n\ncalendars:\n  - id: primary\n    name: Main\n", encoding="utf-8"
    )
    client_secret = tmp_path / "client-secret.json"
    client_secret.write_text("client secret", encoding="utf-8")

    subprocess.run(
        ["sh", "scripts/install-2busy1miss-user-service.sh", "--calendar-client-secret", str(client_secret)],
        cwd=root,
        env=os.environ | {"HOME": str(home), "PATH": f"{fake_bin}:{os.environ['PATH']}"},
        check=True,
        text=True,
        capture_output=True,
        input="n\n",
    )

    timer = (home / ".config/systemd/user/2busy1miss-runtime-agenda.timer").read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 21:00:00 Asia/Taipei" in timer


def test_agenda_timer_falls_back_to_the_environment_timezone(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        '#!/bin/sh\n[ "$2" = "is-active" ] && exit 3\n[ "$2" = "show" ] && printf "inactive\\n"\nexit 0\n',
        encoding="utf-8",
    )
    systemctl.chmod(0o755)
    home = tmp_path / "home"
    config_root = home / ".config/2much2read-runtime"
    config_root.mkdir(parents=True)
    (config_root / ".2busy1miss.env").write_text(
        "AGENDA_SCHEDULE_TIME=07:15\nREMINDER_TIMEZONE=Europe/Berlin\n", encoding="utf-8"
    )
    (config_root / "reminders.yaml").write_text("calendars:\n  - id: primary\n    name: Main\n", encoding="utf-8")
    client_secret = tmp_path / "client-secret.json"
    client_secret.write_text("client secret", encoding="utf-8")

    subprocess.run(
        ["sh", "scripts/install-2busy1miss-user-service.sh", "--calendar-client-secret", str(client_secret)],
        cwd=root,
        env=os.environ | {"HOME": str(home), "PATH": f"{fake_bin}:{os.environ['PATH']}"},
        check=True,
        text=True,
        capture_output=True,
        input="n\n",
    )

    timer = (home / ".config/systemd/user/2busy1miss-runtime-agenda.timer").read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 07:15:00 Europe/Berlin" in timer
