"""Regression tests for v2 task compatibility helpers."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest


def _load_compat_cli():
    path = Path(__file__).resolve().parents[1] / "edict" / "scripts" / "kanban_update_edict.py"
    spec = importlib.util.spec_from_file_location("kanban_update_edict", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _FakeDB:
    def __init__(self, existing: dict[str, object] | None = None):
        self.existing = dict(existing or {})
        self.added: list[object] = []
        self.commits = 0

    async def get(self, _model, key):
        return self.existing.get(key)

    def add(self, obj):
        self.added.append(obj)
        self.existing[getattr(obj, "id", "")] = obj

    async def flush(self):
        return None

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        return None


class _FakeBus:
    def __init__(self):
        self.events: list[dict] = []

    async def publish(self, **payload):
        self.events.append(payload)


def test_create_task_accepts_explicit_task_id_and_initial_state():
    try:
        from edict.backend.app.services.task_service import TaskService
        from edict.backend.app.task_contract import TaskState
    except ModuleNotFoundError as exc:
        pytest.skip(f"optional dependency missing: {exc}")

    db = _FakeDB()
    bus = _FakeBus()
    svc = TaskService(db, bus)

    async def _noop(_entry):
        return None

    svc._persist_task_audit = _noop  # type: ignore[method-assign]

    task = asyncio.run(
        svc.create_task(
            task_id="JJC-COMPAT-001",
            title="兼容任务",
            official="工部尚书",
            target_dept="工部",
            initial_state=TaskState.Doing,
        )
    )

    assert task.id == "JJC-COMPAT-001"
    assert task.state.value == "Doing"
    assert task.org == "工部"
    assert task.flow_log[0]["to"] == "工部"
    assert bus.events[0]["trace_id"] == "JJC-COMPAT-001"


def test_create_task_rejects_duplicate_explicit_task_id():
    try:
        from edict.backend.app.services.task_service import TaskService
    except ModuleNotFoundError as exc:
        pytest.skip(f"optional dependency missing: {exc}")

    existing = type("ExistingTask", (), {"id": "JJC-COMPAT-002"})()
    db = _FakeDB(existing={"JJC-COMPAT-002": existing})
    bus = _FakeBus()
    svc = TaskService(db, bus)

    async def _noop(_entry):
        return None

    svc._persist_task_audit = _noop  # type: ignore[method-assign]

    try:
        asyncio.run(
            svc.create_task(
                task_id="JJC-COMPAT-002",
                title="重复任务",
            )
        )
    except ValueError as exc:
        assert str(exc) == "Task already exists: JJC-COMPAT-002"
    else:
        raise AssertionError("expected duplicate explicit task id to raise ValueError")


def test_compat_cli_create_uses_standard_tasks_endpoint(monkeypatch):
    mod = _load_compat_cli()
    calls: list[tuple[str, dict]] = []

    monkeypatch.setattr(mod, "_check_api", lambda: True)
    monkeypatch.setattr(mod, "_api_post", lambda path, payload: calls.append((path, payload)) or {"taskId": payload["taskId"]})

    mod.cmd_create("JJC-COMPAT-010", "兼容创建任务", "Doing", "工部", "工部尚书")

    assert calls == [
        (
            "/api/tasks",
            {
                "taskId": "JJC-COMPAT-010",
                "title": "兼容创建任务",
                "official": "工部尚书",
                "priority": "normal",
                "targetDept": "工部",
                "initialState": "Doing",
                "actor": "system",
                "source": "kanban-cli-api",
            },
        )
    ]


def test_compat_cli_state_transition_uses_standard_task_route(monkeypatch):
    mod = _load_compat_cli()
    calls: list[tuple[str, dict]] = []

    monkeypatch.setattr(mod, "_check_api", lambda: True)
    monkeypatch.setattr(mod, "_api_post", lambda path, payload: calls.append((path, payload)) or {"message": "ok"})

    mod.cmd_state("JJC-COMPAT-011", "Review", "提交审查")

    assert calls == [
        (
            "/api/tasks/JJC-COMPAT-011/transition",
            {
                "new_state": "Review",
                "actor": "system",
                "source": "kanban-cli-api",
                "reason": "提交审查",
            },
        )
    ]


def test_compat_cli_todo_updates_current_task_snapshot(monkeypatch):
    mod = _load_compat_cli()
    put_calls: list[tuple[str, dict]] = []

    monkeypatch.setattr(mod, "_check_api", lambda: True)
    monkeypatch.setattr(
        mod,
        "_api_get",
        lambda path: {"id": "JJC-COMPAT-012", "todos": [{"id": "1", "title": "旧标题", "status": "not-started"}]},
    )
    monkeypatch.setattr(mod, "_api_put", lambda path, payload: put_calls.append((path, payload)) or {"message": "ok"})
    monkeypatch.setattr(mod, "_api_post", lambda path, payload: None)

    mod.cmd_todo("JJC-COMPAT-012", "1", "", "completed", detail="补充说明")

    assert put_calls == [
        (
            "/api/tasks/JJC-COMPAT-012/todos",
            {
                "actor": "system",
                "source": "kanban-cli-api",
                "todos": [
                    {
                        "id": "1",
                        "title": "旧标题",
                        "status": "completed",
                        "detail": "补充说明",
                    }
                ],
            },
        )
    ]
