"""Regression tests for the v2 migration baseline helpers."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migrator():
    path = Path(__file__).resolve().parents[1] / "edict" / "migration" / "migrate_json_to_pg.py"
    spec = importlib.util.spec_from_file_location("migrate_json_to_pg", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_parse_old_task_maps_legacy_snapshot_to_current_task_columns():
    migrator = _load_migrator()

    params = migrator.parse_old_task(
        {
            "id": "JJC-LEGACY-001",
            "title": "迁移老任务",
            "state": "Doing",
            "org": "工部",
            "now": "处理中",
            "consult_log": [{"note": "旧式 consult_log"}],
            "scheduler": {"retryCount": 2, "maxRetry": 3},
            "state_version": 6,
            "target_dept": "工部",
            "template_id": "tmpl-migrate",
            "template_params": {"source": "legacy"},
            "created_at": "2026-03-20T08:00:00+00:00",
            "updatedAt": "2026-03-21T09:30:00+00:00",
        }
    )

    assert params["id"] == "JJC-LEGACY-001"
    assert params["state"].value == "Doing"
    assert params["consult_log"] == [{"note": "旧式 consult_log"}]
    assert params["scheduler"]["retryCount"] == 2
    assert params["state_version"] == 6
    assert params["target_dept"] == "工部"
    assert params["template_id"] == "tmpl-migrate"
    assert params["template_params"] == {"source": "legacy"}
    assert params["created_at"].isoformat() == "2026-03-20T08:00:00+00:00"
    assert params["updated_at"].isoformat() == "2026-03-21T09:30:00+00:00"


def test_parse_old_task_handles_legacy_state_fallbacks_and_archive_flags():
    migrator = _load_migrator()

    params = migrator.parse_old_task(
        {
            "id": "JJC-LEGACY-002",
            "title": "旧收件箱任务",
            "state": "Inbox",
            "archived": True,
            "archivedAt": "2026-03-22T10:00:00+00:00",
            "updatedAt": "2026-03-22T09:00:00+00:00",
        }
    )

    assert params["state"].value == "Sili"
    assert params["archived"] is True
    assert params["archived_at"].isoformat() == "2026-03-22T10:00:00+00:00"
