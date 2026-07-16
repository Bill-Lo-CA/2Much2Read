from __future__ import annotations

import argparse
import os
import shutil
from collections.abc import Iterable
from pathlib import Path

from dotenv import dotenv_values

from common.paths import config_dir, data_dir, env_file


def _first_existing(paths: Iterable[Path]) -> Path | None:
    return next((path for path in paths if path.is_file()), None)


def _configured_path(values: dict[str, str | None], key: str, base: Path, fallback: Path) -> Path:
    value = values.get(key)
    if not value:
        return fallback
    path = Path(value).expanduser()
    return path if path.is_absolute() else base / path


def _move(source: Path, destination: Path) -> bool:
    if source == destination or not source.exists():
        return False
    if destination.exists():
        raise ValueError(f"migration target already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))
    os.chmod(destination, 0o600)
    return True


def _move_sqlite(source: Path, destination: Path) -> bool:
    if source == destination or not source.exists():
        return False
    destinations = [destination, *(destination.with_name(destination.name + suffix) for suffix in ("-wal", "-shm"))]
    if any(path.exists() for path in destinations):
        raise ValueError(f"migration target already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        source_path = source.with_name(source.name + suffix)
        if source_path.exists():
            destination_path = destination.with_name(destination.name + suffix)
            shutil.move(str(source_path), str(destination_path))
            os.chmod(destination_path, 0o600)
    return True


def _migrate_path(
    values: dict[str, str | None],
    key: str,
    base: Path,
    fallback: Path,
    destination: Path,
) -> bool:
    source = _configured_path(values, key, base, fallback)
    if not source.exists() and fallback.exists():
        source = fallback
    return _move(source, destination)


def _migrate_sqlite(
    values: dict[str, str | None],
    base: Path,
    fallback: Path,
    destination: Path,
) -> bool:
    source = _configured_path(values, "DATABASE_PATH", base, fallback)
    if not source.exists() and fallback.exists():
        source = fallback
    return _move_sqlite(source, destination)


def _write_env(destination: Path, source: Path | None, overrides: dict[str, Path]) -> None:
    if source is None or destination.exists():
        return
    content = source.read_text(encoding="utf-8").rstrip()
    content += "\n" + "\n".join(f"{key}={value}" for key, value in overrides.items()) + "\n"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
    os.chmod(destination, 0o600)
    source.unlink()


def migrate_newsletter(legacy_envs: list[Path], gmail_client_secret: Path | None = None) -> list[Path]:
    legacy_config = Path.home() / ".config" / "newsletter-digest"
    legacy_data = Path.home() / ".local" / "share" / "newsletter-digest"
    source_env = _first_existing(legacy_envs)
    values = dotenv_values(source_env) if source_env else {}
    base = source_env.parent if source_env else legacy_config
    target_config = config_dir()
    target_data = data_dir()
    moved: list[Path] = []
    sources = _configured_path(values, "SOURCES_CONFIG_PATH", base, legacy_config / "sources.yaml")
    if not sources.exists() and (legacy_config / "sources.yaml").exists():
        sources = legacy_config / "sources.yaml"

    if gmail_client_secret is not None:
        if not gmail_client_secret.is_file():
            raise ValueError(f"Gmail client secret not found: {gmail_client_secret}")
        if _move(gmail_client_secret, target_config / "gmail-client-secret.json"):
            moved.append(target_config / "gmail-client-secret.json")
    elif _migrate_path(
        values,
        "GMAIL_CREDENTIALS_PATH",
        base,
        legacy_config / "google-client-secret.json",
        target_config / "gmail-client-secret.json",
    ):
        moved.append(target_config / "gmail-client-secret.json")
    for key, fallback, destination in (
        ("GMAIL_TOKEN_PATH", legacy_config / "google-token.json", target_config / "gmail-token.json"),
        ("SOURCES_CONFIG_PATH", legacy_config / "sources.yaml", target_config / "sources.yaml"),
        ("LOCK_PATH", legacy_data / "newsletter-digest.lock", target_data / "2much2read.lock"),
    ):
        if _migrate_path(values, key, base, fallback, destination):
            moved.append(destination)
    if _migrate_sqlite(
        values,
        base,
        legacy_data / "newsletter-digest.sqlite3",
        target_data / "2much2read.sqlite3",
    ):
        moved.append(target_data / "2much2read.sqlite3")

    excluded_destination = target_config / "excluded-subscriptions.yaml"
    if _move(sources.with_name("excluded-subscriptions.yaml"), excluded_destination):
        moved.append(excluded_destination)

    _write_env(
        env_file("2much2read"),
        source_env,
        {
            "GMAIL_CREDENTIALS_PATH": target_config / "gmail-client-secret.json",
            "GMAIL_TOKEN_PATH": target_config / "gmail-token.json",
            "SOURCES_CONFIG_PATH": target_config / "sources.yaml",
            "DATABASE_PATH": target_data / "2much2read.sqlite3",
            "LOCK_PATH": target_data / "2much2read.lock",
        },
    )
    return moved


def migrate_calendar(legacy_envs: list[Path], calendar_client_secret: Path | None = None) -> list[Path]:
    legacy_config = Path.home() / ".config" / "2busy1miss"
    legacy_data = Path.home() / ".local" / "share" / "2busy1miss"
    source_env = _first_existing(legacy_envs)
    values = dotenv_values(source_env) if source_env else {}
    base = source_env.parent if source_env else legacy_config
    target_config = config_dir()
    target_data = data_dir()
    moved: list[Path] = []

    if calendar_client_secret is not None:
        if not calendar_client_secret.is_file():
            raise ValueError(f"Calendar client secret not found: {calendar_client_secret}")
        if _move(calendar_client_secret, target_config / "calendar-client-secret.json"):
            moved.append(target_config / "calendar-client-secret.json")
    elif _migrate_path(
        values,
        "GOOGLE_CALENDAR_CREDENTIALS_PATH",
        base,
        legacy_config / "google-client-secret.json",
        target_config / "calendar-client-secret.json",
    ):
        moved.append(target_config / "calendar-client-secret.json")
    for key, fallback, destination in (
        ("GOOGLE_CALENDAR_TOKEN_PATH", legacy_config / "google-calendar-token.json", target_config / "calendar-token.json"),
        ("REMINDERS_CONFIG_PATH", legacy_config / "reminders.yaml", target_config / "reminders.yaml"),
        ("LOCK_PATH", legacy_data / "2busy1miss.lock", target_data / "2busy1miss.lock"),
    ):
        if _migrate_path(values, key, base, fallback, destination):
            moved.append(destination)
    if _migrate_sqlite(
        values,
        base,
        legacy_data / "2busy1miss.sqlite3",
        target_data / "2busy1miss.sqlite3",
    ):
        moved.append(target_data / "2busy1miss.sqlite3")

    _write_env(
        env_file("2busy1miss"),
        source_env,
        {
            "GOOGLE_CALENDAR_CREDENTIALS_PATH": target_config / "calendar-client-secret.json",
            "GOOGLE_CALENDAR_TOKEN_PATH": target_config / "calendar-token.json",
            "REMINDERS_CONFIG_PATH": target_config / "reminders.yaml",
            "DATABASE_PATH": target_data / "2busy1miss.sqlite3",
            "LOCK_PATH": target_data / "2busy1miss.lock",
        },
    )
    return moved


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
