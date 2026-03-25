"""Regression tests for v2 compatibility write-side services."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from edict.backend.app.services.admin_action_service import AdminActionService
from edict.backend.app.services.skills_service import SkillsService
from edict.backend.app.task_contract import make_actor_context


class _ImmediateThread:
    def __init__(self, *, target=None, daemon=None):
        self.target = target
        self.daemon = daemon

    def start(self):
        if self.target:
            self.target()


def test_set_dispatch_channel_updates_agent_config_snapshot(tmp_path: Path):
    project_root = tmp_path / "repo"
    data_dir = project_root / "data"
    data_dir.mkdir(parents=True)
    oclaw = tmp_path / ".openclaw"
    oclaw.mkdir(parents=True)
    (oclaw / "openclaw.json").write_text(json.dumps({"agents": {"defaults": {"model": {"primary": "openai/gpt-4o"}}, "list": []}}), encoding="utf-8")

    svc = AdminActionService(project_root_override=project_root, openclaw_home_override=oclaw, runner=lambda *a, **k: SimpleNamespace(returncode=0))
    result = __import__("asyncio").run(svc.set_dispatch_channel("slack", make_actor_context("emperor", source="test")))

    saved = json.loads((data_dir / "agent_config.json").read_text(encoding="utf-8"))
    assert result["ok"] is True
    assert saved["dispatchChannel"] == "slack"


def test_set_model_updates_openclaw_config_and_change_log(tmp_path: Path):
    project_root = tmp_path / "repo"
    (project_root / "data").mkdir(parents=True)
    oclaw = tmp_path / ".openclaw"
    oclaw.mkdir(parents=True)
    openclaw_cfg = oclaw / "openclaw.json"
    openclaw_cfg.write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {"model": {"primary": "openai/gpt-4o"}},
                    "list": [{"id": "gongbu", "model": "openai/gpt-4o"}],
                }
            }
        ),
        encoding="utf-8",
    )

    calls = []

    def runner(*args, **kwargs):
        calls.append(args[0])
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    svc = AdminActionService(project_root_override=project_root, openclaw_home_override=oclaw, runner=runner)
    result = __import__("asyncio").run(svc.set_model("gongbu", "anthropic/claude-sonnet-4-6", make_actor_context("emperor", source="test")))

    updated = json.loads(openclaw_cfg.read_text(encoding="utf-8"))
    changelog = json.loads((project_root / "data" / "model_change_log.json").read_text(encoding="utf-8"))
    assert result["ok"] is True
    assert updated["agents"]["list"][0]["model"] == "anthropic/claude-sonnet-4-6"
    assert changelog[-1]["agentId"] == "gongbu"
    assert calls[-1] == ["openclaw", "gateway", "restart"]


def test_wake_agent_checks_permission_and_starts_background_job(tmp_path: Path):
    oclaw = tmp_path / ".openclaw"
    (oclaw / "workspace-gongbu").mkdir(parents=True)

    calls = []

    def runner(*args, **kwargs):
        calls.append(args[0])
        if args[0][:2] == ["pgrep", "-f"]:
            return SimpleNamespace(returncode=0, stderr="", stdout="")
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    svc = AdminActionService(
        openclaw_home_override=oclaw,
        runner=runner,
        thread_factory=_ImmediateThread,
    )
    result = __import__("asyncio").run(svc.wake_agent("gongbu", "", make_actor_context("shangshu", source="test")))

    assert result["ok"] is True
    assert any(call[:3] == ["openclaw", "agent", "--agent"] for call in calls)


def test_add_and_remove_remote_skill_manage_workspace_files(tmp_path: Path):
    project_root = tmp_path / "repo"
    (project_root / "data").mkdir(parents=True)
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

    source_path = tmp_path / "REMOTE_SKILL.md"
    source_path.write_text("---\nname: brainstorming\n---\nRemote skill\n", encoding="utf-8")

    svc = SkillsService(project_root_override=project_root, openclaw_home_override=oclaw)
    actor = make_actor_context("emperor", source="test")

    added = __import__("asyncio").run(svc.add_remote_skill("gongbu", "brainstorming", f"file://{source_path}", "remote helper", actor))
    removed = __import__("asyncio").run(svc.remove_remote_skill("gongbu", "brainstorming", actor))

    assert added["ok"] is True
    assert removed["ok"] is True
    assert not (oclaw / "workspace-gongbu" / "skills" / "brainstorming").exists()


def test_add_local_skill_writes_template(tmp_path: Path):
    oclaw = tmp_path / ".openclaw"
    svc = SkillsService(openclaw_home_override=oclaw)
    actor = make_actor_context("emperor", source="test")

    result = __import__("asyncio").run(svc.add_skill_to_agent("gongbu", "code-review", "Review helper", "review code", actor))

    skill_md = oclaw / "workspace-gongbu" / "skills" / "code-review" / "SKILL.md"
    assert result["ok"] is True
    assert skill_md.exists()
    assert "Review helper" in skill_md.read_text(encoding="utf-8")
