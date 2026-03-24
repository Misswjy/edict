"""backend dispatch/orchestrator regression tests"""

import asyncio
import sys
import types
from types import SimpleNamespace


def _stub_backend_config(monkeypatch):
    module = types.ModuleType("edict.backend.app.config")
    module.get_settings = lambda: SimpleNamespace(
        redis_url="redis://unused",
        port=8000,
        dispatch_timeout_sec=300,
        openclaw_project_dir=None,
    )
    monkeypatch.setitem(sys.modules, "edict.backend.app.config", module)


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
