"""Regression tests for EventBus monitoring helpers."""

from __future__ import annotations

import asyncio
import importlib
import sys
import types


def _load_event_bus_module(monkeypatch):
    module = types.ModuleType("edict.backend.app.config")
    module.get_settings = lambda: types.SimpleNamespace(redis_url="redis://unused")
    monkeypatch.setitem(sys.modules, "edict.backend.app.config", module)
    sys.modules.pop("edict.backend.app.services.event_bus", None)
    return importlib.import_module("edict.backend.app.services.event_bus")


class _FakeRedis:
    def __init__(self):
        self.values: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}
        self.expire_calls: list[tuple[str, int]] = []
        self.removed: list[tuple[str, tuple[str, ...]]] = []
        self.pending_response = [3, "1-0", "3-0", [["disp-1", 2], ["disp-2", 1]]]

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def sadd(self, key, *members):
        bucket = self.sets.setdefault(key, set())
        for member in members:
            bucket.add(str(member))
        return len(members)

    async def expire(self, key, ttl):
        self.expire_calls.append((key, ttl))
        return True

    async def smembers(self, key):
        return self.sets.get(key, set())

    async def mget(self, keys):
        return [self.values.get(str(key)) for key in keys]

    async def srem(self, key, *members):
        bucket = self.sets.setdefault(key, set())
        for member in members:
            bucket.discard(str(member))
        self.removed.append((key, tuple(str(member) for member in members)))
        return len(members)

    async def execute_command(self, *_args):
        return self.pending_response


def test_event_bus_worker_heartbeat_registry_round_trip(monkeypatch):
    event_bus = _load_event_bus_module(monkeypatch)
    bus = event_bus.EventBus(redis_url="redis://unused")
    fake = _FakeRedis()
    bus._redis = fake

    payload = asyncio.run(
        bus.report_worker_heartbeat(
            "scheduler",
            "sched-1",
            status="running",
            ttl_sec=90,
            extra={"leader": True},
        )
    )
    registry = fake.sets[event_bus.WORKER_HEARTBEAT_REGISTRY_KEY]
    assert payload["worker"] == "scheduler"
    assert any(item.endswith("scheduler:sched-1") for item in registry)

    # Simulate a stale registry member to ensure cleanup happens.
    stale_key = "edict:worker:heartbeat:dispatcher:disp-stale"
    fake.sets[event_bus.WORKER_HEARTBEAT_REGISTRY_KEY].add(stale_key)

    rows = asyncio.run(bus.list_worker_heartbeats())

    assert len(rows) == 1
    assert rows[0]["worker"] == "scheduler"
    assert rows[0]["status"] == "running"
    assert rows[0]["extra"]["leader"] is True
    assert fake.removed == [(event_bus.WORKER_HEARTBEAT_REGISTRY_KEY, (stale_key,))]


def test_event_bus_pending_summary_parses_xpending_shape(monkeypatch):
    event_bus = _load_event_bus_module(monkeypatch)
    bus = event_bus.EventBus(redis_url="redis://unused")
    fake = _FakeRedis()
    bus._redis = fake

    summary = asyncio.run(bus.pending_summary("task.dispatch", "dispatcher"))

    assert summary == {
        "count": 3,
        "min_id": "1-0",
        "max_id": "3-0",
        "consumers": [
            {"name": "disp-1", "pending": 2},
            {"name": "disp-2", "pending": 1},
        ],
    }


def test_event_bus_list_worker_heartbeats_skips_invalid_json(monkeypatch):
    event_bus = _load_event_bus_module(monkeypatch)
    bus = event_bus.EventBus(redis_url="redis://unused")
    fake = _FakeRedis()
    bus._redis = fake
    key = "edict:worker:heartbeat:orchestrator:orch-1"
    fake.sets[event_bus.WORKER_HEARTBEAT_REGISTRY_KEY] = {key}
    fake.values[key] = "{bad json"

    rows = asyncio.run(bus.list_worker_heartbeats())

    assert rows == []
    assert fake.removed == [(event_bus.WORKER_HEARTBEAT_REGISTRY_KEY, (key,))]
