"""Regression tests for rollback-window delta export/merge helpers."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_export_module():
    return _load_module(
        Path(__file__).resolve().parents[1] / "ops" / "cutover" / "export_v2_delta.py",
        "export_v2_delta",
    )


def _load_merge_module():
    return _load_module(
        Path(__file__).resolve().parents[1] / "ops" / "cutover" / "merge_delta_into_legacy.py",
        "merge_delta_into_legacy",
    )


def test_export_delta_collects_tasks_changed_by_task_and_audit_windows(tmp_path: Path):
    export_mod = _load_export_module()

    tasks_file = tmp_path / "live_status.json"
    audits_file = tmp_path / "task_audit_log.json"
    tasks_file.write_text(
        json.dumps(
            {
                "tasks": [
                    {"id": "JJC-1", "updatedAt": "2026-03-25T01:00:00+00:00", "title": "窗口内任务"},
                    {"id": "JJC-2", "updatedAt": "2026-03-24T01:00:00+00:00", "title": "靠审计入窗"},
                    {"id": "JJC-3", "updatedAt": "2026-03-24T01:00:00+00:00", "title": "窗外任务"},
                ]
            }
        ),
        encoding="utf-8",
    )
    audits_file.write_text(
        json.dumps(
            [
                {"task_id": "JJC-2", "ts": "2026-03-25T02:00:00+00:00", "action": "task.advance"},
                {"task_id": "JJC-3", "ts": "2026-03-24T02:00:00+00:00", "action": "task.advance"},
            ]
        ),
        encoding="utf-8",
    )

    payload = export_mod.export_delta(
        tasks_file=tasks_file,
        audits_file=audits_file,
        start=export_mod._parse_iso("2026-03-25T00:00:00+00:00"),
        end=export_mod._parse_iso("2026-03-25T03:00:00+00:00"),
    )

    assert payload["taskIds"] == ["JJC-1", "JJC-2"]
    assert [task["id"] for task in payload["tasks"]] == ["JJC-1", "JJC-2"]
    assert payload["stats"]["auditsInWindow"] == 1


def test_merge_delta_prefers_higher_state_version_and_unions_logs():
    merge_mod = _load_merge_module()

    legacy_payload = {
        "tasks": [
            {
                "id": "JJC-1",
                "_stateVersion": 2,
                "updatedAt": "2026-03-25T00:00:00+00:00",
                "state": "Doing",
                "flow_log": [{"remark": "legacy"}],
                "progress_log": [],
                "consultLog": [],
                "todos": [{"id": "t1", "title": "旧 todo"}],
            }
        ]
    }
    delta_payload = {
        "window": {"from": "2026-03-25T00:00:00+00:00", "to": "2026-03-25T04:00:00+00:00"},
        "tasks": [
            {
                "id": "JJC-1",
                "_stateVersion": 3,
                "updatedAt": "2026-03-25T03:00:00+00:00",
                "state": "Review",
                "flow_log": [{"remark": "delta"}],
                "progress_log": [],
                "consultLog": [],
                "todos": [{"id": "t2", "title": "新 todo"}],
            },
            {
                "id": "JJC-2",
                "_stateVersion": 1,
                "updatedAt": "2026-03-25T02:00:00+00:00",
                "state": "Pending",
                "flow_log": [],
                "progress_log": [],
                "consultLog": [],
                "todos": [],
            },
        ]
    }

    merged_payload, report = merge_mod.merge_delta(
        delta_payload=delta_payload,
        legacy_payload=legacy_payload,
    )

    tasks = {item["id"]: item for item in merged_payload["tasks"]}
    assert tasks["JJC-1"]["state"] == "Review"
    assert tasks["JJC-1"]["_stateVersion"] == 3
    assert len(tasks["JJC-1"]["flow_log"]) == 2
    assert len(tasks["JJC-1"]["todos"]) == 2
    assert tasks["JJC-2"]["state"] == "Pending"
    assert report["updated"] == 1
    assert report["added"] == 1


def test_merge_delta_keeps_legacy_when_state_version_and_timestamp_are_newer():
    merge_mod = _load_merge_module()

    legacy_payload = [
        {
            "id": "JJC-9",
            "_stateVersion": 5,
            "updatedAt": "2026-03-25T05:00:00+00:00",
            "state": "Done",
            "flow_log": [],
            "progress_log": [],
            "consultLog": [],
            "todos": [],
        }
    ]
    delta_payload = {
        "tasks": [
            {
                "id": "JJC-9",
                "_stateVersion": 4,
                "updatedAt": "2026-03-25T04:00:00+00:00",
                "state": "Review",
                "flow_log": [{"remark": "delta"}],
                "progress_log": [],
                "consultLog": [],
                "todos": [],
            }
        ]
    }

    merged_payload, report = merge_mod.merge_delta(
        delta_payload=delta_payload,
        legacy_payload=legacy_payload,
    )

    assert merged_payload[0]["state"] == "Done"
    assert report["kept"] == 1
    assert report["updated"] == 0
