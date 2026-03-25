"""Regression tests for v2 court discussion service."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from edict.backend.app.services.court_discuss_service import (
    COURT_DISCUSS_DESTROY_ACTION,
    COURT_DISCUSS_SNAPSHOT_ACTION,
    CourtDiscussService,
    get_official_profiles,
    project_sessions_from_audit_entries,
)
from edict.backend.app.task_contract import make_actor_context


def test_project_sessions_from_audit_entries_tracks_latest_snapshot_and_destroy():
    entries = [
        {
            "action": COURT_DISCUSS_SNAPSHOT_ACTION,
            "payload": {
                "session": {
                    "session_id": "sess-1",
                    "topic": "议题一",
                    "officials": [{"id": "sili", "name": "司礼监"}],
                    "messages": [{"type": "system", "content": "start"}],
                    "round": 0,
                    "phase": "discussing",
                    "created_at": 1.0,
                    "updated_at": 1.0,
                }
            },
        },
        {
            "action": COURT_DISCUSS_SNAPSHOT_ACTION,
            "payload": {
                "session": {
                    "session_id": "sess-1",
                    "topic": "议题一",
                    "officials": [{"id": "sili", "name": "司礼监"}],
                    "messages": [{"type": "system", "content": "start"}, {"type": "official", "content": "答复"}],
                    "round": 1,
                    "phase": "concluded",
                    "summary": "已有定论",
                    "created_at": 1.0,
                    "updated_at": 2.0,
                    "concluded_at": 2.0,
                }
            },
        },
    ]

    sessions = project_sessions_from_audit_entries(entries)
    assert sessions["sess-1"]["round"] == 1
    assert sessions["sess-1"]["phase"] == "concluded"

    sessions = project_sessions_from_audit_entries(
        entries
        + [
            {
                "action": COURT_DISCUSS_DESTROY_ACTION,
                "payload": {"session_id": "sess-1"},
            }
        ]
    )
    assert "sess-1" not in sessions


def test_court_discuss_service_persists_across_reload_with_shadow_fallback(tmp_path: Path):
    project_root = tmp_path / "repo"
    (project_root / "data").mkdir(parents=True)
    svc = CourtDiscussService(project_root_override=project_root)
    actor = make_actor_context("emperor", source="test")

    created = asyncio.run(svc.create_session("讨论 v2 改造收尾", ["sili", "zhongshu", "menxia"], "JJC-COURT-001", actor))
    assert created["ok"] is True
    session_id = created["session_id"]

    advanced = asyncio.run(svc.advance_discussion(session_id, "请速议剩余关键项", None, actor))
    assert advanced["ok"] is True
    assert advanced["round"] == 1
    assert len(advanced["new_messages"]) == 3

    concluded = asyncio.run(svc.conclude_session(session_id, actor))
    assert concluded["ok"] is True
    assert concluded["summary"]

    reloaded = CourtDiscussService(project_root_override=project_root)
    recovered = asyncio.run(reloaded.get_session(session_id))
    assert recovered is not None
    assert recovered["phase"] == "concluded"
    assert recovered["summary"] == concluded["summary"]
    assert any("朝堂议政结束" in item.get("content", "") for item in recovered["messages"])

    sessions = asyncio.run(reloaded.list_sessions())
    assert sessions
    assert sessions[0]["session_id"] == session_id
    assert sessions[0]["summary"] == concluded["summary"]

    shadow = json.loads((project_root / "data" / "court_discuss_sessions.json").read_text(encoding="utf-8"))
    assert session_id in shadow


def test_destroy_session_removes_shadow_copy_and_officials_payload_is_available(tmp_path: Path):
    project_root = tmp_path / "repo"
    (project_root / "data").mkdir(parents=True)
    svc = CourtDiscussService(project_root_override=project_root)
    actor = make_actor_context("emperor", source="test")

    created = asyncio.run(svc.create_session("讨论风险", ["sili", "gongbu"], actor=actor))
    session_id = created["session_id"]
    destroyed = asyncio.run(svc.destroy_session(session_id, actor))
    payload = asyncio.run(svc.get_officials_payload())

    assert destroyed["ok"] is True
    assert asyncio.run(svc.get_session(session_id)) is None
    assert payload["ok"] is True
    assert payload["officials"]["sili"]["name"] == get_official_profiles()["sili"]["name"]
