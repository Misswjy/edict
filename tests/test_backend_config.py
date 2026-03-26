"""Regression tests for backend settings env overrides."""

from __future__ import annotations

from edict.backend.app.config import Settings


def test_settings_accepts_database_url_env_alias(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://edict:secret@postgres:5432/edict")

    settings = Settings()

    assert settings.database_url_override == "postgresql+asyncpg://edict:secret@postgres:5432/edict"
    assert settings.database_url == "postgresql+asyncpg://edict:secret@postgres:5432/edict"


def test_settings_keeps_database_url_override_backwards_compatible(monkeypatch):
    monkeypatch.setenv("DATABASE_URL_OVERRIDE", "postgresql+asyncpg://edict:secret@127.0.0.1:5432/edict")

    settings = Settings()

    assert settings.database_url_override == "postgresql+asyncpg://edict:secret@127.0.0.1:5432/edict"
    assert settings.database_url == "postgresql+asyncpg://edict:secret@127.0.0.1:5432/edict"
