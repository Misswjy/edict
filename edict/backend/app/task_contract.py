"""Shared task contract for legacy JSON mode and the Edict backend."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .generated.institution_schema import (
    AGENT_DIRECTORY,
    AGENT_ORG_MAP,
    CENTRAL_QUEUE_SLA,
    CENTRAL_QUEUE_STATES,
    CONSULTATION_ALLOW_AGENTS as _CONSULTATION_ALLOW_AGENTS,
    DEFAULT_ALLOW_AGENTS as _DEFAULT_ALLOW_AGENTS,
    DEPT_COLOR,
    EXECUTION_STATES as _EXECUTION_STATES,
    MANUAL_ADVANCE_FLOW,
    ORG_AGENT_MAP,
    PIPE_STAGES,
    STATE_ALIASES,
    STATE_DEFAULT_ORG,
    STATE_DISPATCH_AGENT,
    STATE_LABELS,
    STATE_OWNER_AGENT,
    TASK_STATE_VALUES,
    TERMINAL_STATES as _TERMINAL_STATES,
    TaskState,
    UI_STATE_LABELS,
    VALID_TRANSITIONS as _VALID_TRANSITIONS,
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


TERMINAL_STATES = set(_TERMINAL_STATES)
EXECUTION_STATES = set(_EXECUTION_STATES)
TASK_LANE_VALUES = tuple(CENTRAL_QUEUE_SLA.keys())
VALID_TRANSITIONS = {state: set(targets) for state, targets in _VALID_TRANSITIONS.items()}
DEFAULT_ALLOW_AGENTS = {state: set(targets) for state, targets in _DEFAULT_ALLOW_AGENTS.items()}
CONSULTATION_ALLOW_AGENTS = {state: set(targets) for state, targets in _CONSULTATION_ALLOW_AGENTS.items()}
POLICY_VERSION = "2026-03-24-p2"

HIGH_PRIVILEGE_ACTORS = {"emperor", "system"}
SCHEDULER_ACTORS = {"sili", "system", "emperor"}


@dataclass(frozen=True)
class ActorContext:
    actor_id: str
    actor_type: str = "agent"
    source: str = "unknown"
    request_id: str = ""
    signature_verified: bool = False


def canonicalize_state(value: str | TaskState | None) -> str:
    if isinstance(value, TaskState):
        return value.value
    raw = (value or "").strip()
    if raw in STATE_ALIASES:
        return STATE_ALIASES[raw]
    return raw


def canonicalize_actor(value: str | None) -> str:
    actor = (value or "").strip()
    return "sili" if actor == "main" else actor


def canonicalize_lane(value: str | None) -> str:
    lane = (value or "").strip().lower()
    return lane if lane in TASK_LANE_VALUES else "standard"


def actor_type_for(actor_id: str) -> str:
    if actor_id in {"emperor", "dashboard"}:
        return "user"
    if actor_id == "system":
        return "system"
    return "agent"


def verify_actor_signature(actor_id: str, source: str, request_id: str, timestamp: str, signature: str) -> bool:
    secret = (os.environ.get("EDICT_ACTOR_SHARED_SECRET") or "").strip()
    if not secret or not signature:
        return False
    payload = f"{actor_id}|{source}|{request_id}|{timestamp}".encode("utf-8")
    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def make_actor_context(actor_id: str | None, source: str = "unknown", request_id: str | None = None, signature: str = "", timestamp: str = "") -> ActorContext:
    actor = canonicalize_actor(actor_id) or "anonymous"
    rid = request_id or str(uuid.uuid4())
    return ActorContext(
        actor_id=actor,
        actor_type=actor_type_for(actor),
        source=source or "unknown",
        request_id=rid,
        signature_verified=verify_actor_signature(actor, source or "unknown", rid, timestamp or "", signature or ""),
    )


def _copy_jsonlike(value: Any) -> Any:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, dict):
        return dict(value)
    return value


def _coerce_iso_text(value: Any) -> str:
    if isinstance(value, datetime):
        dt = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    text = str(value or "").strip()
    return text


def _apply_task_aliases(task: dict[str, Any]) -> None:
    alias_groups = {
        "flow_log": ("flow_log", "flowLog"),
        "progress_log": ("progress_log", "progressLog"),
        "consultLog": ("consultLog", "consult_log"),
        "_scheduler": ("_scheduler", "scheduler"),
        "_prev_state": ("_prev_state", "prev_state"),
        "_stateVersion": ("_stateVersion", "state_version"),
        "templateId": ("templateId", "template_id"),
        "templateParams": ("templateParams", "template_params"),
        "targetDept": ("targetDept", "target_dept"),
        "review_round": ("review_round", "reviewRound"),
        "createdAt": ("createdAt", "created_at"),
        "updatedAt": ("updatedAt", "updated_at"),
    }
    for canonical, aliases in alias_groups.items():
        if task.get(canonical) is not None:
            continue
        for alias in aliases:
            if alias == canonical:
                continue
            value = task.get(alias)
            if value is None:
                continue
            if canonical in {"createdAt", "updatedAt"}:
                task[canonical] = _coerce_iso_text(value)
            else:
                task[canonical] = _copy_jsonlike(value)
            break


def task_defaults(now: str | None = None) -> dict[str, Any]:
    ts = now or utc_now_iso()
    return {
        "official": "",
        "org": STATE_DEFAULT_ORG[TaskState.Pending.value],
        "state": TaskState.Pending.value,
        "now": "",
        "eta": "-",
        "block": "无",
        "output": "",
        "ac": "",
        "priority": "normal",
        "lane": "standard",
        "review_round": 0,
        "archived": False,
        "flow_log": [],
        "progress_log": [],
        "consultLog": [],
        "todos": [],
        "templateId": "",
        "templateParams": {},
        "targetDept": "",
        "_prev_state": "",
        "_stateVersion": 1,
        "_scheduler": {},
        "createdAt": ts,
        "updatedAt": ts,
    }


def ensure_task_shape(task: dict[str, Any], now: str | None = None) -> dict[str, Any]:
    _apply_task_aliases(task)
    defaults = task_defaults(now)
    for key, value in defaults.items():
        if key not in task or task.get(key) is None:
            task[key] = _copy_jsonlike(value)
    task["state"] = canonicalize_state(task.get("state")) or TaskState.Pending.value
    task["flow_log"] = list(task.get("flow_log") or [])
    task["progress_log"] = list(task.get("progress_log") or [])
    task["consultLog"] = list(task.get("consultLog") or [])
    task["todos"] = list(task.get("todos") or [])
    task["templateParams"] = dict(task.get("templateParams") or {})
    task["_scheduler"] = dict(task.get("_scheduler") or {})
    task["review_round"] = int(task.get("review_round") or 0)
    task["archived"] = bool(task.get("archived"))
    task["block"] = task.get("block") or "无"
    task["eta"] = task.get("eta") or "-"
    task["lane"] = canonicalize_lane(task.get("lane"))
    task.setdefault("id", "")
    task.setdefault("title", "")
    task.setdefault("official", "")
    task.setdefault("templateId", "")
    task.setdefault("targetDept", "")
    task.setdefault("_prev_state", "")
    task["_stateVersion"] = max(1, int(task.get("_stateVersion") or 1))
    if task["state"] in EXECUTION_STATES:
        execution_org = resolve_execution_org(task)
        if execution_org:
            task["org"] = execution_org
    elif task["state"] in STATE_DEFAULT_ORG and not task.get("org"):
        task["org"] = STATE_DEFAULT_ORG[task["state"]]
    return task


def resolve_execution_org(task: dict[str, Any]) -> str:
    current_org = (task.get("org") or "").strip()
    if current_org in ORG_AGENT_MAP:
        return current_org
    target_org = (task.get("targetDept") or "").strip()
    if target_org in ORG_AGENT_MAP:
        return target_org
    return ""


def resolve_state_org(task: dict[str, Any], state: str | None = None) -> str:
    current = canonicalize_state(state or task.get("state"))
    if current in EXECUTION_STATES:
        return resolve_execution_org(task)
    return STATE_DEFAULT_ORG.get(current, (task.get("org") or "").strip())


def is_fast_lane(task: dict[str, Any]) -> bool:
    return canonicalize_lane(task.get("lane")) == "fast"


def fast_lane_ready(task: dict[str, Any]) -> bool:
    ensure_task_shape(task)
    return is_fast_lane(task) and bool(resolve_execution_org(task))


def central_queue_owner(state: str | None) -> str:
    return CENTRAL_QUEUE_STATES.get(canonicalize_state(state), "")


def queue_sla_seconds(task: dict[str, Any], state: str | None = None) -> int:
    lane = "fast" if is_fast_lane(task) else "standard"
    current = canonicalize_state(state or task.get("state"))
    return int(CENTRAL_QUEUE_SLA.get(lane, {}).get(current, 0))


def resolve_state_owner(task: dict[str, Any], state: str | None = None) -> str:
    current = canonicalize_state(state or task.get("state"))
    if current in EXECUTION_STATES:
        return ORG_AGENT_MAP.get(resolve_execution_org(task), "")
    return STATE_OWNER_AGENT.get(current, "")


def resolve_dispatch_agent(task: dict[str, Any], state: str | None = None) -> str:
    current = canonicalize_state(state or task.get("state"))
    if current in EXECUTION_STATES:
        return ORG_AGENT_MAP.get(resolve_execution_org(task), "")
    return STATE_DISPATCH_AGENT.get(current, "")


def can_dispatch_to(from_actor: str, to_actor: str) -> bool:
    actor = canonicalize_actor(from_actor)
    target = canonicalize_actor(to_actor)
    if actor in HIGH_PRIVILEGE_ACTORS:
        return True
    return target in DEFAULT_ALLOW_AGENTS.get(actor, set())


def ensure_execution_assignment(task: dict[str, Any], new_state: str) -> tuple[bool, str]:
    next_state = canonicalize_state(new_state)
    if next_state not in EXECUTION_STATES:
        return True, ""
    execution_org = resolve_execution_org(task)
    if not execution_org:
        return False, "Doing/Next 必须绑定具体执行部门或执行 Agent"
    task["org"] = execution_org
    return True, ""


def validate_transition(task: dict[str, Any], new_state: str | TaskState) -> tuple[bool, str]:
    ensure_task_shape(task)
    current = canonicalize_state(task.get("state"))
    target = canonicalize_state(new_state)
    if not target:
        return False, "目标状态不能为空"
    if target not in TASK_STATE_VALUES:
        return False, f"未知状态: {target}"
    allowed = VALID_TRANSITIONS.get(current, set())
    if target not in allowed:
        return False, f"非法状态转换: {current} -> {target}"
    if current in TERMINAL_STATES:
        return False, f"终态 {current} 不允许继续流转"
    return ensure_execution_assignment(task, target)


def next_manual_transition(task: dict[str, Any]) -> tuple[bool, dict[str, str]]:
    ensure_task_shape(task)
    current = canonicalize_state(task.get("state"))
    flow = MANUAL_ADVANCE_FLOW.get(current)
    if not flow:
        return False, {"reason": f"任务状态 {current} 不支持手动推进"}
    next_state, from_org, to_org, default_remark = flow
    if next_state in EXECUTION_STATES:
        ok, reason = ensure_execution_assignment(task, next_state)
        if not ok:
            return False, {"reason": reason}
        execution_org = resolve_execution_org(task)
        from_org = execution_org if from_org == "__execution__" else from_org
        to_org = execution_org if to_org == "__execution__" else to_org
    else:
        from_org = task.get("org", from_org) if from_org == "__execution__" else from_org
        to_org = task.get("org", to_org) if to_org == "__execution__" else to_org
    return True, {"next_state": next_state, "from_org": from_org, "to_org": to_org, "remark": default_remark}


def review_transition(task: dict[str, Any], action: str, comment: str = "") -> tuple[bool, dict[str, Any]]:
    ensure_task_shape(task)
    current = canonicalize_state(task.get("state"))
    action_name = (action or "").strip()
    if action_name not in {"approve", "reject"}:
        return False, {"reason": f"未知审批动作: {action_name}"}
    if current == TaskState.Menxia.value:
        if action_name == "approve":
            return True, {"new_state": TaskState.Assigned.value, "from_org": "门下省", "to_org": "尚书省", "now": "门下省准奏，移交尚书省派发", "remark": f"✅ 准奏：{comment or '门下省审议通过'}", "increment_review_round": False}
        return True, {"new_state": TaskState.Zhongshu.value, "from_org": "门下省", "to_org": "中书省", "now": "封驳退回中书省修订", "remark": f"🚫 封驳：{comment or '需要修改'}", "increment_review_round": True}
    if current == TaskState.Review.value:
        if action_name == "approve":
            return True, {"new_state": TaskState.Done.value, "from_org": "皇上", "to_org": "皇上", "now": "御批通过，任务完成", "remark": f"✅ 御批准奏：{comment or '审查通过'}", "increment_review_round": False}
        ok, reason = ensure_execution_assignment(task, TaskState.Doing.value)
        if not ok:
            return False, {"reason": reason}
        execution_org = resolve_execution_org(task)
        return True, {"new_state": TaskState.Doing.value, "from_org": "皇上", "to_org": execution_org, "now": "审查退回执行部门返工", "remark": f"↩️ 审查退回：{comment or '请返工修正'}", "increment_review_round": False}
    return False, {"reason": f"任务当前状态为 {current}，不支持审批"}


def authorize_transition(actor: ActorContext, task: dict[str, Any], new_state: str) -> tuple[bool, str]:
    owner = resolve_state_owner(task)
    actor_id = canonicalize_actor(actor.actor_id)
    if actor_id in HIGH_PRIVILEGE_ACTORS or actor_id == owner:
        return True, ""
    return False, f"actor {actor_id} 无权推进 {task.get('state')} -> {new_state}"


def authorize_review(actor: ActorContext, task: dict[str, Any], action: str) -> tuple[bool, str]:
    actor_id = canonicalize_actor(actor.actor_id)
    state = canonicalize_state(task.get("state"))
    if actor_id in HIGH_PRIVILEGE_ACTORS:
        return True, ""
    if state == TaskState.Menxia.value and actor_id == "menxia":
        return True, ""
    if state == TaskState.Review.value and actor_id == "emperor":
        return True, ""
    return False, f"actor {actor_id} 无权在状态 {state} 执行审批动作 {action}"


def authorize_progress(actor: ActorContext, task: dict[str, Any]) -> tuple[bool, str]:
    actor_id = canonicalize_actor(actor.actor_id)
    owner = resolve_state_owner(task)
    if actor_id in HIGH_PRIVILEGE_ACTORS or actor_id == owner:
        return True, ""
    return False, f"actor {actor_id} 无权上报当前状态 {task.get('state')} 的进度"


def authorize_todos(actor: ActorContext, task: dict[str, Any]) -> tuple[bool, str]:
    return authorize_progress(actor, task)


def authorize_task_action(actor: ActorContext, task: dict[str, Any], action: str) -> tuple[bool, str]:
    actor_id = canonicalize_actor(actor.actor_id)
    if actor_id in HIGH_PRIVILEGE_ACTORS or actor_id == "sili":
        if action == "resume" and canonicalize_state(task.get("state")) == TaskState.Cancelled.value:
            return False, "Cancelled 为真终态，不允许直接 resume"
        return True, ""
    return False, f"actor {actor_id} 无权执行任务控制动作 {action}"


def authorize_scheduler(actor: ActorContext, action: str) -> tuple[bool, str]:
    actor_id = canonicalize_actor(actor.actor_id)
    if actor_id in SCHEDULER_ACTORS:
        return True, ""
    return False, f"actor {actor_id} 无权执行调度动作 {action}"


def authorize_dispatch(actor: ActorContext, task: dict[str, Any], target_agent: str) -> tuple[bool, str]:
    actor_id = canonicalize_actor(actor.actor_id)
    target = canonicalize_actor(target_agent)
    if actor_id in HIGH_PRIVILEGE_ACTORS:
        return True, ""
    owner = resolve_state_owner(task)
    if actor_id != owner:
        return False, f"actor {actor_id} 不是当前状态负责人，不能派发给 {target}"
    if not can_dispatch_to(actor_id, target):
        return False, f"actor {actor_id} 不允许派发给 {target}"
    return True, ""


def authorize_consultation(actor: ActorContext, task: dict[str, Any], target_agent: str) -> tuple[bool, str]:
    actor_id = canonicalize_actor(actor.actor_id)
    target = canonicalize_actor(target_agent)
    if actor_id in HIGH_PRIVILEGE_ACTORS:
        return True, ""
    owner = resolve_state_owner(task)
    if actor_id != owner:
        return False, f"actor {actor_id} 不是当前状态负责人，不能发起横向咨询"
    if target not in CONSULTATION_ALLOW_AGENTS.get(actor_id, set()):
        return False, f"actor {actor_id} 不允许咨询 {target}"
    return True, ""


def authorize_agent_wake(actor: ActorContext, target_agent: str) -> tuple[bool, str]:
    actor_id = canonicalize_actor(actor.actor_id)
    target = canonicalize_actor(target_agent)
    if actor_id in HIGH_PRIVILEGE_ACTORS or actor_id == "sili" or can_dispatch_to(actor_id, target):
        return True, ""
    return False, f"actor {actor_id} 无权唤醒 {target}"


def build_audit_entry(*, task_id: str, action: str, actor: ActorContext, allowed: bool, from_state: str = "", to_state: str = "", target_agent: str = "", deny_reason: str = "", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized_payload = payload or {}
    payload_json = json.dumps(normalized_payload, ensure_ascii=False, sort_keys=True)
    return {
        "audit_id": str(uuid.uuid4()),
        "ts": utc_now_iso(),
        "request_id": actor.request_id or str(uuid.uuid4()),
        "task_id": task_id,
        "action": action,
        "actor_id": actor.actor_id,
        "actor_type": actor.actor_type,
        "source": actor.source,
        "signature_verified": actor.signature_verified,
        "from_state": canonicalize_state(from_state),
        "to_state": canonicalize_state(to_state),
        "target_agent": canonicalize_actor(target_agent),
        "allowed": allowed,
        "deny_reason": deny_reason,
        "policy_version": POLICY_VERSION,
        "payload": normalized_payload,
        "payload_hash": hashlib.sha256(payload_json.encode("utf-8")).hexdigest()[:16],
        "payload_summary": payload_json[:400],
    }


def build_dispatch_key(
    task_id: str,
    state: str,
    version: int,
    agent: str,
    *,
    manual: bool = False,
    request_id: str = "",
) -> str:
    mode = "manual" if manual else "auto"
    suffix = f":{request_id}" if manual and request_id else ""
    return f"{mode}:{task_id}:{canonicalize_state(state)}:v{max(1, int(version or 1))}:{canonicalize_actor(agent)}{suffix}"


def build_consult_key(task_id: str, state: str, version: int, source_agent: str, target_agent: str, request_id: str = "") -> str:
    suffix = f":{request_id}" if request_id else ""
    return (
        f"consult:{task_id}:{canonicalize_state(state)}:v{max(1, int(version or 1))}:"
        f"{canonicalize_actor(source_agent)}:{canonicalize_actor(target_agent)}{suffix}"
    )
