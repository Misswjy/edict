"""Regression tests for v2 compatibility read-model services."""

from __future__ import annotations

import json
import asyncio
from pathlib import Path

from edict.backend.app.services.agent_config_service import (
    build_agent_config_payload,
    heartbeat_from_agent_status,
    list_remote_skills,
    project_model_change_log,
)
from edict.backend.app.services.morning_service import (
    DEFAULT_MORNING_CONFIG,
    MorningService,
    get_morning_config,
    project_morning_brief_entries,
    project_morning_config_entries,
)
from edict.backend.app.services.officials_service import build_officials_payload
from edict.backend.app.task_contract import make_actor_context


def test_build_agent_config_payload_scans_openclaw_workspaces(tmp_path: Path):
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
                    "defaults": {"model": {"primary": "custom/default-model"}},
                    "list": [
                        {
                            "id": "gongbu",
                            "model": {"primary": "custom/gongbu-model"},
                            "workspace": str(oclaw / "workspace-gongbu"),
                            "subagents": {"allowAgents": ["shangshu"]},
                        }
                    ],
                },
                "providers": {"custom": {"models": ["custom/gongbu-model", "custom/default-model"]}},
            }
        ),
        encoding="utf-8",
    )
    skill_dir = oclaw / "workspace-gongbu" / "skills" / "code-review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: code-review\n---\nCode review helper\n", encoding="utf-8")

    payload = build_agent_config_payload(project_root_override=project_root, openclaw_home_override=oclaw)

    gongbu = next(item for item in payload["agents"] if item["id"] == "gongbu")
    assert payload["dispatchChannel"] == "slack"
    assert gongbu["model"] == "custom/gongbu-model"
    assert gongbu["skills"][0]["name"] == "code-review"
    assert any(model["id"] == "custom/default-model" for model in payload["knownModels"])


def test_list_remote_skills_reads_source_metadata(tmp_path: Path):
    oclaw = tmp_path / ".openclaw"
    skill_dir = oclaw / "workspace-gongbu" / "skills" / "brainstorming"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("content", encoding="utf-8")
    (skill_dir / ".source.json").write_text(
        json.dumps(
            {
                "sourceUrl": "https://example.com/SKILL.md",
                "description": "remote helper",
                "addedAt": "2026-03-25T00:00:00+00:00",
                "lastUpdated": "2026-03-25T01:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    payload = list_remote_skills(openclaw_home_override=oclaw)

    assert payload["count"] == 1
    assert payload["remoteSkills"][0]["agentId"] == "gongbu"
    assert payload["remoteSkills"][0]["status"] == "valid"


def test_build_officials_payload_uses_task_snapshots_and_agent_status(tmp_path: Path):
    oclaw = tmp_path / ".openclaw"
    oclaw.mkdir(parents=True)
    (oclaw / "openclaw.json").write_text(json.dumps({"agents": {"defaults": {"model": {"primary": "openai/gpt-4o"}}, "list": []}}), encoding="utf-8")
    sessions_dir = oclaw / "agents" / "gongbu" / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "sessions.json").write_text(
        json.dumps(
            {
                "sess-1": {
                    "inputTokens": 100,
                    "outputTokens": 40,
                    "cacheRead": 0,
                    "cacheWrite": 0,
                    "updatedAt": 1_742_860_800_000,
                }
            }
        ),
        encoding="utf-8",
    )

    payload = build_officials_payload(
        [
            {
                "id": "JJC-1",
                "title": "实现 v2",
                "state": "Done",
                "org": "工部",
                "flow_log": [{"from": "尚书省", "to": "工部"}],
            }
        ],
        openclaw_home_override=oclaw,
        agents_status_payload={"agents": [{"id": "gongbu", "status": "running"}]},
    )

    gongbu = next(item for item in payload["officials"] if item["id"] == "gongbu")
    assert gongbu["tasks_done"] == 1
    assert gongbu["heartbeat"]["status"] == "active"
    assert payload["top_official"] == gongbu["label"]


def test_morning_config_returns_defaults_when_missing(tmp_path: Path):
    project_root = tmp_path / "repo"
    (project_root / "data").mkdir(parents=True)

    payload = asyncio.run(get_morning_config(project_root_override=project_root))

    assert payload == DEFAULT_MORNING_CONFIG


def test_heartbeat_mapping_preserves_frontend_status_contract():
    assert heartbeat_from_agent_status("running") == {"status": "active", "label": "🟢 在线"}
    assert heartbeat_from_agent_status("offline")["status"] == "stalled"


def test_project_model_change_log_uses_audit_payload_instead_of_legacy_file():
    payload = project_model_change_log(
        [
            {
                "ts": "2026-03-25T01:00:00+00:00",
                "action": "config.set_model",
                "target_agent": "gongbu",
                "payload": {
                    "oldModel": "openai/gpt-4o",
                    "newModel": "anthropic/claude-sonnet-4-6",
                    "rolledBack": False,
                },
            },
            {
                "ts": "2026-03-25T02:00:00+00:00",
                "action": "config.set_model",
                "target_agent": "hubu",
                "payload": {
                    "oldModel": "openai/gpt-4o-mini",
                    "newModel": "openai/gpt-4o",
                    "rolledBack": True,
                },
            },
            {
                "ts": "2026-03-25T03:00:00+00:00",
                "action": "config.set_model",
                "target_agent": "gongbu",
                "payload": {},
            },
        ]
    )

    assert payload == [
        {
            "at": "2026-03-25T01:00:00+00:00",
            "agentId": "gongbu",
            "oldModel": "openai/gpt-4o",
            "newModel": "anthropic/claude-sonnet-4-6",
            "rolledBack": False,
        },
        {
            "at": "2026-03-25T02:00:00+00:00",
            "agentId": "hubu",
            "oldModel": "openai/gpt-4o-mini",
            "newModel": "openai/gpt-4o",
            "rolledBack": True,
        },
    ]


def test_project_morning_config_prefers_snapshot_payload():
    payload = project_morning_config_entries(
        [
            {"payload": {"categories": [{"name": "旧分类", "enabled": False}]}},
            {
                "payload": {
                    "config": {
                        "categories": [{"name": "政治", "enabled": True}],
                        "keywords": ["AI"],
                        "custom_feeds": [{"name": "示例", "url": "https://example.com/rss", "category": "政治"}],
                        "feishu_webhook": "",
                    }
                }
            },
        ]
    )

    assert payload["categories"] == [{"name": "政治", "enabled": True}]
    assert payload["keywords"] == ["AI"]


def test_project_morning_brief_filters_by_date():
    payload = project_morning_brief_entries(
        [
            {"payload": {"brief": {"date": "20260324", "categories": {"政治": [{"title": "旧闻"}]}}}},
            {"payload": {"brief": {"date": "20260325", "categories": {"政治": [{"title": "新闻"}]}}}},
        ],
        date="2026-03-25",
    )

    assert payload["date"] == "20260325"
    assert payload["categories"]["政治"][0]["title"] == "新闻"


def test_save_morning_config_updates_legacy_shadow_file(tmp_path: Path):
    project_root = tmp_path / "repo"
    (project_root / "data").mkdir(parents=True)

    svc = MorningService(project_root_override=project_root)
    actor = make_actor_context("emperor", source="test")

    result = asyncio.run(
        svc.save_config(
            {
                "categories": [{"name": "政治", "enabled": True}],
                "keywords": ["AI"],
                "custom_feeds": [{"name": "示例", "url": "https://example.com/rss", "category": "政治"}],
                "feishu_webhook": "",
            },
            actor,
        )
    )

    saved = json.loads((project_root / "data" / "morning_brief_config.json").read_text(encoding="utf-8"))
    assert result["ok"] is True
    assert saved["keywords"] == ["AI"]
