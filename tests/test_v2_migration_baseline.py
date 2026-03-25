"""Regression tests for the v2 migration baseline helpers."""

from __future__ import annotations

import importlib.util
import json
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


def test_parse_legacy_morning_sidecars_to_audit_snapshots(tmp_path: Path):
    migrator = _load_migrator()

    config_path = tmp_path / "morning_brief_config.json"
    config_path.write_text("{}", encoding="utf-8")
    config_entry = migrator.parse_legacy_morning_config(
        {
            "categories": [{"name": "政治", "enabled": True}],
            "keywords": ["AI"],
            "custom_feeds": [],
            "feishu_webhook": "",
        },
        source_path=config_path,
    )
    assert config_entry is not None
    assert config_entry["action"] == "morning.config.snapshot"
    assert config_entry["payload"]["config"]["keywords"] == ["AI"]

    brief_path = tmp_path / "morning_brief_20260325.json"
    brief_path.write_text("{}", encoding="utf-8")
    brief_entry = migrator.parse_legacy_morning_brief(
        {
            "date": "20260325",
            "generated_at": "2026-03-25T08:30:00+00:00",
            "categories": {"政治": [{"title": "迁移演练"}]},
        },
        source_path=brief_path,
    )
    assert brief_entry is not None
    assert brief_entry["action"] == "morning.brief.snapshot"
    assert brief_entry["payload"]["brief"]["categories"]["政治"][0]["title"] == "迁移演练"


def test_analyze_migration_sources_reports_sidecars(tmp_path: Path):
    migrator = _load_migrator()

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "tasks_source.json").write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "id": "JJC-LEGACY-010",
                        "title": "朝会演练",
                        "state": "Doing",
                        "review_round": 2,
                    },
                    {
                        "id": "JJC-LEGACY-011",
                        "title": "归档任务",
                        "state": "Inbox",
                        "archived": True,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    (data_dir / "model_change_log.json").write_text(
        json.dumps([{"agentId": "gongbu", "oldModel": "a", "newModel": "b", "at": "2026-03-25T00:00:00+00:00"}]),
        encoding="utf-8",
    )
    (data_dir / "court_discuss_sessions.json").write_text(
        json.dumps({"sess-1": {"session_id": "sess-1", "topic": "演练", "officials": [], "messages": []}}),
        encoding="utf-8",
    )
    (data_dir / "morning_brief_config.json").write_text(
        json.dumps({"categories": [{"name": "政治", "enabled": True}], "keywords": ["AI"]}),
        encoding="utf-8",
    )
    (data_dir / "morning_brief_20260325.json").write_text(
        json.dumps({"date": "20260325", "categories": {"政治": [{"title": "简报"}]}}),
        encoding="utf-8",
    )
    (data_dir / "agent_config.json").write_text(
        json.dumps({"dispatchChannel": "slack"}),
        encoding="utf-8",
    )

    report = migrator.analyze_migration_sources(data_dir / "tasks_source.json")

    assert report["tasks"]["total"] == 2
    assert report["tasks"]["archived"] == 1
    assert report["tasks"]["normalizedState"]["Doing"] == 1
    assert report["tasks"]["normalizedState"]["Sili"] == 1
    assert report["sidecars"]["modelChanges"] == 1
    assert report["sidecars"]["courtDiscussSessions"] == 1
    assert report["sidecars"]["morningConfigPresent"] is True
    assert report["sidecars"]["morningBriefFiles"] == ["morning_brief_20260325.json"]
    assert report["sidecars"]["dispatchChannel"] == "slack"
