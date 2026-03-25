"""Shared event contract for the v2 event-driven backend."""

from __future__ import annotations

from typing import Any

from .task_contract import build_consult_key, build_dispatch_key, canonicalize_state

TOPIC_TASK_CREATED = "task.created"
TOPIC_TASK_PLANNING_REQUEST = "task.planning.request"
TOPIC_TASK_PLANNING_COMPLETE = "task.planning.complete"
TOPIC_TASK_REVIEW_REQUEST = "task.review.request"
TOPIC_TASK_REVIEW_RESULT = "task.review.result"
TOPIC_TASK_DISPATCH = "task.dispatch"
TOPIC_TASK_STATUS = "task.status"
TOPIC_TASK_COMPLETED = "task.completed"
TOPIC_TASK_CLOSED = "task.closed"
TOPIC_TASK_REPLAN = "task.replan"
TOPIC_TASK_STALLED = "task.stalled"
TOPIC_TASK_ESCALATED = "task.escalated"

TOPIC_AGENT_THOUGHTS = "agent.thoughts"
TOPIC_AGENT_TODO_UPDATE = "agent.todo.update"
TOPIC_AGENT_HEARTBEAT = "agent.heartbeat"


EVENT_TOPIC_SCHEMAS: dict[str, dict[str, Any]] = {
    TOPIC_TASK_CREATED: {
        "description": "任务创建快照",
        "event_types": ["task.created"],
        "payload_required": ["id", "title", "state", "org", "_stateVersion"],
        "meta_required": ["source", "request_id", "version"],
        "dedupe_rule": "task-created:{task_id}:v{version}",
    },
    TOPIC_TASK_STATUS: {
        "description": "非终态状态变更",
        "event_types": ["task.state.*"],
        "payload_required": ["task_id", "from", "to", "reason", "org", "targetDept", "lane", "_stateVersion", "task"],
        "meta_required": ["source", "request_id", "version", "transition_kind"],
        "dedupe_rule": "task-state:{task_id}:{to_state}:v{version}",
    },
    TOPIC_TASK_COMPLETED: {
        "description": "终态状态变更",
        "event_types": ["task.state.*"],
        "payload_required": ["task_id", "from", "to", "reason", "org", "targetDept", "lane", "_stateVersion", "task"],
        "meta_required": ["source", "request_id", "version", "transition_kind"],
        "dedupe_rule": "task-state:{task_id}:{to_state}:v{version}",
    },
    TOPIC_TASK_DISPATCH: {
        "description": "派发与横向咨询",
        "event_types": ["task.dispatch.request", "task.consult.request"],
        "payload_required": ["task_id", "agent", "message", "state", "dispatch_key", "version", "task"],
        "meta_required": ["source", "request_id", "version", "dispatch_key"],
        "dedupe_rule": "dispatch/consult key（见 build_dispatch_key / build_consult_key）",
    },
    TOPIC_TASK_STALLED: {
        "description": "停滞扫描结果",
        "event_types": ["task.scheduler.stalled"],
        "payload_required": ["task_id", "state", "org", "stalledSec", "thresholdSec", "retryCount", "escalationLevel", "task"],
        "meta_required": ["source", "request_id", "version"],
        "dedupe_rule": "task-stalled:{task_id}:{state}:v{version}",
    },
    TOPIC_TASK_ESCALATED: {
        "description": "调度升级",
        "event_types": ["task.scheduler.escalated"],
        "payload_required": ["task_id", "state", "target", "reason", "level", "task"],
        "meta_required": ["source", "request_id", "version", "level"],
        "dedupe_rule": "scheduler-escalate:{task_id}:v{version}:level{level}",
    },
    TOPIC_AGENT_TODO_UPDATE: {
        "description": "任务 Todo 快照更新",
        "event_types": ["task.todos.updated"],
        "payload_required": ["task_id", "items", "source_of_truth"],
        "meta_required": ["source", "request_id", "version", "source_of_truth"],
        "dedupe_rule": "task-todos:{task_id}:v{version}:{request_id}",
    },
    TOPIC_AGENT_HEARTBEAT: {
        "description": "派发执行心跳",
        "event_types": ["agent.dispatch.start"],
        "payload_required": ["task_id", "agent", "dispatch_key", "version"],
        "meta_required": ["dispatch_key", "version"],
        "dedupe_rule": "dispatch-heartbeat:{dispatch_key}:start",
    },
    TOPIC_AGENT_THOUGHTS: {
        "description": "Agent 输出/思考流",
        "event_types": ["agent.output"],
        "payload_required": ["task_id", "agent", "output", "return_code", "dispatch_key", "version"],
        "meta_required": ["dispatch_key", "version"],
        "dedupe_rule": "dispatch-output:{dispatch_key}",
    },
}


def build_task_created_dedupe_key(task_id: str, version: int) -> str:
    return f"task-created:{task_id}:v{max(1, int(version or 1))}"


def build_task_state_dedupe_key(task_id: str, to_state: str, version: int) -> str:
    return f"task-state:{task_id}:{canonicalize_state(to_state)}:v{max(1, int(version or 1))}"


def build_task_todos_dedupe_key(task_id: str, version: int, request_id: str = "") -> str:
    suffix = f":{request_id}" if request_id else ""
    return f"task-todos:{task_id}:v{max(1, int(version or 1))}{suffix}"


def build_task_stalled_dedupe_key(task_id: str, state: str, version: int) -> str:
    return f"task-stalled:{task_id}:{canonicalize_state(state)}:v{max(1, int(version or 1))}"


def build_task_escalated_dedupe_key(task_id: str, version: int, level: int) -> str:
    return f"scheduler-escalate:{task_id}:v{max(1, int(version or 1))}:level{max(0, int(level or 0))}"


def build_agent_heartbeat_dedupe_key(dispatch_key: str, stage: str = "start") -> str:
    return f"dispatch-heartbeat:{dispatch_key}:{stage}" if dispatch_key else ""


def build_agent_output_dedupe_key(dispatch_key: str) -> str:
    return f"dispatch-output:{dispatch_key}" if dispatch_key else ""


def build_consult_dispatch_key(
    task_id: str,
    state: str,
    version: int,
    source_agent: str,
    target_agent: str,
    request_id: str = "",
) -> str:
    return build_consult_key(task_id, state, version, source_agent, target_agent, request_id)


def build_auto_dispatch_key(
    task_id: str,
    state: str,
    version: int,
    agent: str,
    *,
    request_id: str = "",
) -> str:
    return build_dispatch_key(task_id, state, version, agent, manual=False, request_id=request_id)


def build_manual_dispatch_key(
    task_id: str,
    state: str,
    version: int,
    agent: str,
    *,
    request_id: str = "",
) -> str:
    return build_dispatch_key(task_id, state, version, agent, manual=True, request_id=request_id)
