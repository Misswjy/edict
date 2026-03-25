"""Regression tests for the v2 scheduler worker."""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
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
        scheduler_scan_interval_seconds=60,
        stall_threshold_sec=180,
    )
    monkeypatch.setitem(sys.modules, "edict.backend.app.config", module)


def _import_scheduler_worker(monkeypatch):
    _stub_backend_config(monkeypatch)
    sys.modules.pop("edict.backend.app.workers.scheduler_worker", None)
    return importlib.import_module("edict.backend.app.workers.scheduler_worker")


class _FakeRedis:
    def __init__(self):
        self.store = {}
        self.expire_calls = []
        self.deleted = []

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    async def expire(self, key, ttl):
        self.expire_calls.append((key, ttl))
        return True

    async def delete(self, key):
        self.deleted.append(key)
        self.store.pop(key, None)
        return 1


class _FakeBus:
    def __init__(self):
        self.redis = _FakeRedis()
        self.closed = False

    async def close(self):
        self.closed = True


def test_scheduler_worker_acquires_and_renews_leader_lock(monkeypatch):
    module = _import_scheduler_worker(monkeypatch)
    worker = module.SchedulerWorker(scan_interval_sec=15, threshold_sec=180, lock_ttl_sec=45)
    worker.bus = _FakeBus()

    acquired = asyncio.run(worker._acquire_or_renew_leader_lock())
    assert acquired is True
    assert worker.bus.redis.store[module.LOCK_KEY] == worker.instance_id

    renewed = asyncio.run(worker._acquire_or_renew_leader_lock())
    assert renewed is True
    assert worker.bus.redis.expire_calls == [(module.LOCK_KEY, 45)]


def test_scheduler_worker_skips_scan_when_lock_owned_by_other_instance(monkeypatch):
    module = _import_scheduler_worker(monkeypatch)
    worker = module.SchedulerWorker(scan_interval_sec=15, threshold_sec=180, lock_ttl_sec=45)
    worker.bus = _FakeBus()
    worker.bus.redis.store[module.LOCK_KEY] = "scheduler-other"

    called = {"scan": 0}

    async def fake_scan_tasks():
        called["scan"] += 1
        return {"count": 0, "thresholdSec": 180}

    async def fake_sleep(_seconds):
        return None

    worker._scan_tasks = fake_scan_tasks
    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)

    asyncio.run(worker._run_cycle())

    assert called["scan"] == 0


def test_scheduler_worker_runs_scan_and_releases_lock_on_stop(monkeypatch):
    module = _import_scheduler_worker(monkeypatch)
    worker = module.SchedulerWorker(scan_interval_sec=15, threshold_sec=180, lock_ttl_sec=45)
    worker.bus = _FakeBus()

    called = {"scan": 0}

    async def fake_scan_tasks():
        called["scan"] += 1
        return {"count": 2, "thresholdSec": 180}

    async def fake_sleep(_seconds):
        return None

    worker._scan_tasks = fake_scan_tasks
    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)

    asyncio.run(worker._run_cycle())
    assert called["scan"] == 1
    assert worker.bus.redis.store[module.LOCK_KEY] == worker.instance_id

    asyncio.run(worker.stop())
    assert worker.bus.closed is True
    assert worker.bus.redis.deleted == [module.LOCK_KEY]
