"""Regression tests for the legacy-v2 parity diff tool."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_diff_tool():
    path = Path(__file__).resolve().parents[1] / "scripts" / "diff_legacy_vs_v2.py"
    spec = importlib.util.spec_from_file_location("diff_legacy_vs_v2", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_live_status_compare_sorts_tasks_and_ignores_v2_sync_metadata():
    tool = _load_diff_tool()

    legacy = {
        "tasks": [
            {"id": "JJC-2", "state": "Doing", "title": "乙"},
            {"id": "JJC-1", "state": "Pending", "title": "甲"},
        ],
        "syncStatus": {"ok": True},
    }
    v2 = {
        "tasks": [
            {"id": "JJC-1", "state": "Pending", "title": "甲"},
            {"id": "JJC-2", "state": "Doing", "title": "乙"},
        ],
        "syncStatus": {
            "ok": True,
            "engine": "edict-backend",
            "controlPlane": "fastapi",
            "queueMetrics": {"shangshu": {"waiting": 1, "tasks": [{"taskId": "JJC-2", "waitSec": 30}]}},
            "updatedAt": "2026-03-25T00:00:00+00:00",
        },
    }

    report = tool.compare_payloads("live-status", legacy, v2, ignore_patterns=tool.DEFAULT_IGNORE_PATTERNS["live-status"])

    assert report["ok"] is True
    assert report["diffCount"] == 0
    assert report["ignoredDiffCount"] >= 1
    assert [item["id"] for item in report["legacy"]["tasks"]] == ["JJC-1", "JJC-2"]


def test_queue_metrics_compare_reports_field_level_diff():
    tool = _load_diff_tool()

    legacy = {
        "ok": True,
        "queues": {
            "shangshu": {
                "waiting": 1,
                "tasks": [{"taskId": "JJC-1", "waitSec": 120}],
            }
        },
        "checkedAt": "2026-03-25T00:00:00+00:00",
    }
    v2 = {
        "ok": True,
        "queues": {
            "shangshu": {
                "waiting": 2,
                "tasks": [{"taskId": "JJC-1", "waitSec": 120}],
            }
        },
        "checkedAt": "2026-03-25T00:01:00+00:00",
    }

    report = tool.compare_payloads("queue-metrics", legacy, v2, ignore_patterns=tool.DEFAULT_IGNORE_PATTERNS["queue-metrics"])

    assert report["ok"] is False
    assert report["diffCount"] == 1
    assert report["diffs"][0]["path"] == "queues/shangshu/waiting"
    assert report["ignoredDiffCount"] == 1


def test_partition_diffs_respects_glob_ignore_patterns():
    tool = _load_diff_tool()
    diffs = [
        {"path": "tasks/0/updatedAt", "kind": "value", "legacy": "a", "v2": "b"},
        {"path": "tasks/0/state", "kind": "value", "legacy": "Doing", "v2": "Review"},
    ]

    kept, ignored = tool.partition_diffs(diffs, ["tasks/*/updatedAt"])

    assert [item["path"] for item in kept] == ["tasks/0/state"]
    assert [item["path"] for item in ignored] == ["tasks/0/updatedAt"]


def test_cli_offline_compare_writes_report_file(tmp_path: Path):
    tool = _load_diff_tool()
    legacy_file = tmp_path / "legacy.json"
    v2_file = tmp_path / "v2.json"
    report_file = tmp_path / "report.json"

    legacy_file.write_text(json.dumps({"ok": True, "queues": {"menxia": {"waiting": 1}}, "checkedAt": "a"}), encoding="utf-8")
    v2_file.write_text(json.dumps({"ok": True, "queues": {"menxia": {"waiting": 1}}, "checkedAt": "b"}), encoding="utf-8")

    exit_code = tool.main(
        [
            "queue-metrics",
            "--legacy-file",
            str(legacy_file),
            "--v2-file",
            str(v2_file),
            "--report-file",
            str(report_file),
        ]
    )

    saved = json.loads(report_file.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert saved["ok"] is True
    assert saved["ignoredDiffCount"] == 1
