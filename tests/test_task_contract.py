"""tests for shared task contract p1 extensions"""

import hashlib
import hmac
from datetime import datetime, timezone

from edict.backend.app.event_contract import (
    EVENT_TOPIC_SCHEMAS,
    TOPIC_AGENT_TODO_UPDATE,
    TOPIC_TASK_COMPLETED,
    TOPIC_TASK_CREATED,
    TOPIC_TASK_DISPATCH,
    TOPIC_TASK_ESCALATED,
    TOPIC_TASK_STALLED,
    TOPIC_TASK_STATUS,
    build_auto_dispatch_key,
    build_consult_dispatch_key,
    build_manual_dispatch_key,
    build_task_created_dedupe_key,
    build_task_escalated_dedupe_key,
    build_task_stalled_dedupe_key,
    build_task_state_dedupe_key,
    build_task_todos_dedupe_key,
)
from edict.backend.app.task_contract import (
    authorize_agent_wake,
    authorize_review,
    build_audit_entry,
    authorize_consultation,
    central_queue_owner,
    ensure_task_shape,
    make_actor_context,
    next_manual_transition,
    queue_sla_seconds,
    review_transition,
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


def test_manual_transition_contract_keeps_expected_state_and_owner():
    task = {
        "id": "JJC-TEST-MANUAL-1",
        "state": "Assigned",
        "org": "尚书省",
        "targetDept": "工部",
        "lane": "standard",
    }

    allowed, transition = next_manual_transition(task)

    assert allowed is True
    assert transition["next_state"] == "Doing"
    assert transition["to_org"] == "工部"


def test_review_transition_contract_keeps_menxia_and_review_paths():
    menxia_task = {
        "id": "JJC-TEST-REVIEW-1",
        "state": "Menxia",
        "org": "门下省",
        "targetDept": "工部",
        "lane": "standard",
        "review_round": 0,
    }
    allowed, approved = review_transition(menxia_task, "approve", "准奏")
    assert allowed is True
    assert approved["new_state"] == "Assigned"
    assert approved["to_org"] == "尚书省"

    review_task = {
        "id": "JJC-TEST-REVIEW-2",
        "state": "Review",
        "org": "尚书省",
        "targetDept": "工部",
        "lane": "standard",
        "review_round": 0,
    }
    allowed, rejected = review_transition(review_task, "reject", "退回工部")
    assert allowed is True
    assert rejected["new_state"] == "Doing"
    assert rejected["to_org"] == "工部"


def test_queue_sla_uses_fast_lane_thresholds():
    menxia_fast = {"state": "Menxia", "lane": "fast"}
    menxia_standard = {"state": "Menxia", "lane": "standard"}
    assert queue_sla_seconds(menxia_fast) < queue_sla_seconds(menxia_standard)
    assert central_queue_owner("Assigned") == "shangshu"


def test_review_and_agent_wake_authorization_boundaries_hold():
    review_task = {
        "id": "JJC-TEST-AUTH-1",
        "state": "Review",
        "org": "尚书省",
        "targetDept": "工部",
        "lane": "standard",
    }
    allowed, _ = authorize_review(make_actor_context("emperor", source="test"), review_task, "approve")
    denied, reason = authorize_review(make_actor_context("gongbu", source="test"), review_task, "approve")
    assert allowed is True
    assert denied is False
    assert "不允许" in reason or "审批" in reason

    wake_allowed, _ = authorize_agent_wake(make_actor_context("sili", source="test"), "menxia")
    wake_denied, reason = authorize_agent_wake(make_actor_context("gongbu", source="test"), "menxia")
    assert wake_allowed is True
    assert wake_denied is False
    assert "不允许" in reason or "唤醒" in reason


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


def test_actor_context_preserves_request_metadata_and_signature(monkeypatch):
    monkeypatch.setenv("EDICT_ACTOR_SHARED_SECRET", "shared-secret")
    request_id = "req-contract-001"
    timestamp = "2026-03-25T10:00:00+00:00"
    payload = f"shangshu|dashboard|{request_id}|{timestamp}".encode("utf-8")
    signature = hmac.new(b"shared-secret", payload, hashlib.sha256).hexdigest()

    actor = make_actor_context(
        "shangshu",
        source="dashboard",
        request_id=request_id,
        signature=signature,
        timestamp=timestamp,
    )
    entry = build_audit_entry(
        task_id="JJC-TEST-AUDIT-2",
        action="task.dispatch",
        actor=actor,
        allowed=True,
        payload={"reason": "contract"},
    )

    assert actor.request_id == request_id
    assert actor.source == "dashboard"
    assert actor.signature_verified is True
    assert entry["request_id"] == request_id
    assert entry["source"] == "dashboard"
    assert entry["signature_verified"] is True


def test_event_topic_catalog_covers_key_side_effects():
    expected_topics = {
        TOPIC_TASK_CREATED,
        TOPIC_TASK_STATUS,
        TOPIC_TASK_COMPLETED,
        TOPIC_TASK_DISPATCH,
        TOPIC_AGENT_TODO_UPDATE,
        TOPIC_TASK_STALLED,
        TOPIC_TASK_ESCALATED,
    }

    assert expected_topics <= set(EVENT_TOPIC_SCHEMAS)
    assert "task.consult.request" in EVENT_TOPIC_SCHEMAS[TOPIC_TASK_DISPATCH]["event_types"]
    assert "transition_kind" in EVENT_TOPIC_SCHEMAS[TOPIC_TASK_STATUS]["meta_required"]
    assert "task.scheduler.stalled" in EVENT_TOPIC_SCHEMAS[TOPIC_TASK_STALLED]["event_types"]


def test_event_dedupe_helpers_are_version_scoped():
    assert build_task_created_dedupe_key("JJC-EVT-1", 2) == "task-created:JJC-EVT-1:v2"
    assert build_task_state_dedupe_key("JJC-EVT-1", "Doing", 4) == "task-state:JJC-EVT-1:Doing:v4"
    assert build_task_todos_dedupe_key("JJC-EVT-1", 4, "req-001") == "task-todos:JJC-EVT-1:v4:req-001"
    assert build_task_stalled_dedupe_key("JJC-EVT-1", "Doing", 4) == "task-stalled:JJC-EVT-1:Doing:v4"
    assert build_task_escalated_dedupe_key("JJC-EVT-1", 4, 2) == "scheduler-escalate:JJC-EVT-1:v4:level2"
    assert build_auto_dispatch_key("JJC-EVT-1", "Doing", 4, "gongbu") == "auto:JJC-EVT-1:Doing:v4:gongbu"
    assert build_manual_dispatch_key("JJC-EVT-1", "Doing", 4, "gongbu", request_id="req-001") == "manual:JJC-EVT-1:Doing:v4:gongbu:req-001"
    assert build_consult_dispatch_key("JJC-EVT-1", "Assigned", 4, "shangshu", "gongbu", "req-001") == "consult:JJC-EVT-1:Assigned:v4:shangshu:gongbu:req-001"
