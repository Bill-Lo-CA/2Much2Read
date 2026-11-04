from __future__ import annotations

from datetime import time
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from two_read_runtime.discord import DiscordDestination, configured_destinations
from two_read_runtime.paths import app_data_file, env_file
from two_read_runtime.settings_validation import valid_timezone as _timezone


def settings_env_file() -> Path:
    return env_file("2fun4distract")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    database_path: Path = Field(default_factory=lambda: app_data_file("2fun4distract", "2fun4distract.sqlite3"))
    lock_path: Path = Field(default_factory=lambda: app_data_file("2fun4distract", "2fun4distract.lock"))
    discord_delivery_mode: Literal["webhook", "bot", "both"] = "webhook"
    discord_webhook_url: str = ""
    discord_username: str = "2fun4distract"
    discord_bot_token: str = ""
    discord_bot_channel_id: str = ""
    summary_timezone: str = "America/Toronto"
    summary_schedule_time: time = time(8)

    @field_validator("summary_timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        return _timezone(value)

    @field_validator("summary_schedule_time")
    @classmethod
    def valid_schedule_time(cls, value: time) -> time:
        if value.second or value.microsecond:
            raise ValueError("summary schedule time must use HH:MM")
        return value

    def __init__(self, **data: Any) -> None:
        super().__init__(_env_file=settings_env_file(), **data)

    def discord_destinations(self) -> list[DiscordDestination]:
        return configured_destinations(
            self.discord_delivery_mode, self.discord_webhook_url, self.discord_bot_token, self.discord_bot_channel_id
        )
