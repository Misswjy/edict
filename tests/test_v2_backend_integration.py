"""Local v2 backend integration tests with real Postgres + Redis.

These tests intentionally stay out of the default fast unit loop because they
launch real dependencies and a live backend process:

    EDICT_RUN_DOCKER_INTEGRATION=1 python3 -m pytest tests/test_v2_backend_integration.py -q

Stack under test:
- Postgres: local server on `127.0.0.1:5432`
- Redis: ephemeral Docker container on a random host port
- Backend: local venv process running `uvicorn app.main:app`
"""

from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
BACKEND_ROOT = ROOT / "edict" / "backend"
ALEMBIC_INI = ROOT / "edict" / "alembic.ini"
VENV_PYTHON = pathlib.Path("/tmp/edict-backend-venv/bin/python")


def _integration_requested() -> bool:
    return os.environ.get("EDICT_RUN_DOCKER_INTEGRATION", "").strip() == "1"


def _docker_available() -> bool:
    probe = subprocess.run(["docker", "info"], capture_output=True, text=True, check=False)
    return probe.returncode == 0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def _http_json(method: str, url: str, payload: dict | None = None) -> tuple[int, dict]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read().decode("utf-8")
            return response.status, json.loads(body or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        return exc.code, json.loads(body or "{}")


def _wait_for_backend(base_url: str, *, timeout_sec: int = 90, backend_log: pathlib.Path | None = None) -> None:
    deadline = time.time() + timeout_sec
    last_error = ""
    while time.time() < deadline:
        try:
            status, payload = _http_json("GET", f"{base_url}/health")
            if status == 200 and payload.get("status") == "ok":
                return
            last_error = f"unexpected health payload: {payload}"
        except Exception as exc:  # pragma: no cover - polling only
            last_error = str(exc)
        time.sleep(1)

    log_tail = ""
    if backend_log and backend_log.exists():
        text = backend_log.read_text(encoding="utf-8", errors="ignore")
        log_tail = "\n\nbackend log tail:\n" + "\n".join(text.splitlines()[-50:])
    raise AssertionError(f"backend did not become healthy within {timeout_sec}s: {last_error}{log_tail}")


def _docker_port(container_id: str, container_port: str) -> int:
    result = subprocess.run(
        ["docker", "port", container_id, container_port],
        capture_output=True,
        text=True,
        check=True,
    )
    raw = result.stdout.strip().splitlines()[0]
    return int(raw.rsplit(":", 1)[1])


def _run_checked(cmd: list[str], *, env: dict[str, str] | None = None, cwd: pathlib.Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        env=env,
        cwd=str(cwd or ROOT),
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture(scope="module")
def v2_stack(tmp_path_factory: pytest.TempPathFactory):
    if not _integration_requested():
        pytest.skip("set EDICT_RUN_DOCKER_INTEGRATION=1 to run real v2 integration tests")
    if not VENV_PYTHON.exists():
        pytest.skip(f"backend venv not found: {VENV_PYTHON}")
    if not _docker_available():
        pytest.skip("Docker daemon is not available")

    temp_dir = tmp_path_factory.mktemp("v2-backend-integration")
    db_name = f"edict_integration_{uuid.uuid4().hex[:8]}"
    api_port = _free_port()
    redis_container = ""
    backend_proc: subprocess.Popen[str] | None = None
    backend_log_path = temp_dir / "backend.log"
    backend_log_file = backend_log_path.open("w", encoding="utf-8")

    try:
        _run_checked(["createdb", "-h", "127.0.0.1", "-p", "5432", db_name])

        redis_run = _run_checked(["docker", "run", "--rm", "-d", "-p", "0:6379", "redis:7-alpine"])
        redis_container = redis_run.stdout.strip()
        redis_port = _docker_port(redis_container, "6379/tcp")

        database_url = f"postgresql+asyncpg://xingzhan@127.0.0.1:5432/{db_name}"
        env = os.environ.copy()
        env.update(
            {
                "DATABASE_URL_OVERRIDE": database_url,
                "REDIS_URL": f"redis://127.0.0.1:{redis_port}/0",
                "PYTHONPATH": str(BACKEND_ROOT),
            }
        )
        alembic_ini = temp_dir / "alembic.integration.ini"
        alembic_ini.write_text(
            ALEMBIC_INI.read_text(encoding="utf-8").replace(
                "sqlalchemy.url = postgresql+asyncpg://edict:edict_dev_2024@localhost:5432/edict",
                f"sqlalchemy.url = {database_url}",
            ),
            encoding="utf-8",
        )

        _run_checked(
            [str(VENV_PYTHON), "-m", "alembic", "-c", str(alembic_ini), "upgrade", "head"],
            env=env,
            cwd=ROOT / "edict",
        )

        backend_proc = subprocess.Popen(
            [
                str(VENV_PYTHON),
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(api_port),
            ],
            cwd=str(BACKEND_ROOT),
            env=env,
            stdout=backend_log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )

        base_url = f"http://127.0.0.1:{api_port}"
        _wait_for_backend(base_url, backend_log=backend_log_path)

        yield {
            "base_url": base_url,
            "database_url": database_url,
            "redis_container": redis_container,
            "redis_port": redis_port,
            "db_name": db_name,
        }
    finally:
        if backend_proc is not None and backend_proc.poll() is None:
            backend_proc.terminate()
            try:
                backend_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - cleanup only
                backend_proc.kill()
                backend_proc.wait(timeout=10)
        backend_log_file.close()
        if redis_container:
            subprocess.run(["docker", "rm", "-f", redis_container], capture_output=True, text=True, check=False)
        subprocess.run(["dropdb", "-h", "127.0.0.1", "-p", "5432", "--if-exists", db_name], capture_output=True, text=True, check=False)


def test_backend_stack_persists_task_and_event_stream(v2_stack):
    base_url = v2_stack["base_url"]
    create_payload = {
        "title": f"集成测试任务-{uuid.uuid4().hex[:8]}",
        "official": "工部尚书",
        "targetDept": "工部",
        "initialState": "Sili",
        "actor": "system",
        "source": "integration-test",
    }

    status, created = _http_json("POST", f"{base_url}/api/tasks", create_payload)
    assert status == 201, created
    task_id = created["taskId"]

    status, task = _http_json("GET", f"{base_url}/api/tasks/{urllib.parse.quote(task_id)}")
    assert status == 200, task
    assert task["id"] == task_id
    assert task["title"] == create_payload["title"]
    assert task["state"] == "Sili"
    assert task["targetDept"] == "工部"

    status, events = _http_json("GET", f"{base_url}/api/events?trace_id={urllib.parse.quote(task_id)}")
    assert status == 200, events
    assert events["count"] >= 1
    task_created = next(event for event in events["events"] if event["topic"] == "task.created")
    assert task_created["trace_id"] == task_id
    assert task_created["stream_entry_id"]
    assert task_created["payload"]["title"] == create_payload["title"]

    status, stream = _http_json("GET", f"{base_url}/api/events/stream-info?topic=task.created")
    assert status == 200, stream
    assert int(stream["info"].get("length", 0) or 0) >= 1


def test_backend_stack_live_status_queue_metrics_and_health(v2_stack):
    base_url = v2_stack["base_url"]
    create_payload = {
        "title": f"队列指标任务-{uuid.uuid4().hex[:8]}",
        "official": "尚书令",
        "targetDept": "工部",
        "initialState": "Assigned",
        "actor": "system",
        "source": "integration-test",
    }

    status, created = _http_json("POST", f"{base_url}/api/tasks", create_payload)
    assert status == 201, created
    task_id = created["taskId"]

    status, live_status = _http_json("GET", f"{base_url}/api/live-status")
    assert status == 200, live_status
    assert live_status["syncStatus"]["engine"] == "edict-backend"
    assert live_status["syncStatus"]["controlPlane"] == "fastapi"
    assert any(task["id"] == task_id for task in live_status["tasks"])

    status, queue_metrics = _http_json("GET", f"{base_url}/api/queue-metrics")
    assert status == 200, queue_metrics
    shangshu = queue_metrics["queues"]["shangshu"]
    assert shangshu["waiting"] >= 1
    assert any(item["taskId"] == task_id for item in shangshu["tasks"])

    status, health = _http_json("GET", f"{base_url}/api/admin/health/deep")
    assert status == 200, health
    assert health["checks"]["postgres"]["ok"] is True
    assert health["checks"]["redis"]["ok"] is True
    assert health["status"] in {"ok", "degraded"}
