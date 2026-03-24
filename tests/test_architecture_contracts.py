"""Architecture contract tests for shared state, permissions, dispatch, and API."""

import copy
import json
import pathlib
import sys
import threading
from http.client import HTTPConnection
from http.server import HTTPServer

import pytest

from edict.backend.app.task_contract import (
    TERMINAL_STATES,
    authorize_agent_wake,
    authorize_consultation,
    authorize_review,
    authorize_scheduler,
    make_actor_context,
    next_manual_transition,
    resolve_state_org,
    review_transition,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "dashboard"))
sys.path.insert(0, str(ROOT / "scripts"))


def _configure_server(tmp_path, monkeypatch, *, patch_wake=True, patch_dispatch=True):
    import server as srv

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "tasks_source.json").write_text("[]")
    (data_dir / "live_status.json").write_text(json.dumps({"tasks": [], "syncStatus": {"ok": True}}))
    (data_dir / "agent_config.json").write_text(json.dumps({"agents": []}))

    monkeypatch.setattr(srv, "DATA", data_dir, raising=False)
    monkeypatch.setattr(srv, "TASKS_PATH", data_dir / "tasks_source.json", raising=False)
    monkeypatch.setattr(srv, "TASK_AUDIT_PATH", data_dir / "task_audit_log.json", raising=False)
    monkeypatch.setattr(srv, "_trigger_refresh_async", lambda: None, raising=False)
    if patch_dispatch:
        monkeypatch.setattr(srv, "dispatch_for_state", lambda *args, **kwargs: None, raising=False)
    if patch_wake:
        monkeypatch.setattr(srv, "wake_agent", lambda *args, **kwargs: {"ok": True, "message": "noop"}, raising=False)
    return srv, data_dir


def _read_tasks(data_dir: pathlib.Path):
    return json.loads((data_dir / "tasks_source.json").read_text())


def _read_audit(data_dir: pathlib.Path):
    audit_path = data_dir / "task_audit_log.json"
    if not audit_path.exists():
        return []
    return json.loads(audit_path.read_text())


def _json_request(port: int, method: str, path: str, payload=None):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    body = None
    headers = {}
    if payload is not None:
        body = json.dumps(payload)
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    data = json.loads(resp.read() or b"{}")
    conn.close()
    return resp.status, data


def test_manual_advance_matches_shared_state_machine(tmp_path, monkeypatch):
    srv, _ = _configure_server(tmp_path, monkeypatch)
    actor = make_actor_context("emperor", source="test")

    for state in ("Pending", "Sili", "Zhongshu", "Menxia", "Assigned", "Next", "Doing", "Review"):
        task = {
            "id": f"JJC-CONTRACT-{state}",
            "title": f"{state} 契约测试",
            "state": state,
            "targetDept": "工部",
            "lane": "standard",
            "flow_log": [],
            "updatedAt": "2026-03-24T00:00:00+00:00",
        }
        org = resolve_state_org(task, state)
        if org:
            task["org"] = org
        srv.ensure_task_shape(task)

        ok, transition = next_manual_transition(copy.deepcopy(task))
        assert ok is True

        result = srv._handle_advance_state_update(task, "", actor)
        assert result["allowed"] is True
        assert task["state"] == transition["next_state"]
        assert task["org"] == transition["to_org"]
        if task["state"] not in TERMINAL_STATES:
            assert result["dispatch_state"] == task["state"]


@pytest.mark.parametrize(
    ("state", "action", "actor_id", "expected_state", "expected_org", "expected_round"),
    [
        ("Menxia", "approve", "menxia", "Assigned", "尚书省", 0),
        ("Menxia", "reject", "menxia", "Zhongshu", "中书省", 1),
        ("Review", "approve", "emperor", "Done", "皇上", 0),
        ("Review", "reject", "emperor", "Doing", "工部", 0),
    ],
)
def test_review_transition_matches_shared_state_machine(
    tmp_path, monkeypatch, state, action, actor_id, expected_state, expected_org, expected_round
):
    srv, _ = _configure_server(tmp_path, monkeypatch)
    actor = make_actor_context(actor_id, source="test")
    task = {
        "id": f"JJC-REVIEW-{state}-{action}",
        "title": "审批契约测试",
        "state": state,
        "targetDept": "工部",
        "lane": "standard",
        "flow_log": [],
        "updatedAt": "2026-03-24T00:00:00+00:00",
    }
    org = resolve_state_org(task, state)
    if org:
        task["org"] = org
    srv.ensure_task_shape(task)

    ok, transition = review_transition(copy.deepcopy(task), action, "契约测试")
    assert ok is True

    result = srv._handle_review_action_update(task, action, "契约测试", actor)
    assert result["allowed"] is True
    assert task["state"] == transition["new_state"] == expected_state
    assert task["org"] == transition["to_org"] == expected_org
    assert int(task.get("review_round") or 0) == expected_round


def test_permission_matrix_contract_matches_legacy_handlers(tmp_path, monkeypatch):
    srv, data_dir = _configure_server(tmp_path, monkeypatch)
    tasks = [
        {
            "id": "JJC-PERM-REVIEW",
            "title": "审批权限",
            "state": "Review",
            "org": "尚书省",
            "targetDept": "工部",
            "flow_log": [],
            "updatedAt": "2026-03-24T00:00:00+00:00",
        },
        {
            "id": "JJC-PERM-CONSULT",
            "title": "咨询权限",
            "state": "Assigned",
            "org": "尚书省",
            "targetDept": "工部",
            "flow_log": [],
            "updatedAt": "2026-03-24T00:00:00+00:00",
        },
        {
            "id": "JJC-PERM-SCHED",
            "title": "调度权限",
            "state": "Doing",
            "org": "工部",
            "targetDept": "工部",
            "flow_log": [],
            "updatedAt": "2026-03-24T00:00:00+00:00",
        },
    ]
    (data_dir / "tasks_source.json").write_text(json.dumps(tasks, ensure_ascii=False))

    review_actor = make_actor_context("gongbu", source="test")
    allowed, reason = authorize_review(review_actor, tasks[0], "approve")
    assert allowed is False
    review_result = srv.handle_review_action("JJC-PERM-REVIEW", "approve", "越权审批", actor=review_actor)
    assert review_result["ok"] is False
    assert reason in review_result["error"]

    consult_actor = make_actor_context("shangshu", source="test")
    allowed, reason = authorize_consultation(consult_actor, tasks[1], "gongbu")
    assert allowed is True
    consult_result = srv.handle_task_consult("JJC-PERM-CONSULT", "gongbu", "请评估复杂度", actor=consult_actor)
    assert consult_result["ok"] is True

    denied_consult_actor = make_actor_context("gongbu", source="test")
    allowed, reason = authorize_consultation(denied_consult_actor, tasks[1], "menxia")
    assert allowed is False
    denied_consult_result = srv.handle_task_consult("JJC-PERM-CONSULT", "menxia", "越权咨询", actor=denied_consult_actor)
    assert denied_consult_result["ok"] is False
    assert reason in denied_consult_result["error"]

    scheduler_actor = make_actor_context("gongbu", source="test")
    allowed, reason = authorize_scheduler(scheduler_actor, "retry")
    assert allowed is False
    retry_result = srv.handle_scheduler_retry("JJC-PERM-SCHED", "越权重试", actor=scheduler_actor)
    assert retry_result["ok"] is False
    assert reason in retry_result["error"]

    audit_entries = _read_audit(data_dir)
    denied_actions = {(entry["action"], entry["allowed"]) for entry in audit_entries}
    assert ("review.approve", False) in denied_actions
    assert ("task.consult", False) in denied_actions
    assert ("scheduler.retry", False) in denied_actions


def test_agent_wake_permission_contract_matches_legacy_handler(tmp_path, monkeypatch):
    srv, _ = _configure_server(tmp_path, monkeypatch, patch_wake=False)
    actor = make_actor_context("gongbu", source="test")
    allowed, reason = authorize_agent_wake(actor, "menxia")
    assert allowed is False

    result = srv.wake_agent("menxia", actor=actor, task_id="JJC-PERM-WAKE")
    assert result["ok"] is False
    assert reason in result["error"]


def test_legacy_dispatch_is_idempotent_for_same_state_version(tmp_path, monkeypatch):
    srv, data_dir = _configure_server(tmp_path, monkeypatch, patch_wake=False, patch_dispatch=False)
    task = {
        "id": "JJC-DISPATCH-001",
        "title": "幂等派发契约",
        "state": "Doing",
        "org": "工部",
        "targetDept": "工部",
        "flow_log": [],
        "updatedAt": "2026-03-24T00:00:00+00:00",
        "_stateVersion": 4,
    }
    (data_dir / "tasks_source.json").write_text(json.dumps([task], ensure_ascii=False))

    starts = []

    class DummyThread:
        def __init__(self, target=None, daemon=None):
            self.target = target

        def start(self):
            starts.append(self.target)

    monkeypatch.setattr(srv.threading, "Thread", DummyThread, raising=False)

    actor = make_actor_context("system", source="test")
    srv.dispatch_for_state(task["id"], copy.deepcopy(task), "Doing", trigger="contract-test", actor=actor)

    [updated] = _read_tasks(data_dir)
    first_key = updated["_scheduler"]["lastDispatchKey"]
    assert updated["_scheduler"]["lastDispatchStatus"] == "queued"

    srv.dispatch_for_state(updated["id"], copy.deepcopy(updated), "Doing", trigger="contract-test", actor=actor)

    [updated_again] = _read_tasks(data_dir)
    assert updated_again["_scheduler"]["lastDispatchKey"] == first_key
    assert updated_again["_scheduler"]["lastDispatchStatus"] == "queued"
    assert len(starts) == 1

    dispatch_audits = [entry for entry in _read_audit(data_dir) if entry["action"] == "task.dispatch"]
    assert len(dispatch_audits) == 1


def test_http_control_plane_contract_matches_frontend_expectations(tmp_path, monkeypatch):
    srv, _ = _configure_server(tmp_path, monkeypatch)
    httpd = HTTPServer(("127.0.0.1", 0), srv.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    try:
        port = httpd.server_port
        status, created = _json_request(
            port,
            "POST",
            "/api/create-task",
            {"title": "架构契约 HTTP 创建任务", "actor": "emperor", "source": "test"},
        )
        assert status == 200
        assert created["ok"] is True
        assert created["taskId"].startswith("JJC-")

        task_id = created["taskId"]
        status, scheduler = _json_request(port, "GET", f"/api/scheduler-state/{task_id}")
        assert status == 200
        assert scheduler["ok"] is True
        assert scheduler["taskId"] == task_id
        assert {"state", "org", "scheduler", "stalledSec"} <= set(scheduler)

        status, activity = _json_request(port, "GET", f"/api/task-activity/{task_id}")
        assert status == 200
        assert activity["ok"] is True
        assert activity["taskId"] == task_id
        assert {"taskMeta", "activity"} <= set(activity)

        status, queues = _json_request(port, "GET", "/api/queue-metrics")
        assert status == 200
        assert queues["ok"] is True
        assert {"menxia", "shangshu"} <= set(queues["queues"])
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=1)
