import os
import stat
from pathlib import Path

import pytest

from two_read_runtime import permissions
from two_read_runtime.permissions import (
    path_within,
    prepare_private_directory,
    prepare_private_file,
    private_directory_status,
    private_file_status,
    repair_private_file,
    sqlite_files_status,
)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_prepare_private_file_ignores_permissive_umask_and_keeps_existing_parent(tmp_path: Path) -> None:
    custom_parent = tmp_path / "custom"
    custom_parent.mkdir(mode=0o755)
    os.chmod(custom_parent, 0o755)
    path = custom_parent / "leaf" / "token.json"
    original_umask = os.umask(0)
    try:
        prepare_private_file(path)
    finally:
        os.umask(original_umask)

    assert mode(path.parent) == 0o700
    assert mode(path) == 0o600
    assert mode(custom_parent) == 0o755


def test_prepare_private_directory_rejects_symlink_parent(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="RUNTIME_PERMISSION_UNSAFE"):
        prepare_private_directory(link)


def test_repair_private_file_repairs_owned_regular_files(tmp_path: Path) -> None:
    path = tmp_path / "token.json"
    path.write_text("token", encoding="utf-8")
    os.chmod(path, 0o644)

    assert private_file_status(path) == "unsafe"
    repair_private_file(path)

    assert mode(path) == 0o600
    assert private_file_status(path) == "ok"


def test_private_statuses_and_sqlite_sidecars(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite3"
    assert private_file_status(database) == "missing"
    assert private_file_status(database, missing_ok=True) == "not_created"
    assert sqlite_files_status(database) == "not_created"
    prepare_private_file(database)
    assert sqlite_files_status(database) == "ok"
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(f"{database}{suffix}")
        sidecar.write_bytes(b"")
        os.chmod(sidecar, 0o644)

    assert sqlite_files_status(database) == "unsafe"
    for suffix in ("-wal", "-shm", "-journal"):
        repair_private_file(Path(f"{database}{suffix}"))
    assert sqlite_files_status(database) == "ok"


def test_symlinks_are_unsafe_and_never_repaired(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("target", encoding="utf-8")
    os.chmod(target, 0o644)
    link = tmp_path / "link"
    link.symlink_to(target)

    assert private_file_status(link) == "unsafe"
    with pytest.raises(ValueError, match="RUNTIME_PERMISSION_UNSAFE"):
        repair_private_file(link)
    assert mode(target) == 0o644


def test_path_within_resolves_missing_paths_and_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    assert path_within(root / "missing" / "file", root)
    assert not path_within(tmp_path / "outside", root)
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "outside")
    assert not path_within(link / "file", root)


def test_private_directory_status(tmp_path: Path) -> None:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    assert private_directory_status(directory) == "ok"
    assert private_directory_status(tmp_path / "missing", missing_ok=True) == "not_created"


def test_prepare_private_directory_rejects_a_directory_another_user_won_the_race_to_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The walk up and the mkdir back down have to enforce the same three properties.

    The walk checked that each existing directory is a real directory, is not a symlink, and is
    owned by us. The FileExistsError branch on the way down checked only the first two, so a
    directory that appeared in the window between them was accepted whoever owned it. Simulated by
    flipping the answer from getuid at exactly that moment, which is the whole of the race.
    """
    target = tmp_path / "appeared" / "leaf"
    raced = False
    real_getuid = os.getuid
    real_mkdir = os.mkdir

    def racing_mkdir(path: object, mode: int = 0o777, **kwargs: object) -> None:
        nonlocal raced
        real_mkdir(path, mode)  # type: ignore[arg-type]
        if Path(str(path)) == target:
            raced = True
            raise FileExistsError(path)

    monkeypatch.setattr(os, "mkdir", racing_mkdir)
    monkeypatch.setattr(os, "getuid", lambda: real_getuid() + 1 if raced else real_getuid())

    with pytest.raises(ValueError, match="RUNTIME_PERMISSION_UNSAFE"):
        prepare_private_directory(target)


def test_runtime_permission_checks_reports_the_shared_data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both roots are shared by all three tools, and only the installers ever chmod them.

    Nothing reported the data root, so a setup that created it with the default mode - which the
    documented `install -d -m 700 a/b` does, applying the mode to the leaf only - looked healthy.
    """
    data_root = tmp_path / "data"
    application_data = data_root / "2much2read"
    application_data.mkdir(parents=True)
    data_root.chmod(0o755)
    application_data.chmod(0o700)
    monkeypatch.setattr(permissions, "data_dir", lambda: data_root)
    monkeypatch.setattr(permissions, "app_data_dir", lambda _: application_data)

    checks = permissions.runtime_permission_checks(
        "2much2read",
        config_path=tmp_path / "sources.yaml",
        credentials_path=tmp_path / "secret.json",
        token_path=tmp_path / "token.json",
        database_path=application_data / "db.sqlite3",
        lock_path=application_data / "db.lock",
    )

    assert checks["data_root"] == "unsafe"
    assert checks["data_dir"] == "ok"
