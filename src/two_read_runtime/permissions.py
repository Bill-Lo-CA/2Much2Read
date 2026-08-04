from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from typing import Literal

from .paths import app_config_dir, app_data_dir, config_dir, env_file

PermissionStatus = Literal["ok", "missing", "not_created", "unsafe"]


def _missing_status(missing_ok: bool) -> PermissionStatus:
    return "not_created" if missing_ok else "missing"


def _status(path: Path, *, directory: bool, missing_ok: bool) -> PermissionStatus:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return _missing_status(missing_ok)
    except OSError:
        return "unsafe"
    if stat.S_ISLNK(metadata.st_mode):
        return "unsafe"
    if (stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)) is False:
        return "unsafe"
    if metadata.st_uid != os.getuid():
        return "unsafe"
    expected_mode = 0o700 if directory else 0o600
    return "ok" if stat.S_IMODE(metadata.st_mode) == expected_mode else "unsafe"


def private_directory_status(path: Path, missing_ok: bool = False) -> PermissionStatus:
    return _status(path, directory=True, missing_ok=missing_ok)


def private_file_status(path: Path, missing_ok: bool = False) -> PermissionStatus:
    return _status(path, directory=False, missing_ok=missing_ok)


def _sqlite_paths(database_path: Path) -> list[Path]:
    return [database_path, *(Path(f"{database_path}{suffix}") for suffix in ("-wal", "-shm", "-journal"))]


def sqlite_files_status(database_path: Path, missing_ok: bool = True) -> PermissionStatus:
    main_status = private_file_status(database_path, missing_ok=missing_ok)
    sidecar_statuses = [private_file_status(path, missing_ok=True) for path in _sqlite_paths(database_path)[1:]]
    if main_status == "unsafe" or "unsafe" in sidecar_statuses:
        return "unsafe"
    return main_status


def path_within(path: Path, root: Path) -> bool:
    try:
        candidate = path.resolve(strict=False)
        base = root.resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    return candidate == base or base in candidate.parents


def runtime_permission_checks(
    application: str,
    *,
    config_path: Path,
    credentials_path: Path,
    token_path: Path,
    database_path: Path,
    lock_path: Path,
) -> dict[str, str]:
    token_dir = app_config_dir(application)
    application_data_dir = app_data_dir(application)
    return {
        "config_dir": private_directory_status(config_dir(), missing_ok=True),
        "token_dir": private_directory_status(token_dir, missing_ok=True),
        "data_dir": private_directory_status(application_data_dir, missing_ok=True),
        "env_file": private_file_status(env_file(application)),
        "config_file": private_file_status(config_path),
        "client_secret": private_file_status(credentials_path),
        "token_file": private_file_status(token_path),
        "database": sqlite_files_status(database_path),
        "lock_file": private_file_status(lock_path, missing_ok=True),
        "runtime_paths": (
            "ok"
            if path_within(token_path, token_dir)
            and path_within(database_path, application_data_dir)
            and path_within(lock_path, application_data_dir)
            else "custom"
        ),
    }


def _unsafe() -> ValueError:
    return ValueError("RUNTIME_PERMISSION_UNSAFE")


def repair_private_file(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except (OSError, ValueError) as error:
        raise _unsafe() from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise _unsafe()
        os.fchmod(descriptor, 0o600)
    except (OSError, ValueError) as error:
        if isinstance(error, ValueError) and str(error) == "RUNTIME_PERMISSION_UNSAFE":
            raise
        raise _unsafe() from error
    finally:
        os.close(descriptor)


def prepare_private_directory(path: Path) -> None:
    missing: list[Path] = []
    parent = path
    while True:
        try:
            metadata = parent.lstat()
        except FileNotFoundError:
            missing.append(parent)
            if parent == parent.parent:
                break
            parent = parent.parent
            continue
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise _unsafe()
        break
    for directory in reversed(missing):
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            metadata = directory.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise _unsafe() from None


def prepare_private_file(path: Path) -> None:
    prepare_private_directory(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        repair_private_file(path)
        return
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise _unsafe() from error
        raise
    try:
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


def repair_sqlite_files(database_path: Path) -> None:
    for path in _sqlite_paths(database_path):
        if os.path.lexists(path):
            repair_private_file(path)
