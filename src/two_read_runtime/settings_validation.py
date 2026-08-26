from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import dotenv_values


def valid_timezone(value: str) -> str:
    """An IANA timezone name, rejected here rather than wherever it is first used.

    ZoneInfoNotFoundError subclasses KeyError, not ValueError, so it slips past every
    `except ValueError` handler in the CLIs and surfaces as a traceback - and in the newsletter
    pipeline it did so only after the process lock had been taken and the database opened.
    """
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"unknown IANA timezone {value!r}") from error
    return value


def unknown_env_keys(path: Path, fields: Iterable[str], *, also_allowed: Iterable[str] = ()) -> list[str]:
    """Keys in an environment file that no setting will ever read.

    Settings use extra="ignore", but that is not what makes a misspelling silent: pydantic-settings
    looks up each field name it knows in the environment, so a key it does not know is never
    offered to `extra` in the first place - extra="forbid" would change nothing here. The only
    place a typo is visible is the file itself, compared against the field names.

    `also_allowed` carries the keys the installers read with sed and the settings do not have,
    which would otherwise be reported as misspellings on every healthy installation.
    """
    if not path.is_file():
        return []
    try:
        values: Mapping[str, str | None] = dotenv_values(path, encoding="utf-8")
    except OSError:
        return []
    known = {name.casefold() for name in fields} | {name.casefold() for name in also_allowed}
    return sorted(key for key in values if key.casefold() not in known)
