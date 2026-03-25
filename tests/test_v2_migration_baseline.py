"""Regression tests for the v2 migration baseline helpers."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path


def _load_migrator():
    path = Path(__file__).resolve().parents[1] / "edict" / "migration" / "migrate_json_to_pg.py"
    spec = importlib.util.spec_from_file_location("migrate_json_to_pg", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _FakeMigrationSession:
    def __init__(self):
        self.tasks: dict[str, object] = {}
        self.audits: dict[object, object] = {}
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, model, key):
        if getattr(model, "__name__", "") == "Task":
            return self.tasks.get(str(key))
        if getattr(model, "__name__", "") == "TaskAudit":
            return self.audits.get(key)
        return None

    def add(self, obj):
        if getattr(obj, "__tablename__", "") == "tasks":
            self.tasks[getattr(obj, "id")] = obj
            return
        if getattr(obj, "__tablename__", "") == "task_audits":
            self.audits[getattr(obj, "audit_id")] = obj

    async def commit(self):
        self.commits += 1


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
    assert params["scheduler"]["legacyImport"]["legacyTaskId"] == "JJC-LEGACY-001"
    assert params["scheduler"]["legacyImport"]["importedFrom"] == "data/tasks_source.json"
    assert params["scheduler"]["legacyImport"]["originalState"] == "Doing"
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
    assert params["scheduler"]["legacyImport"]["legacyArchivedAt"] == "2026-03-22T10:00:00+00:00"


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


def test_parse_legacy_court_session_keeps_numeric_timestamps_stable():
    migrator = _load_migrator()

    entry = migrator.parse_legacy_court_session(
        {
            "session_id": "sess-legacy-1",
            "topic": "迁移朝议",
            "task_id": "JJC-COURT-001",
            "officials": [],
            "messages": [],
            "phase": "discussing",
            "round": 2,
            "created_at": 1_742_860_800.0,
            "updated_at": 1_742_861_234.0,
        }
    )

    assert entry is not None
    assert entry["ts"].isoformat() == "2025-03-25T00:07:14+00:00"
    assert entry["payload"]["importedFrom"] == "data/court_discuss_sessions.json"
    assert entry["payload"]["session"]["updated_at"] == 1_742_861_234.0
    same_entry = migrator.parse_legacy_court_session(
        {
            "session_id": "sess-legacy-1",
            "topic": "迁移朝议",
            "task_id": "JJC-COURT-001",
            "officials": [],
            "messages": [],
            "phase": "discussing",
            "round": 2,
            "created_at": 1_742_860_800.0,
            "updated_at": 1_742_861_234.0,
        }
    )
    assert same_entry is not None
    assert same_entry["audit_id"] == entry["audit_id"]


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
        json.dumps(
            {
                "dispatchChannel": "slack",
                "agents": [
                    {"id": "gongbu", "skills": []},
                    {"id": "zhongshu", "skills": []},
                ],
            }
        ),
        encoding="utf-8",
    )
    openclaw = tmp_path / "openclaw"
    skill_workspace = openclaw / "workspace-gongbu" / "skills" / "remote-brain"
    skill_workspace.mkdir(parents=True)
    (skill_workspace / "SKILL.md").write_text("remote skill", encoding="utf-8")
    (skill_workspace / ".source.json").write_text(
        json.dumps(
            {
                "sourceUrl": "https://example.com/remote-brain/SKILL.md",
                "description": "remote brainstorm",
                "addedAt": "2026-03-25T00:00:00+00:00",
                "lastUpdated": "2026-03-25T01:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    report = migrator.analyze_migration_sources(data_dir / "tasks_source.json", remote_root=openclaw)

    assert report["tasks"]["total"] == 2
    assert report["tasks"]["archived"] == 1
    assert report["tasks"]["normalizedState"]["Doing"] == 1
    assert report["tasks"]["normalizedState"]["Sili"] == 1
    assert report["sidecars"]["modelChanges"] == 1
    assert report["sidecars"]["courtDiscussSessions"] == 1
    assert report["sidecars"]["morningConfigPresent"] is True
    assert report["sidecars"]["morningBriefFiles"] == ["morning_brief_20260325.json"]
    assert report["sidecars"]["dispatchChannel"] == "slack"
    assert report["sidecars"]["officialsStatsStrategy"] == "runtime-derived"
    skill_index = report["sidecars"]["skillIndex"]
    assert skill_index["total"] == 0
    assert skill_index["remoteSkillCount"] == 1
    assert skill_index["remoteSkills"][0]["agentId"] == "gongbu"
    assert any(entry["agent"] == "gongbu" for entry in skill_index["perAgent"])
    assert report["sidecars"]["agentConfigSnapshotEligible"] is True
    assert report["sidecars"]["skillInventorySnapshotEligible"] is True


def test_parse_agent_config_snapshot_uses_openclaw_runtime_state(tmp_path: Path):
    migrator = _load_migrator()

    project_root = tmp_path / "repo"
    data_dir = project_root / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "agent_config.json").write_text(json.dumps({"dispatchChannel": "slack"}), encoding="utf-8")

    oclaw = tmp_path / ".openclaw"
    oclaw.mkdir(parents=True)
    (oclaw / "openclaw.json").write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {"model": {"primary": "openai/gpt-4o"}},
                    "list": [{"id": "gongbu", "workspace": str(oclaw / "workspace-gongbu")}],
                }
            }
        ),
        encoding="utf-8",
    )
    skill_dir = oclaw / "workspace-gongbu" / "skills" / "dispatch"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: dispatch\n---\nDispatch helper\n", encoding="utf-8")

    entry = migrator.parse_agent_config_snapshot(project_root_override=project_root, remote_root=oclaw)

    assert entry is not None
    assert entry["action"] == "config.agent.snapshot"
    assert entry["payload"]["config"]["dispatchChannel"] == "slack"
    assert entry["payload"]["config"]["agents"][0]["id"] == "gongbu"


def test_migrate_dry_run_returns_bundle_with_reconciliation(tmp_path: Path):
    migrator = _load_migrator()

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "tasks_source.json").write_text(
        json.dumps({"tasks": [{"id": "JJC-DRY-001", "title": "dry run", "state": "Doing"}]}),
        encoding="utf-8",
    )

    result = asyncio.run(migrator.migrate(data_dir / "tasks_source.json", dry_run=True))

    assert result["report"]["tasks"]["total"] == 1
    assert result["stats"]["total"] == 1
    assert result["reconciliation"]["dryRun"] is True
    assert result["reconciliation"]["tasks"]["sourceMatchesProcessed"] is False
    assert result["reconciliation"]["ready"] is True


def test_parse_skill_inventory_snapshot_requires_local_or_remote_skills(tmp_path: Path):
    migrator = _load_migrator()

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "tasks_source.json").write_text(json.dumps({"tasks": []}), encoding="utf-8")
    (data_dir / "agent_config.json").write_text(
        json.dumps({"agents": [{"id": "gongbu", "skills": [{"name": "dispatch"}]}]}),
        encoding="utf-8",
    )

    entry = migrator.parse_skill_inventory_snapshot(
        file_path=data_dir / "tasks_source.json",
        remote_root=None,
    )

    assert entry is not None
    assert entry["action"] == "skill.inventory.snapshot"
    assert entry["payload"]["inventory"]["total"] == 1


def test_migrate_formal_import_persists_sidecars_and_skips_duplicates(tmp_path: Path, monkeypatch):
    migrator = _load_migrator()

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "tasks_source.json").write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "id": "JJC-IMPORT-001",
                        "title": "正式迁移任务",
                        "state": "Doing",
                        "official": "工部尚书",
                        "org": "工部",
                        "review_round": 2,
                        "archived": True,
                        "archivedAt": "2026-03-25T08:15:00+00:00",
                        "flow_log": [{"remark": "legacy-flow"}],
                        "progress_log": [{"content": "legacy-progress"}],
                        "consultLog": [{"to": "hubu"}],
                        "_scheduler": {"retryCount": 1},
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (data_dir / "model_change_log.json").write_text(
        json.dumps(
            [{"agentId": "gongbu", "oldModel": "openai/gpt-4o-mini", "newModel": "openai/gpt-4o", "at": "2026-03-25T00:00:00+00:00"}],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (data_dir / "court_discuss_sessions.json").write_text(
        json.dumps(
            {
                "sess-1": {
                    "session_id": "sess-1",
                    "topic": "迁移朝议",
                    "task_id": "JJC-IMPORT-001",
                    "officials": [],
                    "messages": [],
                    "phase": "discussing",
                    "round": 1,
                    "updated_at": 1_742_861_234.0,
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (data_dir / "morning_brief_config.json").write_text(
        json.dumps({"categories": [{"name": "政治", "enabled": True}], "keywords": ["AI"]}, ensure_ascii=False),
        encoding="utf-8",
    )
    (data_dir / "morning_brief_20260325.json").write_text(
        json.dumps({"date": "20260325", "categories": {"政治": [{"title": "晨报"}]}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (data_dir / "agent_config.json").write_text(
        json.dumps(
            {
                "dispatchChannel": "slack",
                "agents": [{"id": "gongbu", "skills": [{"name": "dispatch"}]}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    openclaw = tmp_path / "openclaw"
    openclaw.mkdir(parents=True)
    (openclaw / "openclaw.json").write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {"model": {"primary": "openai/gpt-4o"}},
                    "list": [{"id": "gongbu", "workspace": str(openclaw / "workspace-gongbu")}],
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    remote_skill = openclaw / "workspace-gongbu" / "skills" / "remote-brain"
    remote_skill.mkdir(parents=True)
    (remote_skill / "SKILL.md").write_text("remote skill", encoding="utf-8")
    (remote_skill / ".source.json").write_text(
        json.dumps(
            {
                "sourceUrl": "https://example.com/remote-brain/SKILL.md",
                "description": "remote brainstorm",
                "addedAt": "2026-03-25T00:00:00+00:00",
                "lastUpdated": "2026-03-25T01:00:00+00:00",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    fake_db = _FakeMigrationSession()

    class Task:
        __tablename__ = "tasks"

        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class TaskAudit:
        __tablename__ = "task_audits"

        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    fake_db_module = types.ModuleType("app.db")
    fake_db_module.async_session = lambda: fake_db
    fake_models_package = types.ModuleType("app.models")
    fake_models_package.__path__ = []
    fake_task_module = types.ModuleType("app.models.task")
    fake_task_module.Task = Task
    fake_task_audit_module = types.ModuleType("app.models.task_audit")
    fake_task_audit_module.TaskAudit = TaskAudit

    monkeypatch.setitem(sys.modules, "app.db", fake_db_module)
    monkeypatch.setitem(sys.modules, "app.models", fake_models_package)
    monkeypatch.setitem(sys.modules, "app.models.task", fake_task_module)
    monkeypatch.setitem(sys.modules, "app.models.task_audit", fake_task_audit_module)

    first = asyncio.run(migrator.migrate(data_dir / "tasks_source.json", dry_run=False, remote_root=openclaw))

    assert first["stats"]["migrated"] == 1
    assert first["stats"]["skipped"] == 0
    assert first["stats"]["errors"] == 0
    assert first["reconciliation"]["tasks"]["sourceMatchesProcessed"] is True
    assert first["stats"]["audits"]["modelChanges"]["imported"] == 1
    assert first["stats"]["audits"]["courtDiscussSessions"]["imported"] == 1
    assert first["stats"]["audits"]["morningConfig"]["imported"] == 1
    assert first["stats"]["audits"]["morningBriefs"]["imported"] == 1
    assert first["stats"]["audits"]["dispatchChannel"]["imported"] == 1
    assert first["stats"]["audits"]["agentConfigSnapshot"]["imported"] == 1
    assert first["stats"]["audits"]["skillInventorySnapshot"]["imported"] == 1
    assert first["stats"]["audits"]["skillIndex"] == {"local": 1, "remote": 1}

    task = fake_db.tasks["JJC-IMPORT-001"]
    assert task.review_round == 2
    assert task.archived is True
    assert task.scheduler["legacyImport"]["importedFrom"] == "data/tasks_source.json"
    assert task.scheduler["legacyImport"]["originalState"] == "Doing"
    assert task.scheduler["legacyImport"]["legacyArchivedAt"] == "2026-03-25T08:15:00+00:00"

    audits = list(fake_db.audits.values())
    actions = {audit.action for audit in audits}
    assert actions == {
        "config.set_model",
        "court_discuss.snapshot",
        "morning.config.snapshot",
        "morning.brief.snapshot",
        "config.set_dispatch_channel",
        "config.agent.snapshot",
        "skill.inventory.snapshot",
    }
    court_audit = next(audit for audit in audits if audit.action == "court_discuss.snapshot")
    assert court_audit.payload["importedFrom"] == "data/court_discuss_sessions.json"
    skill_audit = next(audit for audit in audits if audit.action == "skill.inventory.snapshot")
    assert skill_audit.payload["inventory"]["remoteSkillCount"] == 1
    assert skill_audit.payload["inventory"]["remoteSkills"][0]["sourceUrl"] == "https://example.com/remote-brain/SKILL.md"

    second = asyncio.run(migrator.migrate(data_dir / "tasks_source.json", dry_run=False, remote_root=openclaw))

    assert second["stats"]["migrated"] == 0
    assert second["stats"]["skipped"] == 1
    assert second["stats"]["errors"] == 0
    assert second["reconciliation"]["tasks"]["sourceMatchesProcessed"] is True
    assert second["stats"]["audits"]["modelChanges"]["skipped"] == 1
    assert second["stats"]["audits"]["courtDiscussSessions"]["skipped"] == 1
    assert second["stats"]["audits"]["morningConfig"]["skipped"] == 1
    assert second["stats"]["audits"]["morningBriefs"]["skipped"] == 1
    assert second["stats"]["audits"]["dispatchChannel"]["skipped"] == 1
    assert second["stats"]["audits"]["agentConfigSnapshot"]["skipped"] == 1
    assert second["stats"]["audits"]["skillInventorySnapshot"]["skipped"] == 1
    assert fake_db.commits == 2
