"""Atomic local JSON/file helpers used by compatibility services."""

from __future__ import annotations

import fcntl
import ipaddress
import json
import os
import pathlib
import tempfile
from typing import Any, Callable
from urllib.parse import urlparse


def _lock_path(path: pathlib.Path) -> pathlib.Path:
    return path.parent / f"{path.name}.lock"


def atomic_json_read(path: pathlib.Path, default: Any = None) -> Any:
    lock_file = _lock_path(path)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        try:
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
        except Exception:
            return default
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def atomic_json_write(path: pathlib.Path, data: Any) -> None:
    lock_file = _lock_path(path)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=f"{path.stem}_")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
            os.replace(tmp_path, str(path))
        except Exception:
            os.unlink(tmp_path)
            raise
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def atomic_json_update(path: pathlib.Path, modifier: Callable[[Any], Any], default: Any = None) -> Any:
    lock_file = _lock_path(path)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
        except Exception:
            current = default
        updated = modifier(current)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=f"{path.stem}_")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
                json.dump(updated, handle, ensure_ascii=False, indent=2)
            os.replace(tmp_path, str(path))
        except Exception:
            os.unlink(tmp_path)
            raise
        return updated
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def validate_url(url: str, allowed_schemes: tuple[str, ...] = ("https",), allowed_domains: tuple[str, ...] | None = None) -> bool:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in allowed_schemes:
            return False
        if not parsed.hostname:
            return False
        if allowed_domains and parsed.hostname not in allowed_domains:
            return False
        try:
            ip = ipaddress.ip_address(parsed.hostname)
            if ip.is_private or ip.is_loopback or ip.is_reserved:
                return False
        except ValueError:
            pass
        return True
    except Exception:
        return False
