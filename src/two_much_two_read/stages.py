"""What every step of a digest run shares: how it reports status, and how it releases a model."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

StatusReporter = Callable[[str], None]


def _ignore_status(_: str) -> None:
    pass


class UnloadsModels(Protocol):
    def unload(self, model: str) -> bool: ...


def _unload_model(ollama: UnloadsModels, model: str, status: StatusReporter = _ignore_status) -> None:
    if not ollama.unload(model):
        status(f"Warning: {model} did not unload and may still hold memory")
