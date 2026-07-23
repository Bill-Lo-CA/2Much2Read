from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from two_read_runtime.paths import env_file


def settings_env_file() -> Path:
    return env_file("2bored1made")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    discord_webhook_url: str = ""
    discord_username: str = "2bored1made"
    discord_allowed_mention_ids: str = ""

    @field_validator("discord_allowed_mention_ids")
    @classmethod
    def valid_allowed_mention_ids(cls, value: str) -> str:
        if not value.strip():
            return ""
        user_ids = [user_id.strip() for user_id in value.split(",")]
        if not all(user_id.isascii() and user_id.isdecimal() for user_id in user_ids):
            raise ValueError("DISCORD_ALLOWED_MENTION_IDS must be comma-separated numeric Discord user IDs")
        return ",".join(user_ids)

    @property
    def allowed_mention_ids(self) -> set[str]:
        return set(self.discord_allowed_mention_ids.split(",")) if self.discord_allowed_mention_ids else set()

    def __init__(self, **data: Any) -> None:
        super().__init__(_env_file=settings_env_file(), **data)
