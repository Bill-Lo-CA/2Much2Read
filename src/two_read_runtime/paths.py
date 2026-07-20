from __future__ import annotations

import os
from pathlib import Path


def config_dir() -> Path:
    return Path.home() / ".config" / "2much2read"


def data_dir() -> Path:
    return Path.home() / ".local" / "share" / "2much2read"


def env_file(application: str) -> Path:
    return config_dir() / f".{application}.env"


def directory_is_creatable(path: Path) -> bool:
    parent = path
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    return parent.is_dir() and os.access(parent, os.W_OK | os.X_OK)
