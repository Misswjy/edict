"""backend dispatch/orchestrator regression tests"""

import asyncio
import importlib
import sys
import types
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace


def _stub_backend_config(monkeypatch):
    module = types.ModuleType("edict.backend.app.config")
    module.get_settings = lambda: SimpleNamespace(
        redis_url="redis://unused",
        database_url="postgresql+asyncpg://unused/edict",
        debug=False,
        port=8000,
        dispatch_timeout_sec=300,
        openclaw_project_dir=None,
    )
    monkeypatch.setitem(sys.modules, "edict.backend.app.config", module)


def _import_task_service_with_stubs(monkeypatch):
    class FakeIntegrityError(Exception):
        pass

    sqlalchemy_module = types.ModuleType("sqlalchemy")
    sqlalchemy_module.and_ = lambda *args, **kwargs: None
    sqlalchemy_module.func = SimpleNamespace(count=lambda *args, **kwargs: None)
    sqlalchemy_module.select = lambda *args, **kwargs: None

    sqlalchemy_exc = types.ModuleType("sqlalchemy.exc")
    sqlalchemy_exc.IntegrityError = FakeIntegrityError

    sqlalchemy_ext = types.ModuleType("sqlalchemy.ext")
    sqlalchemy_asyncio = types.ModuleType("sqlalchemy.ext.asyncio")
    sqlalchemy_asyncio.AsyncSession = object

    monkeypatch.setitem(sys.modules, "sqlalchemy", sqlalchemy_module)
    monkeypatch.setitem(sys.modules, "sqlalchemy.exc", sqlalchemy_exc)
    monkeypatch.setitem(sys.modules, "sqlalchemy.ext", sqlalchemy_ext)
    monkeypatch.setitem(sys.modules, "sqlalchemy.ext.asyncio", sqlalchemy_asyncio)

    task_module = types.ModuleType("edict.backend.app.models.task")

    class FakeTask:
        def __init__(self, **kwargs):
            self.archived_at = None
            self.created_at = kwargs.get("created_at")
            self.updated_at = kwargs.get("updated_at")
            self.__dict__.update(kwargs)

        def to_dict(self):
            state = self.state.value if hasattr(self.state, "value") else self.state
            created_at = self.created_at.isoformat() if self.created_at else ""
            updated_at = self.updated_at.isoformat() if self.updated_at else ""
            return {
                "id": self.id,
                "title": self.title,
                "official": self.official,
                "org": self.org,
                "state": state,
                "now": self.now,
                "eta": getattr(self, "eta", "-"),
                "block": getattr(self, "block", "无"),
                "output": getattr(self, "output", ""),
                "ac": getattr(self, "ac", ""),
                "priority": self.priority,
                "lane": self.lane,
                "review_round": getattr(self, "review_round", 0),
                "archived": getattr(self, "archived", False),
                "archivedAt": None,
                "flow_log": self.flow_log,
                "progress_log": self.progress_log,
                "consultLog": self.consult_log,
                "todos": self.todos,
                "templateId": self.template_id,
                "templateParams": self.template_params,
                "targetDept": self.target_dept,
                "_prev_state": getattr(self, "prev_state", ""),
                "_stateVersion": getattr(self, "state_version", 1),
                "_scheduler": getattr(self, "scheduler", {}),
                "createdAt": created_at,
                "updatedAt": updated_at,
            }

    task_module.Task = FakeTask
    monkeypatch.setitem(sys.modules, "edict.backend.app.models.task", task_module)
    sys.modules.pop("edict.backend.app.services.task_service", None)
    module = importlib.import_module("edict.backend.app.services.task_service")
    return module, FakeIntegrityError


def test_orchestrator_emits_stable_dispatch_key_from_status_snapshot(monkeypatch):
    _stub_backend_config(monkeypatch)
    from edict.backend.app.workers.orchestrator_worker import OrchestratorWorker

    calls = []

    class FakeBus:
        async def publish(self, **kwargs):
            calls.append(kwargs)
            return "1-0"

    worker = OrchestratorWorker()
    worker.bus = FakeBus()

    payload = {
        "task_id": "JJC-TEST-900",
        "to": "Doing",
        "task": {
            "id": "JJC-TEST-900",
            "state": "Doing",
            "org": "工部",
            "targetDept": "工部",
            "_stateVersion": 3,
        },
    }
    meta = {"version": 3, "request_id": "req-status-1"}

    asyncio.run(worker._on_task_status("task.state.Doing", payload, meta, "JJC-TEST-900"))

    assert len(calls) == 1
    call = calls[0]
    assert call["topic"] == "task.dispatch"
    assert call["dedupe_key"] == "auto:JJC-TEST-900:Doing:v3:gongbu"
    assert call["payload"]["dispatch_key"] == "auto:JJC-TEST-900:Doing:v3:gongbu"
    assert call["payload"]["version"] == 3
    assert call["payload"]["agent"] == "gongbu"


def test_orchestrator_resolves_execution_agent_from_top_level_status_payload(monkeypatch):
    _stub_backend_config(monkeypatch)
    from edict.backend.app.workers.orchestrator_worker import OrchestratorWorker

    calls = []

    class FakeBus:
        async def publish(self, **kwargs):
            calls.append(kwargs)
            return "1-0"

    worker = OrchestratorWorker()
    worker.bus = FakeBus()

    payload = {
        "task_id": "JJC-TEST-901A",
        "to": "Doing",
        "org": "工部",
        "targetDept": "工部",
        "_stateVersion": 5,
        "lane": "fast",
    }
    meta = {"version": 5, "request_id": "req-status-2"}

    asyncio.run(worker._on_task_status("task.state.Doing", payload, meta, "JJC-TEST-901A"))

    assert len(calls) == 1
    assert calls[0]["payload"]["agent"] == "gongbu"
    assert calls[0]["payload"]["dispatch_key"] == "auto:JJC-TEST-901A:Doing:v5:gongbu"
    assert calls[0]["payload"]["task"]["targetDept"] == "工部"


def test_orchestrator_wakes_agent_on_scheduler_escalated_event(monkeypatch):
    _stub_backend_config(monkeypatch)
    from edict.backend.app.workers.orchestrator_worker import OrchestratorWorker

    calls = []

    class FakeAdminActions:
        async def wake_agent(self, agent_id, message, actor, task_id=""):
            calls.append(
                {
                    "agent_id": agent_id,
                    "message": message,
                    "actor_id": actor.actor_id,
                    "request_id": actor.request_id,
                    "task_id": task_id,
                }
            )
            return {"ok": True, "message": "queued"}

    worker = OrchestratorWorker()
    worker.admin_actions = FakeAdminActions()

    payload = {
        "task_id": "JJC-TEST-ESC-1",
        "state": "Doing",
        "target": "menxia",
        "reason": "停滞 600 秒",
    }
    meta = {"source": "scheduler", "request_id": "req-escalate-1"}

    asyncio.run(worker._on_task_escalated(payload, meta, "JJC-TEST-ESC-1"))

    assert len(calls) == 1
    assert calls[0]["agent_id"] == "menxia"
    assert calls[0]["actor_id"] == "sili"
    assert calls[0]["request_id"] == "req-escalate-1"
    assert calls[0]["task_id"] == "JJC-TEST-ESC-1"
    assert "任务ID: JJC-TEST-ESC-1" in calls[0]["message"]
    assert "当前状态: Doing" in calls[0]["message"]
    assert "原因: 停滞 600 秒" in calls[0]["message"]


def test_orchestrator_reports_worker_heartbeat(monkeypatch):
    _stub_backend_config(monkeypatch)
    from edict.backend.app.workers.orchestrator_worker import OrchestratorWorker

    heartbeats = []

    class FakeBus:
        async def report_worker_heartbeat(self, worker_name, instance_id, *, status="running", extra=None, ttl_sec=120):
            heartbeats.append(
                {
                    "worker_name": worker_name,
                    "instance_id": instance_id,
                    "status": status,
                    "extra": dict(extra or {}),
                    "ttl_sec": ttl_sec,
                }
            )

    worker = OrchestratorWorker()
    worker.bus = FakeBus()

    asyncio.run(worker._heartbeat(status="running"))

    assert len(heartbeats) == 1
    assert heartbeats[0]["worker_name"] == "orchestrator"
    assert heartbeats[0]["status"] == "running"
    assert "task.created" in heartbeats[0]["extra"]["topics"]


def test_dispatch_worker_reports_active_dispatch_heartbeat(monkeypatch):
    _stub_backend_config(monkeypatch)
    from edict.backend.app.workers.dispatch_worker import DispatchWorker

    heartbeats = []

    class FakeBus:
        async def report_worker_heartbeat(self, worker_name, instance_id, *, status="running", extra=None, ttl_sec=120):
            heartbeats.append(
                {
                    "worker_name": worker_name,
                    "instance_id": instance_id,
                    "status": status,
                    "extra": dict(extra or {}),
                    "ttl_sec": ttl_sec,
                }
            )

    worker = DispatchWorker()
    worker.bus = FakeBus()

    asyncio.run(worker._heartbeat(status="running", active_dispatches=2))

    assert len(heartbeats) == 1
    assert heartbeats[0]["worker_name"] == "dispatcher"
    assert heartbeats[0]["status"] == "running"
    assert heartbeats[0]["extra"]["active_dispatches"] == 2
    assert heartbeats[0]["extra"]["group"] == "dispatcher"


def test_task_service_create_task_retries_on_id_collision(monkeypatch):
    _stub_backend_config(monkeypatch)
    task_service_module, fake_integrity_error = _import_task_service_with_stubs(monkeypatch)
    from edict.backend.app.task_contract import make_actor_context

    class RetrySession:
        def __init__(self):
            self.tasks = []
            self.commit_calls = 0
            self.rollback_calls = 0

        def add(self, task):
            self.tasks.append(task)

        async def flush(self):
            return None

        async def commit(self):
            self.commit_calls += 1
            if self.commit_calls == 1:
                raise fake_integrity_error("duplicate task id")
            return None

        async def rollback(self):
            self.rollback_calls += 1

    class FakeBus:
        def __init__(self):
            self.calls = []

        async def publish(self, **kwargs):
            self.calls.append(kwargs)
            return "1-0"

    svc = task_service_module.TaskService(RetrySession(), FakeBus())
    ids = iter(["JJC-20260324-001", "JJC-20260324-002"])

    async def fake_next_task_id():
        return next(ids)

    monkeypatch.setattr(svc, "_next_task_id", fake_next_task_id)

    task = asyncio.run(
        svc.create_task(
            "并发创建去重",
            actor=make_actor_context("system", source="test"),
        )
    )

    assert task.id == "JJC-20260324-002"
    assert svc.db.rollback_calls == 1
    assert len(svc.bus.calls) == 1
    assert svc.bus.calls[0]["payload"]["id"] == "JJC-20260324-002"


def test_dispatch_worker_skips_duplicate_dispatch_execution(monkeypatch):
    _stub_backend_config(monkeypatch)
    from edict.backend.app.workers.dispatch_worker import DispatchWorker

    class FakeBus:
        def __init__(self):
            self.acked = []
            self.published = []
            self.completed = []
            self.claimed = set()

        async def claim_dispatch_execution(self, dispatch_key, ttl_sec=0):
            if dispatch_key in self.claimed:
                return "completed"
            self.claimed.add(dispatch_key)
            return "acquired"

        async def publish(self, **kwargs):
            self.published.append(kwargs)
            return "1-0"

        async def ack(self, topic, group, entry_id):
            self.acked.append((topic, group, entry_id))

        async def mark_dispatch_execution_complete(self, dispatch_key, **kwargs):
            self.completed.append((dispatch_key, kwargs))

    worker = DispatchWorker(max_concurrent=1)
    worker.bus = FakeBus()
    call_count = {"n": 0}

    async def fake_call_openclaw(agent, message, task_id, trace_id):
        call_count["n"] += 1
        return {"returncode": 0, "stdout": "ok", "stderr": ""}

    worker._call_openclaw = fake_call_openclaw

    event = {
        "trace_id": "JJC-TEST-901",
        "payload": {
            "task_id": "JJC-TEST-901",
            "agent": "gongbu",
            "message": "请继续推进",
            "state": "Doing",
            "dispatch_key": "auto:JJC-TEST-901:Doing:v4:gongbu",
            "version": 4,
        },
        "meta": {
            "dispatch_key": "auto:JJC-TEST-901:Doing:v4:gongbu",
            "version": 4,
        },
    }

    asyncio.run(worker._dispatch("1-0", event))
    asyncio.run(worker._dispatch("2-0", event))

    assert call_count["n"] == 1
    assert len(worker.bus.completed) == 1
    assert len(worker.bus.acked) == 2


def test_event_bus_publish_skips_duplicate_dedupe_key(monkeypatch):
    _stub_backend_config(monkeypatch)
    from edict.backend.app.services.event_bus import EventBus

    class FakeRedis:
        def __init__(self):
            self.xadd_calls = []
            self.pubsub_calls = []

        async def xadd(self, stream_key, event, maxlen=10000):
            self.xadd_calls.append((stream_key, event, maxlen))
            return "1-0"

        async def publish(self, channel, payload):
            self.pubsub_calls.append((channel, payload))

    bus = EventBus()
    bus._redis = FakeRedis()
    record = SimpleNamespace(event_id="evt-1", timestamp=SimpleNamespace(isoformat=lambda: "2026-03-24T00:00:00+00:00"), stream_entry_id=None)

    state = {"created": False}

    async def fake_get_or_create_event(**kwargs):
        if not state["created"]:
            state["created"] = True
            return record, True
        record.stream_entry_id = "1-0"
        return record, False

    async def fake_mark_event_published(event_id, stream_entry_id):
        record.stream_entry_id = stream_entry_id

    async def fake_persist_auxiliary_records(*args, **kwargs):
        return None

    monkeypatch.setattr(bus, "_get_or_create_event", fake_get_or_create_event)
    monkeypatch.setattr(bus, "_mark_event_published", fake_mark_event_published)
    monkeypatch.setattr(bus, "_persist_auxiliary_records", fake_persist_auxiliary_records)

    asyncio.run(
        bus.publish(
            topic="task.dispatch",
            trace_id="JJC-TEST-902",
            event_type="task.dispatch.request",
            producer="orchestrator",
            payload={"task_id": "JJC-TEST-902"},
            meta={"version": 1},
            dedupe_key="auto:JJC-TEST-902:Doing:v1:gongbu",
        )
    )
    asyncio.run(
        bus.publish(
            topic="task.dispatch",
            trace_id="JJC-TEST-902",
            event_type="task.dispatch.request",
            producer="orchestrator",
            payload={"task_id": "JJC-TEST-902"},
            meta={"version": 1},
            dedupe_key="auto:JJC-TEST-902:Doing:v1:gongbu",
        )
    )

    assert len(bus.redis.xadd_calls) == 1
    assert len(bus.redis.pubsub_calls) == 1


def test_orchestrator_acknowledges_recovered_stale_events(monkeypatch):
    _stub_backend_config(monkeypatch)
    from edict.backend.app.workers.orchestrator_worker import (
        CONSUMER,
        GROUP,
        OrchestratorWorker,
        TOPIC_TASK_STATUS,
    )

    class FakeBus:
        def __init__(self):
            self.acks = []

        async def claim_stale(self, topic, group, consumer, min_idle_ms=0, count=0):
            if topic != TOPIC_TASK_STATUS:
                return []
            assert group == GROUP
            assert consumer == CONSUMER
            return [
                (
                    "9-0",
                    {
                        "event_type": "task.state.Doing",
                        "trace_id": "JJC-TEST-902A",
                        "payload": {
                            "task_id": "JJC-TEST-902A",
                            "to": "Doing",
                            "org": "工部",
                            "targetDept": "工部",
                            "_stateVersion": 6,
                        },
                        "meta": {"version": 6, "request_id": "req-stale-1"},
                    },
                )
            ]

        async def ack(self, topic, group, entry_id):
            self.acks.append((topic, group, entry_id))

        async def publish(self, **kwargs):
            return "1-0"

    worker = OrchestratorWorker()
    worker.bus = FakeBus()

    asyncio.run(worker._recover_pending())

    assert worker.bus.acks == [(TOPIC_TASK_STATUS, GROUP, "9-0")]


def test_dispatch_worker_recovers_stale_dispatch_events_and_acks(monkeypatch):
    _stub_backend_config(monkeypatch)
    from edict.backend.app.workers.dispatch_worker import CONSUMER, GROUP, DispatchWorker, TOPIC_TASK_DISPATCH

    class FakeBus:
        def __init__(self):
            self.acks = []
            self.published = []
            self.completed = []

        async def claim_stale(self, topic, group, consumer, min_idle_ms=0, count=0):
            assert topic == TOPIC_TASK_DISPATCH
            assert group == GROUP
            assert consumer == CONSUMER
            return [
                (
                    "7-0",
                    {
                        "trace_id": "JJC-TEST-RECOVER-1",
                        "payload": {
                            "task_id": "JJC-TEST-RECOVER-1",
                            "agent": "gongbu",
                            "message": "请继续推进",
                            "state": "Doing",
                            "dispatch_key": "auto:JJC-TEST-RECOVER-1:Doing:v3:gongbu",
                            "version": 3,
                        },
                        "meta": {
                            "dispatch_key": "auto:JJC-TEST-RECOVER-1:Doing:v3:gongbu",
                            "version": 3,
                        },
                    },
                )
            ]

        async def claim_dispatch_execution(self, dispatch_key, ttl_sec=0):
            return "acquired"

        async def publish(self, **kwargs):
            self.published.append(kwargs)
            return "1-0"

        async def ack(self, topic, group, entry_id):
            self.acks.append((topic, group, entry_id))

        async def mark_dispatch_execution_complete(self, dispatch_key, **kwargs):
            self.completed.append((dispatch_key, kwargs))

    worker = DispatchWorker(max_concurrent=1)
    worker.bus = FakeBus()

    async def fake_call_openclaw(agent, message, task_id, trace_id):
        return {"returncode": 0, "stdout": "ok", "stderr": ""}

    worker._call_openclaw = fake_call_openclaw

    asyncio.run(worker._recover_pending())

    assert worker.bus.acks == [(TOPIC_TASK_DISPATCH, GROUP, "7-0")]
    assert len(worker.bus.published) == 2
    assert worker.bus.published[0]["dedupe_key"] == "dispatch-heartbeat:auto:JJC-TEST-RECOVER-1:Doing:v3:gongbu:start"
    assert worker.bus.published[1]["dedupe_key"] == "dispatch-output:auto:JJC-TEST-RECOVER-1:Doing:v3:gongbu"
    assert len(worker.bus.completed) == 1


def test_scheduler_scan_publishes_stalled_event_before_retry(monkeypatch):
    _stub_backend_config(monkeypatch)
    task_service_module, _ = _import_task_service_with_stubs(monkeypatch)
    from edict.backend.app.task_contract import TaskState, make_actor_context

    class FakeSession:
        async def commit(self):
            return None

    class FakeBus:
        def __init__(self):
            self.calls = []

        async def publish(self, **kwargs):
            self.calls.append(kwargs)
            return "1-0"

    svc = task_service_module.TaskService(FakeSession(), FakeBus())
    now = datetime.now(timezone.utc)
    task = task_service_module.Task(
        id="JJC-TEST-STALL-1",
        title="停滞任务",
        official="工部尚书",
        org="工部",
        state=TaskState.Doing,
        now="处理中",
        eta="-",
        block="无",
        output="",
        ac="",
        priority="normal",
        lane="standard",
        review_round=0,
        archived=False,
        template_id="",
        template_params={},
        target_dept="工部",
        prev_state="",
        state_version=4,
        flow_log=[],
        progress_log=[],
        consult_log=[],
        todos=[],
        scheduler={
            "enabled": True,
            "stallThresholdSec": 60,
            "maxRetry": 2,
            "retryCount": 0,
            "escalationLevel": 0,
            "autoRollback": True,
            "lastProgressAt": (now - timedelta(minutes=5)).isoformat(),
            "stallSince": None,
        },
        created_at=now - timedelta(hours=1),
        updated_at=now - timedelta(minutes=5),
    )

    async def fake_list_tasks(limit=500):
        return [task]

    retries = []

    async def fake_scheduler_retry(task_id, *, reason="", actor=None, dispatch_trigger="", automatic=False, stalled_sec=0):
        retries.append((task_id, reason, actor.actor_id if actor else "", dispatch_trigger, automatic, stalled_sec))
        return {"ok": True, "message": "retried"}

    svc.list_tasks = fake_list_tasks
    svc._scheduler_retry_internal = fake_scheduler_retry

    result = asyncio.run(svc.scheduler_scan(threshold_sec=60, actor=make_actor_context("sili", source="scheduler")))

    assert result["count"] == 1
    assert result["actions"][0]["action"] == "retry"
    assert len(retries) == 1
    assert retries[0][0] == "JJC-TEST-STALL-1"
    assert "停滞" in retries[0][1]
    assert retries[0][2] == "sili"
    assert retries[0][3] == "sili-scan-retry"
    assert retries[0][4] is True
    assert retries[0][5] >= 300
    assert len(svc.bus.calls) == 1
    stalled_event = svc.bus.calls[0]
    assert stalled_event["topic"] == "task.stalled"
    assert stalled_event["event_type"] == "task.scheduler.stalled"
    assert stalled_event["dedupe_key"] == "task-stalled:JJC-TEST-STALL-1:Doing:v4"
    assert stalled_event["payload"]["retryCount"] == 0
    assert stalled_event["payload"]["thresholdSec"] == 60
    assert stalled_event["payload"]["stalledSec"] >= 300


def test_scheduler_retry_records_legacy_dispatch_metadata(monkeypatch):
    _stub_backend_config(monkeypatch)
    task_service_module, _ = _import_task_service_with_stubs(monkeypatch)
    from edict.backend.app.task_contract import TaskState, make_actor_context

    class FakeSession:
        async def commit(self):
            return None

    class FakeBus:
        def __init__(self):
            self.calls = []

        async def publish(self, **kwargs):
            self.calls.append(kwargs)
            return "1-0"

    svc = task_service_module.TaskService(FakeSession(), FakeBus())
    now = datetime.now(timezone.utc)
    task = task_service_module.Task(
        id="JJC-TEST-RETRY-1",
        title="重试任务",
        official="工部尚书",
        org="工部",
        state=TaskState.Doing,
        now="处理中",
        eta="-",
        block="无",
        output="",
        ac="",
        priority="normal",
        lane="standard",
        review_round=0,
        archived=False,
        template_id="",
        template_params={},
        target_dept="工部",
        prev_state="",
        state_version=4,
        flow_log=[],
        progress_log=[],
        consult_log=[],
        todos=[],
        scheduler={
            "enabled": True,
            "stallThresholdSec": 60,
            "maxRetry": 2,
            "retryCount": 0,
            "escalationLevel": 0,
            "autoRollback": True,
            "lastProgressAt": (now - timedelta(minutes=5)).isoformat(),
            "stallSince": None,
        },
        created_at=now - timedelta(hours=1),
        updated_at=now - timedelta(minutes=5),
    )

    async def fake_get_task(task_id, for_update=False):
        assert task_id == "JJC-TEST-RETRY-1"
        return task

    svc._get_task = fake_get_task

    result = asyncio.run(
        svc.scheduler_retry(
            "JJC-TEST-RETRY-1",
            reason="超时未推进",
            actor=make_actor_context("sili", source="scheduler"),
        )
    )

    assert result["ok"] is True
    assert task.scheduler["retryCount"] == 1
    assert task.scheduler["lastDispatchTrigger"] == "sili-retry"
    assert task.scheduler["lastDispatchStatus"] == "queued"
    assert task.scheduler["lastDispatchAgent"] == "gongbu"
    assert task.scheduler["lastDispatchKey"].startswith("manual:JJC-TEST-RETRY-1:Doing:v4:gongbu:")
    assert any("触发重试第1次" in entry["remark"] for entry in task.flow_log)
    assert any("已入队派发：Doing → gongbu（sili-retry）" in entry["remark"] for entry in task.flow_log)
    assert len(svc.bus.calls) == 1
    assert svc.bus.calls[0]["payload"]["agent"] == "gongbu"


def test_scheduler_rollback_auto_mode_uses_legacy_scan_copy(monkeypatch):
    _stub_backend_config(monkeypatch)
    task_service_module, _ = _import_task_service_with_stubs(monkeypatch)
    from edict.backend.app.task_contract import TaskState, make_actor_context

    class FakeSession:
        async def commit(self):
            return None

    class FakeBus:
        async def publish(self, **kwargs):
            return "1-0"

    svc = task_service_module.TaskService(FakeSession(), FakeBus())
    now = datetime.now(timezone.utc)
    task = task_service_module.Task(
        id="JJC-TEST-ROLLBACK-1",
        title="回滚任务",
        official="尚书令",
        org="尚书省",
        state=TaskState.Review,
        now="等待审查",
        eta="-",
        block="无",
        output="",
        ac="",
        priority="normal",
        lane="standard",
        review_round=0,
        archived=False,
        template_id="",
        template_params={},
        target_dept="工部",
        prev_state="",
        state_version=5,
        flow_log=[],
        progress_log=[],
        consult_log=[],
        todos=[],
        scheduler={
            "enabled": True,
            "stallThresholdSec": 60,
            "maxRetry": 2,
            "retryCount": 2,
            "escalationLevel": 2,
            "autoRollback": True,
            "lastProgressAt": (now - timedelta(minutes=12)).isoformat(),
            "stallSince": (now - timedelta(minutes=11)).isoformat(),
            "snapshot": {
                "state": "Doing",
                "org": "工部",
                "now": "处理中",
                "savedAt": (now - timedelta(minutes=20)).isoformat(),
                "note": "before-review",
            },
        },
        created_at=now - timedelta(hours=2),
        updated_at=now - timedelta(minutes=12),
    )

    published = []

    async def fake_get_task(task_id, for_update=False):
        assert task_id == "JJC-TEST-ROLLBACK-1"
        return task

    async def fake_publish_state_event(task_obj, actor, from_state, reason, transition_kind="transition"):
        published.append((task_obj.id, from_state, reason, transition_kind))

    svc._get_task = fake_get_task
    svc._publish_state_event = fake_publish_state_event

    result = asyncio.run(
        svc._scheduler_rollback_internal(
            "JJC-TEST-ROLLBACK-1",
            reason="停滞 720 秒",
            actor=make_actor_context("sili", source="scheduler"),
            dispatch_trigger="sili-auto-rollback",
            automatic=True,
            stalled_sec=720,
        )
    )

    assert result["ok"] is True
    assert task.state.value == "Doing"
    assert task.now == "↩️ 司礼监调度自动回滚到稳定节点"
    assert task.scheduler["retryCount"] == 0
    assert task.scheduler["escalationLevel"] == 0
    assert task.scheduler["lastDispatchTrigger"] == "sili-auto-rollback"
    assert task.scheduler["lastDispatchStatus"] == "queued"
    assert task.scheduler["lastDispatchAgent"] == "gongbu"
    assert task.scheduler["lastDispatchKey"] == "auto:JJC-TEST-ROLLBACK-1:Doing:v6:gongbu"
    assert any("连续停滞，自动回滚：Review → Doing" in entry["remark"] for entry in task.flow_log)
    assert any("已入队派发：Doing → gongbu（sili-auto-rollback）" in entry["remark"] for entry in task.flow_log)
    assert published == [("JJC-TEST-ROLLBACK-1", "Review", "停滞 720 秒", "rollback")]


def test_v2_queue_metrics_reports_central_backlog_and_fast_lane(monkeypatch):
    _stub_backend_config(monkeypatch)
    task_service_module, _ = _import_task_service_with_stubs(monkeypatch)
    from edict.backend.app.task_contract import TaskState

    class FakeSession:
        async def commit(self):
            return None

    class FakeBus:
        async def publish(self, **kwargs):
            return "1-0"

    svc = task_service_module.TaskService(FakeSession(), FakeBus())
    now = datetime.now(timezone.utc)
    tasks = [
        task_service_module.Task(
            id="JJC-QUEUE-V2-001",
            title="门下审议",
            official="给事中",
            org="门下省",
            state=TaskState.Menxia,
            now="等待审议",
            eta="-",
            block="无",
            output="",
            ac="",
            priority="normal",
            lane="fast",
            review_round=0,
            archived=False,
            template_id="",
            template_params={},
            target_dept="",
            prev_state="",
            state_version=1,
            flow_log=[],
            progress_log=[],
            consult_log=[],
            todos=[],
            scheduler={},
            created_at=now - timedelta(hours=1),
            updated_at=now - timedelta(minutes=11),
        ),
        task_service_module.Task(
            id="JJC-QUEUE-V2-002",
            title="尚书派发",
            official="尚书令",
            org="尚书省",
            state=TaskState.Assigned,
            now="等待派发",
            eta="-",
            block="无",
            output="",
            ac="",
            priority="normal",
            lane="standard",
            review_round=0,
            archived=False,
            template_id="",
            template_params={},
            target_dept="工部",
            prev_state="",
            state_version=1,
            flow_log=[],
            progress_log=[],
            consult_log=[],
            todos=[],
            scheduler={},
            created_at=now - timedelta(hours=1),
            updated_at=now - timedelta(minutes=20),
        ),
    ]

    async def fake_list_tasks(limit=500):
        return tasks

    svc.list_tasks = fake_list_tasks

    metrics = asyncio.run(svc.get_queue_metrics())

    assert metrics["ok"] is True
    assert metrics["queues"]["menxia"]["waiting"] == 1
    assert metrics["queues"]["menxia"]["fastLane"] == 1
    assert metrics["queues"]["shangshu"]["waiting"] == 1
    assert metrics["queues"]["shangshu"]["oldestWaitSec"] >= 1200
