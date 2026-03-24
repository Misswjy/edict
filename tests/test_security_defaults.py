import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'dashboard'))
sys.path.insert(0, str(ROOT / 'edict' / 'backend'))


class _StubBaseSettings:
    def __init__(self, **kwargs):
        for cls in reversed(type(self).__mro__):
            for name, value in cls.__dict__.items():
                if name.startswith("_") or callable(value) or isinstance(value, property):
                    continue
                setattr(self, name, value)
        for key, value in kwargs.items():
            if not key.startswith("_"):
                setattr(self, key, value)


sys.modules.setdefault(
    "pydantic_settings",
    types.SimpleNamespace(BaseSettings=_StubBaseSettings),
)

from app.config import Settings
from app.security import redact_sensitive_data
import server as srv


class _DummyHandler:
    def __init__(self, host: str, headers: dict[str, str] | None = None):
        self.client_address = (host, 7891)
        self.headers = headers or {}


def test_settings_default_to_local_only_and_redacted(monkeypatch):
    for key in (
        "BACKEND_HOST",
        "ACTIVITY_SENSITIVITY",
        "ADMIN_API_TOKEN",
        "CORS_ALLOWED_ORIGINS",
    ):
        monkeypatch.delenv(key, raising=False)

    settings = Settings(_env_file=None)

    assert settings.backend_host == "127.0.0.1"
    assert settings.activity_sensitivity == "redacted"
    assert settings.admin_api_token == ""
    assert settings.cors_origins == [
        "http://127.0.0.1:7891",
        "http://localhost:7891",
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    ]


def test_redact_sensitive_data_hides_thinking_output_and_paths():
    payload = {
        "thinking": "先看看 /Users/test/project/.env",
        "output": "token=super-secret-value",
        "text": "日志保留在 /Users/test/project/log.txt",
    }

    redacted = redact_sensitive_data(payload, "redacted")

    assert redacted["thinking"] == "[redacted]"
    assert redacted["output"] == "[redacted]"
    assert "[redacted-path]" in redacted["text"]


def test_legacy_remote_write_requires_token(monkeypatch):
    monkeypatch.delenv("EDICT_ADMIN_API_TOKEN", raising=False)
    monkeypatch.delenv("EDICT_DASHBOARD_ADMIN_TOKEN", raising=False)

    assert srv._legacy_write_allowed(_DummyHandler("127.0.0.1")) is True
    assert srv._legacy_write_allowed(_DummyHandler("203.0.113.10")) is False

    monkeypatch.setenv("EDICT_ADMIN_API_TOKEN", "top-secret")
    assert srv._legacy_write_allowed(_DummyHandler("127.0.0.1")) is False
    assert srv._legacy_write_allowed(
        _DummyHandler("203.0.113.10", {"Authorization": "Bearer top-secret"})
    ) is True


def test_parse_activity_entry_redacts_thinking_by_default(monkeypatch):
    monkeypatch.setenv("EDICT_ACTIVITY_SENSITIVITY", "redacted")

    item = {
        "timestamp": "2026-03-24T00:00:00+00:00",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "正在处理 /Users/test/project"},
                {"type": "thinking", "thinking": "先打开 /Users/test/project/.env"},
            ],
        },
    }

    entry = srv._parse_activity_entry(item)

    assert entry is not None
    assert "thinking" not in entry or entry["thinking"] == "[redacted]"
    assert "[redacted-path]" in entry["text"]
