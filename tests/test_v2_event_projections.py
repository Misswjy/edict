"""Regression tests for v2 event projections and unified activity views."""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import datetime, timezone
from types import SimpleNamespace

from edict.backend.app.event_contract import TOPIC_AGENT_THOUGHTS, TOPIC_AGENT_TODO_UPDATE
from edict.backend.app.services.activity_service import (
    build_task_activity_payload,
    build_todo_projection_rows,
    build_task_activity_view,
)


def _stub_backend_config():
    module = types.ModuleType("edict.backend.app.config")
    module.get_settings = lambda: SimpleNamespace(
        redis_url="redis://unused",
        database_url="postgresql+asyncpg://unused/edict",
        debug=False,
        port=8000,
        dispatch_timeout_sec=300,
        openclaw_project_dir=None,
    )
    sys.modules["edict.backend.app.config"] = module


def _import_event_bus_with_stubbed_config():
    _stub_backend_config()
    _stub_activity_sqlalchemy_modules()
    sys.modules.pop("edict.backend.app.services.event_bus", None)
    module = importlib.import_module("edict.backend.app.services.event_bus")
    return module.EventBus


def _stub_activity_sqlalchemy_modules():
    class _Stmt:
        def where(self, *args, **kwargs):
            return self

        def order_by(self, *args, **kwargs):
            return self

    sqlalchemy_module = types.ModuleType("sqlalchemy")
    sqlalchemy_module.select = lambda *args, **kwargs: _Stmt()
    sqlalchemy_module.delete = lambda *args, **kwargs: _Stmt()
    sys.modules["sqlalchemy"] = sqlalchemy_module

    task_module = types.ModuleType("edict.backend.app.models.task")
    task_module.Task = type("FakeTaskModel", (), {})
    sys.modules["edict.backend.app.models.task"] = task_module

    event_module = types.ModuleType("edict.backend.app.models.event")
    event_module.Event = type("FakeEventModel", (), {"trace_id": "trace_id", "timestamp": SimpleNamespace(asc=lambda: None)})
    sys.modules["edict.backend.app.models.event"] = event_module

    thought_module = types.ModuleType("edict.backend.app.models.thought")
    thought_module.Thought = type(
        "FakeThoughtModel",
        (),
        {
            "__init__": lambda self, **kwargs: self.__dict__.update(kwargs),
            "trace_id": "trace_id",
            "timestamp": SimpleNamespace(asc=lambda: None),
        },
    )
    sys.modules["edict.backend.app.models.thought"] = thought_module

    todo_module = types.ModuleType("edict.backend.app.models.todo")
    todo_module.Todo = type(
        "FakeTodoModel",
        (),
        {
            "__init__": lambda self, **kwargs: self.__dict__.update(kwargs),
            "trace_id": "trace_id",
            "updated_at": SimpleNamespace(asc=lambda: None),
            "created_at": SimpleNamespace(asc=lambda: None),
        },
    )
    sys.modules["edict.backend.app.models.todo"] = todo_module


class _FakeSession:
    def __init__(self):
        self.added = []
        self.executed = []
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def add(self, item):
        self.added.append(item)

    def add_all(self, items):
        self.added.extend(items)

    async def execute(self, stmt):
        self.executed.append(stmt)
        return None

    async def commit(self):
        self.commits += 1


def test_event_bus_persists_agent_thoughts_projection():
    EventBus = _import_event_bus_with_stubbed_config()
    session = _FakeSession()
    bus = EventBus(session_factory=session)

    asyncio.run(
        bus._persist_auxiliary_records(
            TOPIC_AGENT_THOUGHTS,
            "JJC-PROJ-THOUGHT-1",
            "agent.gongbu",
            {
                "agent": "gongbu",
                "output": "已完成关键实现",
                "tokens": 42,
                "confidence": 0.9,
            },
            {"step": 3},
        )
    )

    assert session.commits == 1
    assert len(session.added) == 1
    thought = session.added[0]
    assert thought.trace_id == "JJC-PROJ-THOUGHT-1"
    assert thought.agent == "gongbu"
    assert thought.content == "已完成关键实现"
    assert thought.tokens == 42


def test_event_bus_projects_agent_todos_into_todo_rows():
    EventBus = _import_event_bus_with_stubbed_config()
    session = _FakeSession()
    bus = EventBus(session_factory=session)

    asyncio.run(
        bus._persist_auxiliary_records(
            TOPIC_AGENT_TODO_UPDATE,
            "JJC-PROJ-TODO-1",
            "zhongshu",
            {
                "items": [
                    {"id": "1", "title": "需求分析", "status": "completed"},
                    {"id": "2", "title": "方案设计", "status": "in-progress", "detail": "补齐边界条件"},
                ],
                "source_of_truth": "tasks.todos",
            },
            {"version": 4},
        )
    )

    assert session.commits == 1
    assert len(session.executed) == 1
    assert len(session.added) == 2
    first, second = session.added
    assert first.trace_id == "JJC-PROJ-TODO-1"
    assert first.status == "done"
    assert second.status == "in_progress"
    assert second.description == "补齐边界条件"
    assert second.metadata_["source_of_truth"] == "tasks.todos"


def test_build_task_activity_payload_merges_snapshot_events_and_projections():
    task_snapshot = {
        "id": "JJC-ACT-001",
        "title": "统一活动流",
        "state": "Doing",
        "org": "工部",
        "targetDept": "工部",
        "updatedAt": "2026-03-25T12:05:00+00:00",
        "flow_log": [
            {"at": "2026-03-25T12:00:00+00:00", "from": "尚书省", "to": "工部", "remark": "派发执行"}
        ],
        "progress_log": [
            {
                "at": "2026-03-25T12:02:00+00:00",
                "agent": "gongbu",
                "text": "正在实现事件投影",
                "state": "Doing",
                "org": "工部",
                "tokens": 120,
                "elapsed": 30,
            }
        ],
        "todos": [
            {"id": "1", "title": "需求分析", "status": "completed"},
            {"id": "2", "title": "方案设计", "status": "in-progress"},
        ],
    }
    events = [
        SimpleNamespace(
            topic="task.created",
            event_type="task.created",
            producer="emperor",
            payload={
                "title": "统一活动流",
                "flow_log": [
                    {"at": "2026-03-25T11:50:00+00:00", "from": "皇上", "to": "司礼监", "remark": "下旨：统一活动流"}
                ],
            },
            meta={},
            timestamp=datetime(2026, 3, 25, 11, 50, tzinfo=timezone.utc),
        ),
        SimpleNamespace(
            topic="agent.todo.update",
            event_type="task.todos.updated",
            producer="zhongshu",
            payload={
                "items": [
                    {"id": "1", "title": "需求分析", "status": "completed"},
                    {"id": "2", "title": "方案设计", "status": "not-started"},
                ]
            },
            meta={},
            timestamp=datetime(2026, 3, 25, 11, 55, tzinfo=timezone.utc),
        ),
        SimpleNamespace(
            topic="agent.todo.update",
            event_type="task.todos.updated",
            producer="zhongshu",
            payload={
                "items": [
                    {"id": "1", "title": "需求分析", "status": "completed"},
                    {"id": "2", "title": "方案设计", "status": "in-progress"},
                ]
            },
            meta={},
            timestamp=datetime(2026, 3, 25, 12, 1, tzinfo=timezone.utc),
        ),
    ]
    thoughts = [
        SimpleNamespace(
            trace_id="JJC-ACT-001",
            agent="gongbu",
            type="summary",
            source="tool",
            content="已完成事件契约初稿",
            tokens=16,
            timestamp=datetime(2026, 3, 25, 12, 3, tzinfo=timezone.utc),
        )
    ]
    projected_rows = [
        SimpleNamespace(
            todo_id="todo-1",
            trace_id="JJC-ACT-001",
            title="需求分析",
            description="",
            status="done",
            metadata_={"order": 0, "legacyItemId": "1", "raw": {"id": "1", "title": "需求分析", "status": "completed"}},
            created_at=datetime(2026, 3, 25, 12, 1, tzinfo=timezone.utc),
        ),
        SimpleNamespace(
            todo_id="todo-2",
            trace_id="JJC-ACT-001",
            title="方案设计",
            description="",
            status="in_progress",
            metadata_={"order": 1, "legacyItemId": "2", "raw": {"id": "2", "title": "方案设计", "status": "in-progress"}},
            created_at=datetime(2026, 3, 25, 12, 1, tzinfo=timezone.utc),
        ),
    ]

    payload = build_task_activity_payload(task_snapshot, events, thoughts, projected_rows)

    kinds = [item["kind"] for item in payload["activity"]]
    assert payload["taskMeta"]["title"] == "统一活动流"
    assert payload["agentId"] == "gongbu"
    assert "flow" in kinds
    assert "progress" in kinds
    assert "todos" in kinds
    assert "assistant" in kinds
    assert payload["todosSummary"]["completed"] == 1
    assert payload["todosSummary"]["inProgress"] == 1
    todo_entry = next(
        item
        for item in payload["activity"]
        if item["kind"] == "todos" and item.get("diff") and item["diff"].get("changed")
    )
    assert todo_entry["diff"]["changed"][0]["to"] == "in-progress"
    assert payload["resourceSummary"]["totalTokens"] == 136
    assert "gongbu" in payload["relatedAgents"]


def test_build_task_activity_view_reads_projected_state_from_db():
    _stub_activity_sqlalchemy_modules()

    class FakeTask:
        def to_dict(self):
            return {
                "id": "JJC-ACT-DB-1",
                "title": "DB 活动流",
                "state": "Doing",
                "org": "工部",
                "targetDept": "工部",
                "updatedAt": "2026-03-25T13:00:00+00:00",
                "flow_log": [],
                "progress_log": [],
                "todos": [],
            }

    class FakeResult:
        def __init__(self, rows):
            self._rows = rows

        def scalars(self):
            return self

        def all(self):
            return self._rows

    class FakeDb:
        def __init__(self):
            self.calls = 0

        async def get(self, model, task_id):
            assert task_id == "JJC-ACT-DB-1"
            return FakeTask()

        async def execute(self, stmt):
            self.calls += 1
            if self.calls == 1:
                return FakeResult(
                    [
                        SimpleNamespace(
                            topic="task.created",
                            event_type="task.created",
                            producer="emperor",
                            payload={
                                "title": "DB 活动流",
                                "flow_log": [
                                    {"at": "2026-03-25T12:00:00+00:00", "from": "皇上", "to": "司礼监", "remark": "下旨"}
                                ],
                            },
                            meta={},
                            timestamp=datetime(2026, 3, 25, 12, 0, tzinfo=timezone.utc),
                        )
                    ]
                )
            if self.calls == 2:
                return FakeResult(
                    [
                        SimpleNamespace(
                            trace_id="JJC-ACT-DB-1",
                            agent="gongbu",
                            type="summary",
                            source="tool",
                            content="同步成功",
                            tokens=8,
                            timestamp=datetime(2026, 3, 25, 12, 10, tzinfo=timezone.utc),
                        )
                    ]
                )
            return FakeResult([])

    payload = asyncio.run(build_task_activity_view(FakeDb(), "JJC-ACT-DB-1"))

    assert payload["ok"] is True
    assert payload["taskId"] == "JJC-ACT-DB-1"
    assert payload["projectedThoughtCount"] == 1
    assert any(item["kind"] == "assistant" for item in payload["activity"])


def test_build_todo_projection_rows_keeps_tasks_todos_as_truth_source():
    rows = build_todo_projection_rows(
        trace_id="JJC-ROW-1",
        items=[{"id": "7", "title": "补测试", "status": "not-started"}],
        producer="shangshu",
        source_of_truth="tasks.todos",
        projected_at=datetime(2026, 3, 25, 0, 0, tzinfo=timezone.utc),
    )

    assert len(rows) == 1
    assert rows[0]["status"] == "open"
    assert rows[0]["metadata_"]["source_of_truth"] == "tasks.todos"
    assert rows[0]["title"] == "补测试"
