"""Assertions about the unit files themselves, independent of who installs them.

These used to live in test_installers.py, which is why 2bored1made was never covered: it has no
installer, so nothing in that file mentioned it and its service unit went unchecked by the sandbox
assertions every other unit had to pass. What a unit declares and how it gets onto disk are
separate questions, and only the second one is about installers.
"""

from pathlib import Path

import pytest

UNITS = Path(__file__).parents[1] / "deploy" / "systemd"

# The sandbox every runtime service has to declare. ReadWritePaths and EnvironmentFile differ per
# unit and are checked separately.
REQUIRED_SANDBOX = {
    "UMask=0077",
    "ProtectSystem=strict",
    "ProtectHome=read-only",
    "NoNewPrivileges=true",
    "PrivateTmp=true",
    "ProtectKernelTunables=true",
    "ProtectKernelModules=true",
    "ProtectKernelLogs=true",
    "ProtectControlGroups=true",
    "LockPersonality=true",
    "RestrictSUIDSGID=true",
    "RestrictRealtime=true",
    "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
    "RestrictNamespaces=true",
    "SystemCallArchitectures=native",
    "SystemCallFilter=@system-service",
    "SystemCallFilter=~@privileged @resources",
}

CONFIG_ROOT = "%h/.config/2much2read-runtime"
DATA_ROOT = "%h/.local/share/2much2read-runtime"


@pytest.mark.parametrize(
    ("unit", "env_name", "read_write_paths"),
    [
        ("2much2read-runtime.service", "2much2read", f"{CONFIG_ROOT}/2much2read {DATA_ROOT}/2much2read"),
        ("2busy1miss-runtime.service", "2busy1miss", f"{CONFIG_ROOT}/2busy1miss {DATA_ROOT}/2busy1miss"),
        ("2busy1miss-runtime-agenda.service", "2busy1miss", f"{CONFIG_ROOT}/2busy1miss {DATA_ROOT}/2busy1miss"),
        # 2bored1made refreshes no OAuth token, so it needs no writable config directory - which is
        # also why this could not be derived from the application name like the other three.
        ("2bored1made-runtime.service", "2bored1made", f"{DATA_ROOT}/2bored1made"),
    ],
)
def test_runtime_units_use_the_runtime_sandbox(unit: str, env_name: str, read_write_paths: str) -> None:
    service = (UNITS / unit).read_text(encoding="utf-8")
    lines = service.splitlines()

    assert set(lines) >= REQUIRED_SANDBOX
    assert f"ReadWritePaths={read_write_paths}" in lines
    assert f"EnvironmentFile={CONFIG_ROOT}/.{env_name}.env" in service
    assert sum(line.startswith("ReadWritePaths=") for line in lines) == 1
    if env_name == "2much2read":
        assert f"Environment=HF_HOME={DATA_ROOT}/2much2read/huggingface" in service


@pytest.mark.parametrize(
    "unit",
    [
        "2much2read-runtime.service",
        "2busy1miss-runtime.service",
        "2busy1miss-runtime-agenda.service",
        "2bored1made-runtime.service",
    ],
)
def test_user_units_do_not_depend_on_the_system_managers_network_target(unit: str) -> None:
    """The user manager cannot order these units against a target owned by the system manager."""
    service = (UNITS / unit).read_text(encoding="utf-8")

    assert "network-online.target" not in service


@pytest.mark.parametrize(
    "unit",
    [
        "2much2read-runtime.service",
        "2busy1miss-runtime.service",
        "2busy1miss-runtime-agenda.service",
        "2bored1made-runtime.service",
    ],
)
def test_user_units_declare_no_protectproc(unit: str) -> None:
    """ProtectProc= is silently ignored by the per-user manager, so declaring it only misleads.

    Measured rather than taken from the documentation, because systemd.exec(5) is not reliable on
    this point: it carries the same "not supported for services running in per-user instances"
    paragraph for ProtectControlGroups=, which a transient user unit shows working (/sys/fs/cgroup
    goes rw -> ro). ProtectProc=invisible left /proc unchanged in the same test - the same count of
    other users' PIDs with and without it - so it is the one that has to go.

    systemd-analyze --user verify accepts it either way, which is why this assertion exists.
    """
    service = (UNITS / unit).read_text(encoding="utf-8")

    assert "ProtectProc" not in service


def test_only_the_model_loading_unit_opts_out_of_device_isolation() -> None:
    """PrivateDevices is omitted for PyTorch and GPU access, and that reason covers one unit.

    README.md states the exemption; this keeps it from quietly spreading to the three services that
    load no model. MemoryDenyWriteExecute stays off everywhere: cryptography pulls in _cffi_backend
    on the Google-authenticated paths, and cffi callbacks need writable-executable memory.
    """
    newsletter = (UNITS / "2much2read-runtime.service").read_text(encoding="utf-8")
    assert "PrivateDevices" not in newsletter

    for unit in ("2busy1miss-runtime.service", "2busy1miss-runtime-agenda.service", "2bored1made-runtime.service"):
        assert "PrivateDevices=true" in (UNITS / unit).read_text(encoding="utf-8")

    for service in UNITS.glob("*.service"):
        assert "MemoryDenyWriteExecute" not in service.read_text(encoding="utf-8")


def test_2busy1miss_agenda_timer_is_an_installer_template() -> None:
    timer = (UNITS / "2busy1miss-runtime-agenda.timer").read_text(encoding="utf-8")
    service = (UNITS / "2busy1miss-runtime-agenda.service").read_text(encoding="utf-8")

    # The timezone is part of the line, not optional. Without it systemd reads the hour in whatever
    # zone the manager runs in, while `agenda-next-day --scheduled` reads the same hour in the
    # configured one; where they differ the run lands early, returns before_schedule, and nothing
    # replaces it that day.
    assert "OnCalendar=*-*-* __AGENDA_SCHEDULE_TIME__:00 __AGENDA_SCHEDULE_TIMEZONE__" in timer
    assert "Persistent=true" in timer
    assert "ExecStart=__EXECUTABLE__ agenda-next-day --scheduled" in service


def test_both_scheduled_timers_name_a_timezone() -> None:
    for unit in ("2busy1miss-runtime-agenda.timer", "2much2read-runtime.timer"):
        timer = (UNITS / unit).read_text(encoding="utf-8")
        calendar = next(line for line in timer.splitlines() if line.startswith("OnCalendar="))
        assert calendar.endswith("TIMEZONE__"), unit


def test_2much2read_timer_is_an_installer_template() -> None:
    timer = (UNITS / "2much2read-runtime.timer").read_text(encoding="utf-8")

    assert "OnCalendar=*-*-* __DIGEST_SCHEDULE_TIME__:00 __DIGEST_SCHEDULE_TIMEZONE__" in timer


@pytest.mark.parametrize("unit", ["2busy1miss-runtime.timer", "2bored1made-runtime.timer"])
def test_per_minute_dispatchers_run_every_minute(unit: str) -> None:
    timer = (UNITS / unit).read_text(encoding="utf-8")

    assert "OnCalendar=*-*-* *:*:00" in timer
    assert "RandomizedDelaySec" not in timer
