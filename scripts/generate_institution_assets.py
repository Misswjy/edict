#!/usr/bin/env python3
"""Generate institution assets from config/institution_schema.json."""

from __future__ import annotations

import json
import pprint
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "config" / "institution_schema.json"
PY_OUT = ROOT / "edict" / "backend" / "app" / "generated" / "institution_schema.py"
TS_OUT = ROOT / "edict" / "frontend" / "src" / "generated" / "institutionSchema.ts"
DOC_OUT = ROOT / "docs" / "generated" / "institution-schema.md"
OPENCLAW_OUT = ROOT / "docker" / "demo_data" / "openclaw.json"


def py(value) -> str:
    return pprint.pformat(value, sort_dicts=False, width=100)


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def build_data(schema: dict) -> dict:
    states = schema["states"]
    agents = schema["agents"]

    task_states = [state["key"] for state in states]
    state_aliases = {
        alias: state["key"]
        for state in states
        for alias in state.get("aliases", [])
    }
    state_labels = {state["key"]: state["contractLabel"] for state in states}
    ui_state_labels = {state["key"]: state["uiLabel"] for state in states}
    for state in states:
        for alias, label in state.get("aliasLabels", {}).items():
            ui_state_labels[alias] = label
    state_pipe_index = {state["key"]: int(state["pipeIndex"]) for state in states}
    state_default_org = {
        state["key"]: state["defaultOrg"]
        for state in states
        if state.get("defaultOrg") != ""
    }
    state_owner_agent = {
        state["key"]: state["ownerAgent"]
        for state in states
        if state.get("ownerAgent")
    }
    state_dispatch_agent = {
        state["key"]: state["dispatchAgent"]
        for state in states
        if state.get("dispatchAgent")
    }
    central_queue_states = {
        state["key"]: state["centralQueueOwner"]
        for state in states
        if state.get("centralQueueOwner")
    }
    manual_advance_flow = {
        state: (
            spec["to"],
            spec["fromOrg"],
            spec["toOrg"],
            spec["remark"],
        )
        for state, spec in schema["manualAdvanceFlow"].items()
    }
    terminal_states = [state["key"] for state in states if state.get("terminal")]
    execution_states = [state["key"] for state in states if state.get("execution")]

    org_agent_map = {
        agent["label"]: agent["id"]
        for agent in agents
        if agent.get("executionDept")
    }
    agent_org_map = {value: key for key, value in org_agent_map.items()}
    default_allow_agents = {
        agent["id"]: list(agent.get("allowAgents", []))
        for agent in agents
    }
    consultation_allow_agents = {
        agent["id"]: list(agent.get("consultAgents", []))
        for agent in agents
        if agent.get("consultAgents")
    }
    agent_directory = [
        {
            "id": agent["id"],
            "label": agent["label"],
            "emoji": agent["emoji"],
            "role": agent["role"],
            "rank": agent["rank"],
        }
        for agent in agents
    ]
    pipe_state_idx = dict(state_pipe_index)
    for alias, state in state_aliases.items():
        pipe_state_idx[alias] = state_pipe_index[state]

    return {
        "task_states": task_states,
        "pipe_stages": schema["pipeStages"],
        "state_aliases": state_aliases,
        "state_labels": state_labels,
        "ui_state_labels": ui_state_labels,
        "state_pipe_index": state_pipe_index,
        "pipe_state_idx": pipe_state_idx,
        "state_default_org": state_default_org,
        "state_owner_agent": state_owner_agent,
        "state_dispatch_agent": state_dispatch_agent,
        "central_queue_states": central_queue_states,
        "central_queue_sla": schema["centralQueueSla"],
        "valid_transitions": schema["transitions"],
        "manual_advance_flow": manual_advance_flow,
        "terminal_states": terminal_states,
        "execution_states": execution_states,
        "org_agent_map": org_agent_map,
        "agent_org_map": agent_org_map,
        "default_allow_agents": default_allow_agents,
        "consultation_allow_agents": consultation_allow_agents,
        "agent_directory": agent_directory,
        "org_colors": schema["orgColors"],
        "agents": agents,
    }


def render_python(data: dict) -> str:
    enum_lines = "\n".join(
        f'    {state} = "{state}"'
        for state in data["task_states"]
    )
    return f'''"""Generated from config/institution_schema.json. Do not edit by hand."""

from __future__ import annotations

import enum


class TaskState(str, enum.Enum):
{enum_lines}


PIPE_STAGES = {py(data["pipe_stages"])}
STATE_ALIASES = {py(data["state_aliases"])}
TASK_STATE_VALUES = tuple(state.value for state in TaskState)
TERMINAL_STATES = set({py(data["terminal_states"])})
EXECUTION_STATES = set({py(data["execution_states"])})
STATE_PIPE_INDEX = {py(data["state_pipe_index"])}
STATE_LABELS = {py(data["state_labels"])}
UI_STATE_LABELS = {py(data["ui_state_labels"])}
STATE_DEFAULT_ORG = {py(data["state_default_org"])}
STATE_OWNER_AGENT = {py(data["state_owner_agent"])}
STATE_DISPATCH_AGENT = {py(data["state_dispatch_agent"])}
CENTRAL_QUEUE_STATES = {py(data["central_queue_states"])}
CENTRAL_QUEUE_SLA = {py(data["central_queue_sla"])}
VALID_TRANSITIONS = {py(data["valid_transitions"])}
MANUAL_ADVANCE_FLOW = {py(data["manual_advance_flow"])}
ORG_AGENT_MAP = {py(data["org_agent_map"])}
AGENT_ORG_MAP = {py(data["agent_org_map"])}
DEFAULT_ALLOW_AGENTS = {py(data["default_allow_agents"])}
CONSULTATION_ALLOW_AGENTS = {py(data["consultation_allow_agents"])}
AGENT_DIRECTORY = {py(data["agent_directory"])}
DEPT_COLOR = {py(data["org_colors"])}
'''


def render_ts(data: dict) -> str:
    return (
        "// Generated from config/institution_schema.json. Do not edit by hand.\n\n"
        "export type PipeStage = { key: string; dept: string; icon: string; action: string };\n"
        "export type DeptDef = { id: string; label: string; emoji: string; role: string; rank: string };\n\n"
        f'export const PIPE: PipeStage[] = {json.dumps(data["pipe_stages"], ensure_ascii=False, indent=2)};\n\n'
        f'export const PIPE_STATE_IDX: Record<string, number> = {json.dumps(data["pipe_state_idx"], ensure_ascii=False, indent=2)};\n\n'
        f'export const DEPT_COLOR: Record<string, string> = {json.dumps(data["org_colors"], ensure_ascii=False, indent=2)};\n\n'
        f'export const STATE_LABEL: Record<string, string> = {json.dumps(data["ui_state_labels"], ensure_ascii=False, indent=2)};\n\n'
        f'export const DEPTS: DeptDef[] = {json.dumps(data["agent_directory"], ensure_ascii=False, indent=2)};\n'
    )


def render_docs(data: dict) -> str:
    state_lines = [
        "| 状态 | 契约标签 | UI 标签 | 默认归属 |",
        "| --- | --- | --- | --- |",
    ]
    for state in load_schema()["states"]:
        state_lines.append(
            f'| `{state["key"]}` | {state["contractLabel"]} | {state["uiLabel"]} | {state["defaultOrg"] or "执行部门"} |'
        )

    agent_lines = [
        "| Agent | 官署 | 角色 | 可派发给 | 可咨询 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for agent in load_schema()["agents"]:
        allow = ", ".join(agent.get("allowAgents", [])) or "-"
        consult = ", ".join(agent.get("consultAgents", [])) or "-"
        agent_lines.append(
            f'| `{agent["id"]}` | {agent["label"]} | {agent["role"]} | {allow} | {consult} |'
        )

    return "\n".join(
        [
            "# Institution Schema",
            "",
            "该文档片段由 `config/institution_schema.json` 自动生成。",
            "",
            "## 状态定义",
            "",
            *state_lines,
            "",
            "## Agent 定义",
            "",
            *agent_lines,
            "",
        ]
    )


def render_openclaw(data: dict) -> str:
    existing = {}
    if OPENCLAW_OUT.exists():
        existing = json.loads(OPENCLAW_OUT.read_text(encoding="utf-8"))

    existing.setdefault("agents", {})
    existing["agents"].setdefault("defaults", {"model": {"primary": "anthropic/claude-sonnet-4-6"}})
    existing.setdefault("providers", {})
    existing["agents"]["list"] = [
        {
            "id": agent["id"],
            "workspace": f'/app/.openclaw/workspace-{agent["id"]}',
            "subagents": {"allowAgents": list(agent.get("allowAgents", []))},
        }
        for agent in data["agents"]
    ]
    return json.dumps(existing, ensure_ascii=False, indent=2) + "\n"


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def main() -> None:
    schema = load_schema()
    data = build_data(schema)
    write(PY_OUT, render_python(data))
    write(TS_OUT, render_ts(data))
    write(DOC_OUT, render_docs(data))
    write(OPENCLAW_OUT, render_openclaw(data))
    print("Generated institution assets:")
    print(f"  - {PY_OUT.relative_to(ROOT)}")
    print(f"  - {TS_OUT.relative_to(ROOT)}")
    print(f"  - {DOC_OUT.relative_to(ROOT)}")
    print(f"  - {OPENCLAW_OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
