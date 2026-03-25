"""Runtime-derived officials statistics for the v2 compatibility API."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ..generated.institution_schema import AGENT_DIRECTORY
from .agent_config_service import (
    build_agent_config_payload,
    get_agents_status_payload,
    heartbeat_from_agent_status,
    load_openclaw_config,
    normalize_model,
    openclaw_home,
)

MODEL_PRICING = {
    "anthropic/claude-sonnet-4-6": {"in": 3.0, "out": 15.0, "cr": 0.30, "cw": 3.75},
    "anthropic/claude-opus-4-5": {"in": 15.0, "out": 75.0, "cr": 1.50, "cw": 18.75},
    "anthropic/claude-haiku-3-5": {"in": 0.8, "out": 4.0, "cr": 0.08, "cw": 1.0},
    "openai/gpt-4o": {"in": 2.5, "out": 10.0, "cr": 1.25, "cw": 0.0},
    "openai/gpt-4o-mini": {"in": 0.15, "out": 0.6, "cr": 0.075, "cw": 0.0},
    "google/gemini-2.0-flash": {"in": 0.075, "out": 0.3, "cr": 0.0, "cw": 0.0},
    "google/gemini-2.5-pro": {"in": 1.25, "out": 10.0, "cr": 0.0, "cw": 0.0},
}


def _session_stats(agent_id: str, *, openclaw_home_override: Path | None = None) -> dict[str, Any]:
    sessions_file = openclaw_home(openclaw_home_override=openclaw_home_override) / "agents" / agent_id / "sessions" / "sessions.json"
    if not sessions_file.exists() and agent_id == "sili":
        sessions_file = openclaw_home(openclaw_home_override=openclaw_home_override) / "agents" / "main" / "sessions" / "sessions.json"
    if not sessions_file.exists():
        return {
            "tokens_in": 0,
            "tokens_out": 0,
            "cache_read": 0,
            "cache_write": 0,
            "sessions": 0,
            "messages": 0,
            "last_active": None,
        }

    try:
        data = json.loads(sessions_file.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}

    tokens_in = tokens_out = cache_read = cache_write = 0
    last_dt = None
    for session in data.values():
        tokens_in += int(session.get("inputTokens", 0) or 0)
        tokens_out += int(session.get("outputTokens", 0) or 0)
        cache_read += int(session.get("cacheRead", 0) or 0)
        cache_write += int(session.get("cacheWrite", 0) or 0)
        updated_at = session.get("updatedAt")
        try:
            if isinstance(updated_at, (int, float)):
                current = datetime.fromtimestamp(updated_at / 1000)
            elif isinstance(updated_at, str) and updated_at:
                current = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            else:
                current = None
        except Exception:
            current = None
        if current and (last_dt is None or current > last_dt):
            last_dt = current

    messages = 0
    if data:
        latest_key = max(data, key=lambda key: data[key].get("updatedAt", 0) or 0)
        session_file = data[latest_key].get("sessionFile")
        if session_file:
            session_path = sessions_file.parent / Path(str(session_file)).name
            try:
                for line in session_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                    payload = json.loads(line)
                    if payload.get("type") == "message" and payload.get("message", {}).get("role") == "assistant":
                        messages += 1
            except Exception:
                messages = 0

    return {
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "sessions": len(data),
        "messages": messages,
        "last_active": last_dt.strftime("%Y-%m-%d %H:%M") if last_dt else None,
    }


def _calc_cost_usd(stats: dict[str, Any], model: str) -> float:
    pricing = MODEL_PRICING.get(model, MODEL_PRICING["anthropic/claude-sonnet-4-6"])
    usd = (
        stats["tokens_in"] / 1e6 * pricing["in"]
        + stats["tokens_out"] / 1e6 * pricing["out"]
        + stats["cache_read"] / 1e6 * pricing["cr"]
        + stats["cache_write"] / 1e6 * pricing["cw"]
    )
    return round(usd, 4)


def _task_stats(org_label: str, tasks: list[dict[str, Any]]) -> dict[str, Any]:
    done = [task for task in tasks if task.get("state") == "Done" and task.get("org") == org_label]
    active = [task for task in tasks if task.get("state") in {"Doing", "Review", "Assigned"} and task.get("org") == org_label]
    flow_participations = 0
    participated_edicts: list[dict[str, str]] = []
    seen_edicts: set[str] = set()
    for task in tasks:
        for flow in task.get("flow_log", []) or []:
            if flow.get("from") == org_label or flow.get("to") == org_label:
                flow_participations += 1
                if str(task.get("id", "")).startswith("JJC") and task.get("id") not in seen_edicts:
                    seen_edicts.add(task["id"])
                    participated_edicts.append(
                        {
                            "id": task.get("id", ""),
                            "title": task.get("title", ""),
                            "state": task.get("state", ""),
                        }
                    )
                break
    return {
        "tasks_done": len(done),
        "tasks_active": len(active),
        "flow_participations": flow_participations,
        "participated_edicts": participated_edicts,
    }


def build_officials_payload(
    tasks: list[dict[str, Any]],
    *,
    openclaw_home_override: Path | None = None,
    agents_status_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = load_openclaw_config(openclaw_home_override=openclaw_home_override)
    agent_cfg = build_agent_config_payload(openclaw_home_override=openclaw_home_override)
    model_by_agent = {agent["id"]: normalize_model(agent.get("model"), agent_cfg.get("defaultModel", "unknown")) for agent in agent_cfg.get("agents", [])}
    default_model = normalize_model(cfg.get("agents", {}).get("defaults", {}).get("model"), agent_cfg.get("defaultModel", "unknown"))
    status_payload = agents_status_payload or get_agents_status_payload(openclaw_home_override=openclaw_home_override)
    status_by_agent = {item["id"]: item for item in status_payload.get("agents", [])}

    officials: list[dict[str, Any]] = []
    for official in AGENT_DIRECTORY:
        agent_id = official["id"]
        model = model_by_agent.get(agent_id, default_model)
        session = _session_stats(agent_id, openclaw_home_override=openclaw_home_override)
        task_stats = _task_stats(official["label"], tasks)
        status = status_by_agent.get(agent_id, {}).get("status", "idle")
        heartbeat = heartbeat_from_agent_status(status)
        cost_usd = _calc_cost_usd(session, model)
        merit_score = task_stats["tasks_done"] * 10 + task_stats["flow_participations"] * 2 + min(session["sessions"], 20)
        officials.append(
            {
                "id": agent_id,
                "label": official["label"],
                "emoji": official["emoji"],
                "role": official["role"],
                "rank": official.get("rank", ""),
                "model": model,
                "model_short": model.split("/")[-1] if "/" in str(model) else str(model),
                "sessions": session["sessions"],
                "tokens_in": session["tokens_in"],
                "tokens_out": session["tokens_out"],
                "cache_read": session["cache_read"],
                "cache_write": session["cache_write"],
                "tokens_total": session["tokens_in"] + session["tokens_out"],
                "messages": session["messages"],
                "cost_usd": cost_usd,
                "cost_cny": round(cost_usd * 7.25, 2),
                "last_active": session["last_active"],
                "heartbeat": heartbeat,
                "tasks_done": task_stats["tasks_done"],
                "tasks_active": task_stats["tasks_active"],
                "flow_participations": task_stats["flow_participations"],
                "participated_edicts": task_stats["participated_edicts"],
                "merit_score": merit_score,
            }
        )

    officials.sort(key=lambda item: item["merit_score"], reverse=True)
    for index, item in enumerate(officials, start=1):
        item["merit_rank"] = index

    totals = {
        "tokens_total": sum(item["tokens_total"] for item in officials),
        "cache_total": sum(item["cache_read"] + item["cache_write"] for item in officials),
        "cost_usd": round(sum(item["cost_usd"] for item in officials), 2),
        "cost_cny": round(sum(item["cost_cny"] for item in officials), 2),
        "tasks_done": sum(item["tasks_done"] for item in officials),
    }
    top = max(officials, key=lambda item: item["merit_score"], default={})
    return {
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "officials": officials,
        "totals": totals,
        "top_official": top.get("label", ""),
    }
