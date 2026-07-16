from __future__ import annotations

from pathlib import Path


def config_dir() -> Path:
    return Path.home() / ".config" / "2much2read"


def data_dir() -> Path:
    return Path.home() / ".local" / "share" / "2much2read"


def env_file(application: str) -> Path:
    return config_dir() / f".{application}.env"
