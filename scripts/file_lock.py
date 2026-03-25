"""
文件锁工具 — 防止多进程并发读写 JSON 文件导致数据丢失。

用法:
    from file_lock import atomic_json_update, atomic_json_read

    # 原子读取
    data = atomic_json_read(path, default=[])

    # 原子更新（读 → 修改 → 写回，全程持锁）
    def modifier(tasks):
        tasks.append(new_task)
        return tasks 
    atomic_json_update(path, modifier, default=[])
"""
import fcntl
import json
import os
import pathlib
import tempfile
from typing import Any, Callable

BASE = pathlib.Path(__file__).resolve().parent.parent
LEGACY_WRITE_FREEZE_MARKER = BASE / 'ops' / 'cutover' / '.legacy_write_frozen'


class LegacyWriteFrozenError(RuntimeError):
    """Raised when legacy task writes are blocked during v2 cutover."""

    def __init__(self, path: pathlib.Path, marker: pathlib.Path):
        self.path = pathlib.Path(path)
        self.freeze_marker = pathlib.Path(marker)
        super().__init__('legacy runtime is read-only during v2 cutover')


def _lock_path(path: pathlib.Path) -> pathlib.Path:
    return path.parent / (path.name + '.lock')


def legacy_write_freeze_marker_path() -> pathlib.Path:
    raw = (os.environ.get('EDICT_LEGACY_WRITE_FREEZE_MARKER') or '').strip()
    return pathlib.Path(raw).expanduser() if raw else LEGACY_WRITE_FREEZE_MARKER


def legacy_tasks_write_blocked(path: pathlib.Path) -> bool:
    return pathlib.Path(path).name == 'tasks_source.json' and legacy_write_freeze_marker_path().exists()


def assert_legacy_tasks_writable(path: pathlib.Path) -> None:
    marker = legacy_write_freeze_marker_path()
    if pathlib.Path(path).name == 'tasks_source.json' and marker.exists():
        raise LegacyWriteFrozenError(pathlib.Path(path), marker)


def atomic_json_read(path: pathlib.Path, default: Any = None) -> Any:
    """持锁读取 JSON 文件。"""
    lock_file = _lock_path(path)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        try:
            return json.loads(path.read_text()) if path.exists() else default
        except Exception:
            return default
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def atomic_json_update(
    path: pathlib.Path,
    modifier: Callable[[Any], Any],
    default: Any = None,
) -> Any:
    """
    原子地读取 → 修改 → 写回 JSON 文件。
    modifier(data) 应返回修改后的数据。
    使用临时文件 + rename 保证写入原子性。
    """
    lock_file = _lock_path(path)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert_legacy_tasks_writable(path)
        # Read
        try:
            data = json.loads(path.read_text()) if path.exists() else default
        except Exception:
            data = default
        # Modify
        result = modifier(data)
        # Atomic write via temp file + rename
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent), suffix='.tmp', prefix=path.stem + '_'
        )
        try:
            with os.fdopen(tmp_fd, 'w') as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, str(path))
        except Exception:
            os.unlink(tmp_path)
            raise
        return result
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def atomic_json_write(path: pathlib.Path, data: Any) -> None:
    """原子写入 JSON 文件（持排他锁 + tmpfile rename）。
    直接写入，不读取现有内容（避免 atomic_json_update 的多余读开销）。
    """
    lock_file = _lock_path(path)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert_legacy_tasks_writable(path)
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent), suffix='.tmp', prefix=path.stem + '_'
        )
        try:
            with os.fdopen(tmp_fd, 'w') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, str(path))
        except Exception:
            os.unlink(tmp_path)
            raise
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
