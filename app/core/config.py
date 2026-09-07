from functools import lru_cache
from pathlib import Path
from typing import Self
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "resource-co-debug-platform-backend"
    app_env: str = "local"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    storage_root: Path = Path("data/workspaces")
    task_database_path: Path | None = None
    default_task_timeout_seconds: int = 300
    max_log_lines_per_task: int = 2000
    naturalcc_base_url: str = "http://127.0.0.1:7860"
    naturalcc_connect_timeout_seconds: float = Field(default=5.0, gt=0)
    naturalcc_request_timeout_seconds: float = Field(default=30.0, gt=0)
    naturalcc_approve_execute: bool = False
    allowed_cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://localhost:5173"]
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    @field_validator("allowed_cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("naturalcc_base_url")
    @classmethod
    def normalize_naturalcc_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        parsed = urlsplit(normalized)
        if not normalized or parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("naturalcc_base_url must be a non-empty HTTP(S) URL")
        return normalized

    @model_validator(mode="after")
    def set_default_task_database_path(self) -> Self:
        if self.task_database_path is None:
            self.task_database_path = self.storage_root.parent / "tasks.sqlite3"
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()

