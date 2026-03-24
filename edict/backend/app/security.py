"""Security helpers for auth, origin parsing, and activity redaction."""

from __future__ import annotations

import ipaddress
import re
from typing import Any

_ABS_PATH_RE = re.compile(r"(?:(?:/Users|/home|/root|/app|/tmp|[A-Za-z]:[\\/])[\w .:/\\-]+)")
_SECRET_ASSIGN_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password|authorization)\b\s*[:=]\s*([^\s,;]+)"
)
_SENSITIVE_KEYS = {
    "thinking",
    "prompt",
    "output",
    "stdout",
    "stderr",
    "details",
    "input",
    "arguments",
    "path",
    "api_key",
    "apikey",
    "token",
    "secret",
    "authorization",
}
_TEXTUAL_KEYS = {"text", "content", "message", "remark", "note"}


def is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    candidate = host.strip().lower()
    if candidate == "localhost":
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def split_allowed_origins(raw: str | list[str] | tuple[str, ...] | None) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(item).strip() for item in raw if str(item).strip()]
    return [part.strip() for part in str(raw).split(",") if part.strip()]


def extract_admin_token(authorization: str | None = None, x_admin_token: str | None = None) -> str:
    if x_admin_token and x_admin_token.strip():
        return x_admin_token.strip()
    if not authorization:
        return ""
    prefix = "bearer "
    value = authorization.strip()
    if value.lower().startswith(prefix):
        return value[len(prefix):].strip()
    return value


def normalize_activity_sensitivity(mode: str | None) -> str:
    return "full" if str(mode or "").strip().lower() == "full" else "redacted"


def redact_sensitive_text(text: str) -> str:
    if not text:
        return text
    value = _ABS_PATH_RE.sub("[redacted-path]", text)
    value = _SECRET_ASSIGN_RE.sub(lambda match: f"{match.group(1)}=[redacted]", value)
    return value


def redact_sensitive_data(data: Any, mode: str = "redacted") -> Any:
    if normalize_activity_sensitivity(mode) == "full":
        return data

    if isinstance(data, dict):
        sanitized: dict[str, Any] = {}
        for key, value in data.items():
            key_str = str(key)
            lowered = key_str.lower()
            if lowered in _SENSITIVE_KEYS:
                sanitized[key_str] = "[redacted]"
            elif lowered in _TEXTUAL_KEYS and isinstance(value, str):
                sanitized[key_str] = redact_sensitive_text(value)
            else:
                sanitized[key_str] = redact_sensitive_data(value, mode)
        return sanitized

    if isinstance(data, list):
        return [redact_sensitive_data(item, mode) for item in data]

    if isinstance(data, str):
        return redact_sensitive_text(data)

    return data
