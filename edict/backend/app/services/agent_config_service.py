"""Read-model services for agent/runtime configuration compatibility endpoints."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import urlopen

from ..generated.institution_schema import AGENT_DIRECTORY

SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

KNOWN_MODELS = [
    {"id": "anthropic/claude-sonnet-4-6", "label": "Claude Sonnet 4.6", "provider": "Anthropic"},
    {"id": "anthropic/claude-opus-4-5", "label": "Claude Opus 4.5", "provider": "Anthropic"},
    {"id": "anthropic/claude-haiku-3-5", "label": "Claude Haiku 3.5", "provider": "Anthropic"},
    {"id": "openai/gpt-4o", "label": "GPT-4o", "provider": "OpenAI"},
    {"id": "openai/gpt-4o-mini", "label": "GPT-4o Mini", "provider": "OpenAI"},
    {"id": "openai-codex/gpt-5.3-codex", "label": "GPT-5.3 Codex", "provider": "OpenAI Codex"},
    {"id": "google/gemini-2.0-flash", "label": "Gemini 2.0 Flash", "provider": "Google"},
    {"id": "google/gemini-2.5-pro", "label": "Gemini 2.5 Pro", "provider": "Google"},
    {"id": "copilot/claude-sonnet-4", "label": "Claude Sonnet 4", "provider": "Copilot"},
    {"id": "copilot/claude-opus-4.5", "label": "Claude Opus 4.5", "provider": "Copilot"},
    {"id": "github-copilot/claude-opus-4.6", "label": "Claude Opus 4.6", "provider": "GitHub Copilot"},
    {"id": "copilot/gpt-4o", "label": "GPT-4o", "provider": "Copilot"},
    {"id": "copilot/gemini-2.5-pro", "label": "Gemini 2.5 Pro", "provider": "Copilot"},
    {"id": "copilot/o3-mini", "label": "o3-mini", "provider": "Copilot"},
]

MAIN_ALIAS = {
    "id": "main",
    "label": "司礼监",
    "emoji": "🧾",
    "role": "掌印秉笔",
    "rank": "内廷",
}

log = logging.getLogger("edict.agent_config")


def project_root() -> Path:
    return Path(__file__).resolve().parents[4]


def data_dir(*, project_root_override: Path | None = None) -> Path:
    return (project_root_override or project_root()) / "data"


def openclaw_home(*, openclaw_home_override: Path | None = None) -> Path:
    if openclaw_home_override is not None:
        return openclaw_home_override
    raw = os.environ.get("OPENCLAW_HOME", str(Path.home() / ".openclaw"))
    return Path(raw).expanduser()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def normalize_model(model_value: Any, fallback: str = "unknown") -> str:
    if isinstance(model_value, str) and model_value:
        return model_value
    if isinstance(model_value, dict):
        return str(model_value.get("primary") or model_value.get("id") or fallback)
    return fallback


def load_openclaw_config(*, openclaw_home_override: Path | None = None) -> dict[str, Any]:
    return read_json(openclaw_home(openclaw_home_override=openclaw_home_override) / "openclaw.json", {})


def _agent_catalog() -> dict[str, dict[str, Any]]:
    catalog = {agent["id"]: dict(agent) for agent in AGENT_DIRECTORY}
    catalog.setdefault("main", dict(MAIN_ALIAS))
    return catalog


def _collect_known_models(cfg: dict[str, Any]) -> list[dict[str, str]]:
    known_ids = {item["id"] for item in KNOWN_MODELS}
    merged = list(KNOWN_MODELS)

    def _append(model_id: str, provider: str) -> None:
        if not model_id or model_id in known_ids:
            return
        known_ids.add(model_id)
        merged.append({"id": model_id, "label": model_id, "provider": provider})

    agents_cfg = cfg.get("agents", {})
    _append(normalize_model(agents_cfg.get("defaults", {}).get("model"), ""), "OpenClaw")
    for item in agents_cfg.get("list", []) or []:
        _append(normalize_model(item.get("model"), ""), "OpenClaw")
    for provider, provider_cfg in (cfg.get("providers") or {}).items():
        for raw_model in provider_cfg.get("models") or []:
            if isinstance(raw_model, str):
                _append(raw_model, provider)
            elif isinstance(raw_model, dict):
                _append(str(raw_model.get("id") or raw_model.get("name") or ""), provider)
    return merged


def collect_workspace_skills(workspace: Path) -> list[dict[str, Any]]:
    skills_dir = workspace / "skills"
    skills: list[dict[str, Any]] = []
    if not skills_dir.exists():
        return skills
    for item in sorted(skills_dir.iterdir()):
        if not item.is_dir():
            continue
        skill_md = item / "SKILL.md"
        description = ""
        if skill_md.exists():
            try:
                for line in skill_md.read_text(encoding="utf-8", errors="ignore").splitlines():
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#") and not stripped.startswith("---"):
                        description = stripped[:100]
                        break
            except Exception:
                description = "(读取失败)"
        skills.append(
            {
                "name": item.name,
                "path": str(skill_md),
                "exists": skill_md.exists(),
                "description": description,
            }
        )
    return skills


def build_agent_config_payload(
    *,
    project_root_override: Path | None = None,
    openclaw_home_override: Path | None = None,
) -> dict[str, Any]:
    root = project_root_override or project_root()
    oclaw = openclaw_home(openclaw_home_override=openclaw_home_override)
    cfg = load_openclaw_config(openclaw_home_override=oclaw)
    catalog = _agent_catalog()
    default_model = normalize_model(cfg.get("agents", {}).get("defaults", {}).get("model"), "unknown")
    existing = read_json(root / "data" / "agent_config.json", {})
    dispatch_channel = str(existing.get("dispatchChannel") or "feishu")

    agents: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in cfg.get("agents", {}).get("list", []) or []:
        agent_id = str(item.get("id") or "").strip()
        meta = catalog.get(agent_id)
        if not meta:
            continue
        workspace = Path(item.get("workspace") or (oclaw / f"workspace-{agent_id}"))
        agents.append(
            {
                "id": agent_id,
                "label": meta["label"],
                "role": meta["role"],
                "emoji": meta["emoji"],
                "model": normalize_model(item.get("model"), default_model),
                "defaultModel": default_model,
                "workspace": str(workspace),
                "skills": collect_workspace_skills(workspace),
                "allowAgents": list(item.get("subagents", {}).get("allowAgents", []) or []),
            }
        )
        seen_ids.add(agent_id)

    order = [agent["id"] for agent in AGENT_DIRECTORY] + ["main"]
    for agent_id in order:
        if agent_id in seen_ids:
            continue
        meta = catalog.get(agent_id)
        if not meta:
            continue
        workspace = oclaw / f"workspace-{agent_id}"
        agents.append(
            {
                "id": agent_id,
                "label": meta["label"],
                "role": meta["role"],
                "emoji": meta["emoji"],
                "model": default_model,
                "defaultModel": default_model,
                "workspace": str(workspace),
                "skills": collect_workspace_skills(workspace),
                "allowAgents": [],
                "isDefaultModel": True,
            }
        )

    return {
        "generatedAt": now_iso(),
        "defaultModel": default_model,
        "knownModels": _collect_known_models(cfg),
        "dispatchChannel": dispatch_channel,
        "agents": agents,
    }


def read_skill_content(
    agent_id: str,
    skill_name: str,
    *,
    project_root_override: Path | None = None,
    openclaw_home_override: Path | None = None,
) -> dict[str, Any]:
    if not SAFE_NAME_RE.match(agent_id) or not SAFE_NAME_RE.match(skill_name):
        return {"ok": False, "error": "参数含非法字符"}
    cfg = build_agent_config_payload(
        project_root_override=project_root_override,
        openclaw_home_override=openclaw_home_override,
    )
    agent = next((item for item in cfg.get("agents", []) if item.get("id") == agent_id), None)
    if not agent:
        return {"ok": False, "error": f"Agent {agent_id} 不存在"}
    skill = next((item for item in agent.get("skills", []) if item.get("name") == skill_name), None)
    if not skill:
        return {"ok": False, "error": f"技能 {skill_name} 不存在"}
    skill_path = Path(skill.get("path", "")).resolve()
    allowed_roots = (
        openclaw_home(openclaw_home_override=openclaw_home_override).resolve(),
        (project_root_override or project_root()).resolve(),
    )
    if not any(str(skill_path).startswith(str(root)) for root in allowed_roots):
        return {"ok": False, "error": "路径不在允许的目录范围内"}
    if not skill_path.exists():
        return {"ok": True, "name": skill_name, "agent": agent_id, "content": "(SKILL.md 文件不存在)", "path": str(skill_path)}
    try:
        content = skill_path.read_text(encoding="utf-8", errors="ignore")
        return {"ok": True, "name": skill_name, "agent": agent_id, "content": content, "path": str(skill_path)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def list_remote_skills(*, openclaw_home_override: Path | None = None) -> dict[str, Any]:
    oclaw = openclaw_home(openclaw_home_override=openclaw_home_override)
    remote_skills: list[dict[str, Any]] = []
    for workspace in sorted(oclaw.glob("workspace-*")):
        agent_id = workspace.name.replace("workspace-", "")
        skills_dir = workspace / "skills"
        if not skills_dir.exists():
            continue
        for skill_dir in sorted(skills_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            source_json = skill_dir / ".source.json"
            skill_md = skill_dir / "SKILL.md"
            if not source_json.exists():
                continue
            source = read_json(source_json, {})
            remote_skills.append(
                {
                    "skillName": skill_dir.name,
                    "agentId": agent_id,
                    "sourceUrl": str(source.get("sourceUrl") or ""),
                    "description": str(source.get("description") or ""),
                    "localPath": str(skill_md),
                    "addedAt": str(source.get("addedAt") or ""),
                    "lastUpdated": str(source.get("lastUpdated") or ""),
                    "status": "valid" if skill_md.exists() else "not-found",
                }
            )
    return {"ok": True, "remoteSkills": remote_skills, "count": len(remote_skills), "listedAt": now_iso()}


def project_model_change_log(audit_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    for item in audit_entries:
        payload = dict(item.get("payload") or {})
        old_model = str(payload.get("oldModel") or "")
        new_model = str(payload.get("newModel") or "")
        agent_id = str(item.get("target_agent") or payload.get("agentId") or "")
        if not agent_id or (not old_model and not new_model):
            continue
        projected.append(
            {
                "at": str(item.get("ts") or item.get("at") or ""),
                "agentId": agent_id,
                "oldModel": old_model,
                "newModel": new_model,
                "rolledBack": bool(payload.get("rolledBack") or False),
            }
        )
    projected.sort(key=lambda item: (item.get("at") or "", item.get("agentId") or "", item.get("newModel") or ""))
    return projected


async def get_model_change_log(limit: int = 200) -> list[dict[str, Any]]:
    try:
        from sqlalchemy import select

        from ..db import async_session
        from ..models.task_audit import TaskAudit
    except ModuleNotFoundError:
        log.warning("model-change-log audit store unavailable; sqlalchemy dependency missing")
        return []

    async with async_session() as db:
        rows = (
            await db.execute(
                select(TaskAudit)
                .where(TaskAudit.action == "config.set_model")
                .order_by(TaskAudit.ts.desc())
                .limit(limit)
            )
        ).scalars().all()
    return project_model_change_log([row.to_dict() for row in reversed(rows)])


def _check_gateway_alive() -> bool:
    try:
        result = subprocess.run(["pgrep", "-f", "openclaw-gateway"], capture_output=True, text=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False


def _check_gateway_probe(gateway_url: str) -> bool:
    try:
        response = urlopen(gateway_url, timeout=3)
        return getattr(response, "status", 0) == 200
    except Exception:
        return False


def _check_agent_workspace(agent_id: str, *, openclaw_home_override: Path | None = None) -> bool:
    return (openclaw_home(openclaw_home_override=openclaw_home_override) / f"workspace-{agent_id}").is_dir()


def _check_agent_process(agent_id: str) -> bool:
    try:
        result = subprocess.run(["pgrep", "-f", f"openclaw.*--agent.*{agent_id}"], capture_output=True, text=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False


def _get_agent_session_status(agent_id: str, *, openclaw_home_override: Path | None = None) -> tuple[int, int, bool]:
    sessions_file = openclaw_home(openclaw_home_override=openclaw_home_override) / "agents" / agent_id / "sessions" / "sessions.json"
    if not sessions_file.exists():
        return 0, 0, False
    data = read_json(sessions_file, {})
    if not isinstance(data, dict):
        return 0, 0, False
    last_ts = 0
    for value in data.values():
        ts = value.get("updatedAt", 0)
        if isinstance(ts, (int, float)) and ts > last_ts:
            last_ts = int(ts)
    now_ms = int(datetime.now().timestamp() * 1000)
    age_ms = now_ms - last_ts if last_ts else 10**12
    return last_ts, len(data), age_ms <= 2 * 60 * 1000


def heartbeat_from_agent_status(status: str) -> dict[str, str]:
    mapping = {
        "running": {"status": "active", "label": "🟢 在线"},
        "idle": {"status": "idle", "label": "⚪ 待命"},
        "offline": {"status": "stalled", "label": "🔴 离线"},
        "unconfigured": {"status": "unknown", "label": "⚫ 未配置"},
    }
    return mapping.get(status, {"status": "unknown", "label": "⚪ 未知"})


def get_agents_status_payload(
    *,
    openclaw_home_override: Path | None = None,
    gateway_url: str = "http://127.0.0.1:18789/",
) -> dict[str, Any]:
    gateway_alive = _check_gateway_alive()
    gateway_probe = _check_gateway_probe(gateway_url) if gateway_alive else False
    agents: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for meta in AGENT_DIRECTORY:
        agent_id = meta["id"]
        if agent_id in seen_ids:
            continue
        seen_ids.add(agent_id)
        has_workspace = _check_agent_workspace(agent_id, openclaw_home_override=openclaw_home_override)
        last_ts, session_count, is_busy = _get_agent_session_status(agent_id, openclaw_home_override=openclaw_home_override)
        process_alive = _check_agent_process(agent_id)

        if not has_workspace:
            status = "unconfigured"
            status_label = "❌ 未配置"
        elif not gateway_alive:
            status = "offline"
            status_label = "🔴 Gateway 离线"
        elif process_alive or is_busy:
            status = "running"
            status_label = "🟢 运行中"
        elif last_ts > 0:
            now_ms = int(datetime.now().timestamp() * 1000)
            age_ms = now_ms - last_ts
            if age_ms <= 10 * 60 * 1000:
                status = "idle"
                status_label = "🟡 待命"
            elif age_ms <= 3600 * 1000:
                status = "idle"
                status_label = "⚪ 空闲"
            else:
                status = "idle"
                status_label = "⚪ 休眠"
        else:
            status = "idle"
            status_label = "⚪ 无记录"

        last_active = None
        if last_ts > 0:
            try:
                last_active = datetime.fromtimestamp(last_ts / 1000).strftime("%m-%d %H:%M")
            except Exception:
                last_active = None

        agents.append(
            {
                "id": agent_id,
                "label": meta["label"],
                "emoji": meta["emoji"],
                "role": meta["role"],
                "status": status,
                "statusLabel": status_label,
                "lastActive": last_active,
                "lastActiveTs": last_ts,
                "sessions": session_count,
                "hasWorkspace": has_workspace,
                "processAlive": process_alive,
            }
        )

    return {
        "ok": True,
        "gateway": {
            "alive": gateway_alive,
            "probe": gateway_probe,
            "status": "🟢 运行中" if gateway_probe else ("🟡 进程在但无响应" if gateway_alive else "🔴 未启动"),
        },
        "agents": agents,
        "checkedAt": now_iso(),
    }
