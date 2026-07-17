from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from common.paths import config_dir, data_dir, env_file


@dataclass(frozen=True)
class MigrationOperation:
    source: Path
    destination: Path
    content: str | None = None
    original: bytes | None = None
    sqlite: bool = False
    backup: Path | None = None

    def source_files(self) -> list[Path]:
        if not self.sqlite:
            return [self.source]
        return [
            path
            for path in (
                self.source,
                self.source.with_name(self.source.name + "-wal"),
                self.source.with_name(self.source.name + "-shm"),
            )
            if path.exists()
        ]


def _first_existing(paths: Iterable[Path]) -> Path | None:
    return next((path for path in paths if path.is_file()), None)


def _configured_path(values: dict[str, str | None], key: str, base: Path, fallback: Path) -> Path:
    value = values.get(key)
    if not value:
        return fallback
    path = Path(value).expanduser()
    return path if path.is_absolute() else base / path


def _source_path(values: dict[str, str | None], key: str, base: Path, fallback: Path) -> Path:
    configured = _configured_path(values, key, base, fallback)
    return configured if configured.exists() or not fallback.exists() else fallback


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, mode=0o700)
    os.chmod(path, 0o700)


def _copy_file(source: Path, destination: Path, content: str | None = None) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        if content is None:
            with os.fdopen(descriptor, "wb") as handle, source.open("rb") as original:
                shutil.copyfileobj(original, handle)
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _copy_sqlite(source: Path, destination: Path) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    source_connection: sqlite3.Connection | None = None
    destination_connection: sqlite3.Connection | None = None
    try:
        source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        destination_connection = sqlite3.connect(temporary)
        source_connection.backup(destination_connection)
        destination_connection.close()
        destination_connection = None
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        if destination_connection is not None:
            destination_connection.close()
        if source_connection is not None:
            source_connection.close()
        temporary.unlink(missing_ok=True)


def _environment_content(source: Path, overrides: dict[str, Path]) -> str:
    content = source.read_text(encoding="utf-8").rstrip()
    return content + "\n" + "\n".join(f"{key}={value}" for key, value in overrides.items()) + "\n"


def _add_file(operations: list[MigrationOperation], source: Path, destination: Path) -> None:
    if source == destination or not source.exists():
        return
    if destination.exists():
        raise ValueError(f"migration target already exists: {destination}")
    operations.append(MigrationOperation(source, destination, original=source.read_bytes()))


def _add_sqlite(operations: list[MigrationOperation], source: Path, destination: Path) -> None:
    if source == destination or not source.exists():
        return
    backup = destination.with_name(destination.name + ".pre-migration-backup")
    targets = [
        destination,
        destination.with_name(destination.name + "-wal"),
        destination.with_name(destination.name + "-shm"),
        backup,
    ]
    if existing := next((path for path in targets if path.exists()), None):
        raise ValueError(f"migration target already exists: {existing}")
    operations.append(MigrationOperation(source, destination, sqlite=True, backup=backup))


def _add_environment(
    operations: list[MigrationOperation], source: Path | None, destination: Path, overrides: dict[str, Path]
) -> None:
    if source is None or source == destination:
        return
    if destination.exists():
        raise ValueError(f"migration target already exists: {destination}")
    operations.append(
        MigrationOperation(
            source,
            destination,
            content=_environment_content(source, overrides),
            original=source.read_bytes(),
        )
    )


def _remove_source(source: Path) -> None:
    source.unlink()


def _restore_source(operation: MigrationOperation, source: Path) -> None:
    if operation.sqlite:
        _copy_sqlite(operation.destination, source)
        return
    assert operation.original is not None
    source.write_bytes(operation.original)
    os.chmod(source, 0o600)


def _execute(operations: list[MigrationOperation], directories: Iterable[Path]) -> list[Path]:
    if not operations:
        return []
    for directory in directories:
        _private_directory(directory)
    created: list[Path] = []
    removed: list[tuple[MigrationOperation, Path]] = []
    try:
        for operation in operations:
            if operation.sqlite:
                assert operation.backup is not None
                _copy_sqlite(operation.source, operation.backup)
                created.append(operation.backup)
                _copy_sqlite(operation.source, operation.destination)
            else:
                _copy_file(operation.source, operation.destination, operation.content)
            created.append(operation.destination)
        for operation in operations:
            for source in operation.source_files():
                _remove_source(source)
                removed.append((operation, source))
    except Exception:
        for operation, source in reversed(removed):
            if not source.exists():
                _restore_source(operation, source)
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise
    return [operation.destination for operation in operations]


def migrate_newsletter(legacy_envs: list[Path], gmail_client_secret: Path | None = None) -> list[Path]:
    legacy_config = Path.home() / ".config" / "newsletter-digest"
    legacy_data = Path.home() / ".local" / "share" / "newsletter-digest"
    source_env = _first_existing(legacy_envs)
    values = dotenv_values(source_env) if source_env else {}
    base = source_env.parent if source_env else legacy_config
    target_config = config_dir()
    target_data = data_dir()
    operations: list[MigrationOperation] = []

    if gmail_client_secret is not None:
        destination = target_config / "gmail-client-secret.json"
        if not gmail_client_secret.is_file() and not destination.exists():
            raise ValueError(f"Gmail client secret not found: {gmail_client_secret}")
        _add_file(operations, gmail_client_secret, destination)
    else:
        _add_file(
            operations,
            _source_path(values, "GMAIL_CREDENTIALS_PATH", base, legacy_config / "google-client-secret.json"),
            target_config / "gmail-client-secret.json",
        )
    _add_file(
        operations,
        _source_path(values, "GMAIL_TOKEN_PATH", base, legacy_config / "google-token.json"),
        target_config / "gmail-token.json",
    )
    sources = _source_path(values, "SOURCES_CONFIG_PATH", base, legacy_config / "sources.yaml")
    _add_file(operations, sources, target_config / "sources.yaml")
    _add_file(operations, sources.with_name("excluded-subscriptions.yaml"), target_config / "excluded-subscriptions.yaml")
    _add_sqlite(
        operations,
        _source_path(values, "DATABASE_PATH", base, legacy_data / "newsletter-digest.sqlite3"),
        target_data / "2much2read.sqlite3",
    )
    _add_environment(
        operations,
        source_env,
        env_file("2much2read"),
        {
            "GMAIL_CREDENTIALS_PATH": target_config / "gmail-client-secret.json",
            "GMAIL_TOKEN_PATH": target_config / "gmail-token.json",
            "SOURCES_CONFIG_PATH": target_config / "sources.yaml",
            "DATABASE_PATH": target_data / "2much2read.sqlite3",
            "LOCK_PATH": target_data / "2much2read.lock",
        },
    )
    return _execute(operations, (target_config, target_data))


def migrate_calendar(legacy_envs: list[Path], calendar_client_secret: Path | None = None) -> list[Path]:
    legacy_config = Path.home() / ".config" / "2busy1miss"
    legacy_data = Path.home() / ".local" / "share" / "2busy1miss"
    source_env = _first_existing(legacy_envs)
    values = dotenv_values(source_env) if source_env else {}
    base = source_env.parent if source_env else legacy_config
    target_config = config_dir()
    target_data = data_dir()
    operations: list[MigrationOperation] = []

    if calendar_client_secret is not None:
        destination = target_config / "calendar-client-secret.json"
        if not calendar_client_secret.is_file() and not destination.exists():
            raise ValueError(f"Calendar client secret not found: {calendar_client_secret}")
        _add_file(operations, calendar_client_secret, destination)
    else:
        _add_file(
            operations,
            _source_path(
                values,
                "GOOGLE_CALENDAR_CREDENTIALS_PATH",
                base,
                legacy_config / "google-client-secret.json",
            ),
            target_config / "calendar-client-secret.json",
        )
    _add_file(
        operations,
        _source_path(values, "GOOGLE_CALENDAR_TOKEN_PATH", base, legacy_config / "google-calendar-token.json"),
        target_config / "calendar-token.json",
    )
    _add_file(
        operations,
        _source_path(values, "REMINDERS_CONFIG_PATH", base, legacy_config / "reminders.yaml"),
        target_config / "reminders.yaml",
    )
    _add_sqlite(
        operations,
        _source_path(values, "DATABASE_PATH", base, legacy_data / "2busy1miss.sqlite3"),
        target_data / "2busy1miss.sqlite3",
    )
    _add_environment(
        operations,
        source_env,
        env_file("2busy1miss"),
        {
            "GOOGLE_CALENDAR_CREDENTIALS_PATH": target_config / "calendar-client-secret.json",
            "GOOGLE_CALENDAR_TOKEN_PATH": target_config / "calendar-token.json",
            "REMINDERS_CONFIG_PATH": target_config / "reminders.yaml",
            "DATABASE_PATH": target_data / "2busy1miss.sqlite3",
            "LOCK_PATH": target_data / "2busy1miss.lock",
        },
    )
    return _execute(operations, (target_config, target_data))


def main() -> None:
    parser = argparse.ArgumentParser(description="Move 2Much2Read runtime files into the shared config root")
    parser.add_argument("application", choices=("newsletter", "calendar"))
    parser.add_argument("--legacy-env", action="append", type=Path, default=[])
    parser.add_argument("--gmail-client-secret", type=Path)
    parser.add_argument("--calendar-client-secret", type=Path)
    args = parser.parse_args()
    if args.application == "newsletter":
        moved = migrate_newsletter(args.legacy_env, args.gmail_client_secret)
    else:
        moved = migrate_calendar(args.legacy_env, args.calendar_client_secret)
    for path in moved:
        print(f"moved {path.name}")


if __name__ == "__main__":
    main()
