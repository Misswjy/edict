"""tests for shared task contract p1 extensions"""

from edict.backend.app.task_contract import (
    authorize_consultation,
    central_queue_owner,
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
