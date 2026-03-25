"""tests for shared task contract p1 extensions"""

from datetime import datetime, timezone

from edict.backend.app.task_contract import (
    build_audit_entry,
    authorize_consultation,
    central_queue_owner,
    ensure_task_shape,
    make_actor_context,
    queue_sla_seconds,
    resolve_state_org,
)


def test_consultation_authorization_preserves_owner_boundary():
    task = {
        "id": "JJC-TEST-CONSULT-1",
        "state": "Assigned",
        "org": "尚书省",
        "targetDept": "工部",
        "lane": "fast",
    }
    allowed, reason = authorize_consultation(make_actor_context("shangshu", source="test"), task, "gongbu")
    assert allowed is True
    denied, reason = authorize_consultation(make_actor_context("gongbu", source="test"), task, "menxia")
    assert denied is False
    assert "负责人" in reason or "不允许" in reason


def test_queue_sla_uses_fast_lane_thresholds():
    menxia_fast = {"state": "Menxia", "lane": "fast"}
    menxia_standard = {"state": "Menxia", "lane": "standard"}
    assert queue_sla_seconds(menxia_fast) < queue_sla_seconds(menxia_standard)
    assert central_queue_owner("Assigned") == "shangshu"


def test_resolve_state_org_aligns_non_execution_and_execution_states():
    task = {"state": "Sili", "org": "司礼监", "targetDept": "工部"}
    assert resolve_state_org(task, "Zhongshu") == "中书省"
    assert resolve_state_org(task, "Review") == "尚书省"
    assert resolve_state_org(task, "Doing") == "工部"


def test_ensure_task_shape_accepts_storage_aliases_from_pg_model():
    task = {
        "id": "JJC-TEST-ALIAS-1",
        "title": "统一字段映射",
        "state": "Doing",
        "org": "工部",
        "consult_log": [{"note": "保留旧字段"}],
        "scheduler": {"retryCount": 2},
        "prev_state": "Assigned",
        "state_version": 7,
        "template_id": "tmpl-plan",
        "template_params": {"mode": "shadow"},
        "target_dept": "工部",
        "created_at": datetime(2026, 3, 24, 0, 0, tzinfo=timezone.utc),
        "updated_at": "2026-03-24T01:23:45+00:00",
    }

    normalized = ensure_task_shape(task)

    assert normalized["consultLog"] == [{"note": "保留旧字段"}]
    assert normalized["_scheduler"]["retryCount"] == 2
    assert normalized["_prev_state"] == "Assigned"
    assert normalized["_stateVersion"] == 7
    assert normalized["templateId"] == "tmpl-plan"
    assert normalized["templateParams"] == {"mode": "shadow"}
    assert normalized["targetDept"] == "工部"
    assert normalized["createdAt"] == "2026-03-24T00:00:00+00:00"
    assert normalized["updatedAt"] == "2026-03-24T01:23:45+00:00"


def test_build_audit_entry_keeps_full_payload_for_durable_storage():
    entry = build_audit_entry(
        task_id="JJC-TEST-AUDIT-1",
        action="task.todos",
        actor=make_actor_context("shangshu", source="test"),
        allowed=True,
        payload={"todo_count": 3, "source_of_truth": "tasks.todos"},
    )

    assert entry["payload"] == {"todo_count": 3, "source_of_truth": "tasks.todos"}
    assert entry["payload_hash"]
    assert "tasks.todos" in entry["payload_summary"]
