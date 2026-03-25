"""Backend monitoring API regression tests."""

from __future__ import annotations

import asyncio
import importlib
import pathlib
import sys
import types
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent


class _StubRouter:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def get(self, *args, **kwargs):
        def _decorator(fn):
            return fn

        return _decorator

    def post(self, *args, **kwargs):
        def _decorator(fn):
            return fn

        return _decorator


def _stub_dep(value=None, **_kwargs):
    return value


class _StubHTTPException(Exception):
    def __init__(self, status_code=500, detail=""):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class _StubPlainTextResponse:
    def __init__(self, content: str, media_type: str | None = None):
        self.body = content.encode("utf-8")
        self.media_type = media_type


class _StubRequest:
    def __init__(self):
        self.client = types.SimpleNamespace(host="127.0.0.1")


def _ensure_pkg(monkeypatch, name: str, path: pathlib.Path):
    module = types.ModuleType(name)
    module.__path__ = [str(path)]
    monkeypatch.setitem(sys.modules, name, module)


def _load_monitoring_modules(monkeypatch):
    fastapi_module = types.ModuleType("fastapi")
    fastapi_module.APIRouter = _StubRouter
    fastapi_module.Depends = _stub_dep
    fastapi_module.Header = _stub_dep
    fastapi_module.HTTPException = _StubHTTPException
    fastapi_module.Request = _StubRequest
    monkeypatch.setitem(sys.modules, "fastapi", fastapi_module)

    responses_module = types.ModuleType("fastapi.responses")
    responses_module.PlainTextResponse = _StubPlainTextResponse
    monkeypatch.setitem(sys.modules, "fastapi.responses", responses_module)

    sqlalchemy_module = types.ModuleType("sqlalchemy")
    sqlalchemy_module.text = lambda raw: raw
    monkeypatch.setitem(sys.modules, "sqlalchemy", sqlalchemy_module)

    sqlalchemy_ext_module = types.ModuleType("sqlalchemy.ext")
    sqlalchemy_async_module = types.ModuleType("sqlalchemy.ext.asyncio")
    sqlalchemy_async_module.AsyncSession = object
    monkeypatch.setitem(sys.modules, "sqlalchemy.ext", sqlalchemy_ext_module)
    monkeypatch.setitem(sys.modules, "sqlalchemy.ext.asyncio", sqlalchemy_async_module)

    db_module = types.ModuleType("edict.backend.app.db")

    async def _dummy_get_db():
        yield None

    db_module.get_db = _dummy_get_db
    monkeypatch.setitem(sys.modules, "edict.backend.app.db", db_module)

    config_module = types.ModuleType("edict.backend.app.config")
    config_module.get_settings = lambda: types.SimpleNamespace(heartbeat_interval_sec=30)
    monkeypatch.setitem(sys.modules, "edict.backend.app.config", config_module)

    workers_module = types.ModuleType("edict.backend.app.workers.orchestrator_worker")
    workers_module.WATCHED_TOPICS = [
        "task.created",
        "task.status",
        "task.completed",
        "task.escalated",
        "task.stalled",
    ]
    monkeypatch.setitem(sys.modules, "edict.backend.app.workers.orchestrator_worker", workers_module)

    _ensure_pkg(monkeypatch, "edict", ROOT / "edict")
    _ensure_pkg(monkeypatch, "edict.backend", ROOT / "edict" / "backend")
    _ensure_pkg(monkeypatch, "edict.backend.app", ROOT / "edict" / "backend" / "app")
    _ensure_pkg(monkeypatch, "edict.backend.app.api", ROOT / "edict" / "backend" / "app" / "api")

    sys.modules.pop("edict.backend.app.api.admin", None)
    sys.modules.pop("edict.backend.app.api.metrics", None)
    admin = importlib.import_module("edict.backend.app.api.admin")
    metrics = importlib.import_module("edict.backend.app.api.metrics")
    return admin, metrics


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeDB:
    def __init__(self, *, postgres_ok: bool = True):
        self.postgres_ok = postgres_ok

    async def execute(self, _stmt):
        if not self.postgres_ok:
            raise RuntimeError("postgres down")
        return _ScalarResult(1)


class _FakeRedis:
    def __init__(self, *, ping_ok: bool = True):
        self.ping_ok = ping_ok

    async def ping(self):
        return self.ping_ok


class _FakeBus:
    def __init__(
        self,
        *,
        worker_rows: list[dict] | None = None,
        group_rows: dict[tuple[str, str], dict] | None = None,
        pending_rows: list[dict] | None = None,
        pending_summary: dict[tuple[str, str], dict] | None = None,
    ):
        self.redis = _FakeRedis()
        self._worker_rows = worker_rows or []
        self._group_rows = group_rows or {}
        self._pending_rows = pending_rows or []
        self._pending_summary = pending_summary or {}

    async def list_worker_heartbeats(self):
        return list(self._worker_rows)

    async def stream_groups(self, topic: str):
        rows = []
        for (tp, _group), item in self._group_rows.items():
            if tp == topic:
                rows.append(dict(item))
        return rows

    async def pending_summary(self, topic: str, group: str):
        return dict(self._pending_summary.get((topic, group), {"count": 0, "min_id": "", "max_id": "", "consumers": []}))

    async def get_pending(self, _topic: str, _group: str, _count: int):
        return list(self._pending_rows)


def _healthy_worker_rows():
    now = datetime.now(timezone.utc).isoformat()
    return [
        {"worker": "orchestrator", "instance_id": "orch-a", "status": "running", "ts": now, "extra": {}},
        {"worker": "dispatcher", "instance_id": "disp-a", "status": "running", "ts": now, "extra": {}},
        {"worker": "scheduler", "instance_id": "sched-a", "status": "running", "ts": now, "extra": {"leader": True}},
    ]


def _healthy_stream_rows(admin):
    rows = {}
    summary = {}
    for item in admin._build_monitored_groups():
        key = (item["topic"], item["group"])
        rows[key] = {"name": item["group"], "pending": 1, "lag": 0, "consumers": 1, "last-delivered-id": "1-0"}
        summary[key] = {"count": 1, "min_id": "1-0", "max_id": "1-0", "consumers": [{"name": "c-1", "pending": 1}]}
    return rows, summary


def test_deep_health_reports_workers_and_stream_groups(monkeypatch):
    admin, _metrics = _load_monitoring_modules(monkeypatch)
    rows, summary = _healthy_stream_rows(admin)
    bus = _FakeBus(worker_rows=_healthy_worker_rows(), group_rows=rows, pending_summary=summary)

    async def _fake_get_event_bus():
        return bus

    monkeypatch.setattr(admin, "get_event_bus", _fake_get_event_bus)
    payload = asyncio.run(admin.deep_health(db=_FakeDB()))

    assert payload["status"] == "ok"
    assert payload["checks"]["postgres"]["ok"] is True
    assert payload["checks"]["redis"]["ok"] is True
    assert payload["checks"]["workers"]["ok"] is True
    assert payload["checks"]["streams"]["ok"] is True
    assert payload["checks"]["streams"]["totals"]["pending"] == len(admin._build_monitored_groups())


def test_deep_health_marks_stale_worker_and_high_lag_as_degraded(monkeypatch):
    admin, _metrics = _load_monitoring_modules(monkeypatch)
    rows, summary = _healthy_stream_rows(admin)
    stale_ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    workers = _healthy_worker_rows()
    workers[0]["ts"] = stale_ts

    first_key = next(iter(rows))
    rows[first_key]["lag"] = 999
    bus = _FakeBus(worker_rows=workers, group_rows=rows, pending_summary=summary)

    async def _fake_get_event_bus():
        return bus

    monkeypatch.setattr(admin, "get_event_bus", _fake_get_event_bus)
    payload = asyncio.run(admin.deep_health(db=_FakeDB()))

    assert payload["status"] == "degraded"
    assert payload["checks"]["workers"]["ok"] is False
    assert payload["checks"]["streams"]["ok"] is False
    assert payload["checks"]["streams"]["totals"]["unhealthyGroups"] >= 1


def test_pending_events_includes_summary(monkeypatch):
    admin, _metrics = _load_monitoring_modules(monkeypatch)
    bus = _FakeBus(
        pending_rows=[
            {"message_id": "1-0", "consumer": "disp-a", "time_since_delivered": 2000, "times_delivered": 1},
            {"message_id": "2-0", "consumer": "disp-a", "time_since_delivered": 4000, "times_delivered": 2},
        ],
        pending_summary={
            ("task.dispatch", "dispatcher"): {
                "count": 2,
                "min_id": "1-0",
                "max_id": "2-0",
                "consumers": [{"name": "disp-a", "pending": 2}],
            }
        },
    )

    async def _fake_get_event_bus():
        return bus

    monkeypatch.setattr(admin, "get_event_bus", _fake_get_event_bus)
    payload = asyncio.run(admin.pending_events(topic="task.dispatch", group="dispatcher", count=10))

    assert payload["summary"]["count"] == 2
    assert len(payload["pending"]) == 2


def test_prometheus_metrics_exposes_core_gauges(monkeypatch):
    admin, metrics = _load_monitoring_modules(monkeypatch)
    rows, summary = _healthy_stream_rows(admin)
    bus = _FakeBus(worker_rows=_healthy_worker_rows(), group_rows=rows, pending_summary=summary)

    async def _fake_get_event_bus():
        return bus

    monkeypatch.setattr(metrics, "get_event_bus", _fake_get_event_bus)
    response = asyncio.run(metrics.metrics_prometheus(db=_FakeDB()))
    text = response.body.decode("utf-8")

    assert "edict_health_postgres_up 1" in text
    assert "edict_health_redis_up 1" in text
    assert 'edict_worker_up{instance="orch-a",status="running",worker="orchestrator"} 1' in text
    assert "edict_stream_group_pending" in text
    assert "edict_monitoring_up 1" in text
