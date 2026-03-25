"""Edict 配置管理 — 从环境变量加载所有配置。"""

import secrets
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings

from .security import split_allowed_origins


class Settings(BaseSettings):
    # ── Postgres ──
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "edict"
    postgres_user: str = "edict"
    postgres_password: str = "edict_secret_change_me"
    database_url_override: str | None = None  # 直接设置 DATABASE_URL 环境变量时用

    # ── Redis ──
    redis_url: str = "redis://localhost:6379/0"

    # ── Server ──
    backend_host: str = "127.0.0.1"
    backend_port: int = 8000
    port: int = 8000
    secret_key: str = secrets.token_urlsafe(32)
    debug: bool = False
    admin_api_token: str = ""
    cors_allowed_origins: str = "http://127.0.0.1:7891,http://localhost:7891,http://127.0.0.1:5173,http://localhost:5173"
    activity_sensitivity: Literal["redacted", "full"] = "redacted"

    # ── OpenClaw ──
    openclaw_gateway_url: str = "http://localhost:18789"
    openclaw_bin: str = "openclaw"
    openclaw_project_dir: str | None = None

    # ── Legacy 兼容 ──
    legacy_data_dir: str = "../data"
    legacy_tasks_file: str = "../data/tasks_source.json"
    enable_legacy_task_routes: bool = False

    # ── 调度参数 ──
    stall_threshold_sec: int = 180
    max_dispatch_retry: int = 3
    dispatch_timeout_sec: int = 300
    heartbeat_interval_sec: int = 30
    scheduler_scan_interval_seconds: int = 60

    # ── 飞书 ──
    feishu_deliver: bool = True
    feishu_channel: str = "feishu"

    @property
    def database_url(self) -> str:
        if self.database_url_override:
            return self.database_url_override
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def database_url_sync(self) -> str:
        """同步 URL，供 Alembic 使用。"""
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def cors_origins(self) -> list[str]:
        return split_allowed_origins(self.cors_allowed_origins)

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "env_prefix": "",
        "alias_generator": None,
        "populate_by_name": True,
        "extra": "ignore",
    }


@lru_cache
def get_settings() -> Settings:
    return Settings()
