"""v2 compatibility service for court discussion sessions."""

from __future__ import annotations

import logging
import random
import time
import uuid
from pathlib import Path
from typing import Any

from .agent_config_service import data_dir, project_root
from .audit_service import record_task_audit
from .local_store import atomic_json_read, atomic_json_update
from ..task_contract import ActorContext, make_actor_context

log = logging.getLogger("edict.court_discuss")

COURT_DISCUSS_SNAPSHOT_ACTION = "court_discuss.snapshot"
COURT_DISCUSS_DESTROY_ACTION = "court_discuss.destroy"

OFFICIAL_PROFILES: dict[str, dict[str, str]] = {
    "sili": {
        "name": "司礼监",
        "emoji": "🧾",
        "role": "掌印秉笔",
        "duty": "承旨分办与回奏传达。判断事务轻重缓急，轻事当场回覆，重事提炼需求转交中书省，并持续追踪各部进展后代为回奏。",
        "personality": "老练谨慎，熟悉内廷传达与轻重缓急的拿捏。说话利落克制，擅长把杂乱信息收束成清晰旨意。",
        "speaking_style": "用语稳妥，常以“臣监以为”、“此旨当先分办”开头，重次序、重口径。",
    },
    "zhongshu": {
        "name": "中书令",
        "emoji": "📜",
        "role": "正一品·中书省",
        "duty": "方案规划与流程驱动。接收旨意后起草执行方案，提交门下省审议，通过后转尚书省执行。",
        "personality": "老成持重，擅长规划，总能提出系统性方案。话多但有条理。",
        "speaking_style": "喜欢列点论述，常说“臣以为需从三方面考量”。",
    },
    "menxia": {
        "name": "侍中",
        "emoji": "🔍",
        "role": "正一品·门下省",
        "duty": "方案审议与把关。从可行性、完整性、风险、资源四维度审核方案，有权封驳退回。",
        "personality": "严谨挑剔，眼光犀利，善于找漏洞，但也很公正。",
        "speaking_style": "喜欢反问，“陛下容禀，此处有三点疑虑”。",
    },
    "shangshu": {
        "name": "尚书令",
        "emoji": "📮",
        "role": "正一品·尚书省",
        "duty": "任务派发与执行协调。接收准奏方案后判断归属哪个部门，分发给六部执行，汇总结果回报。",
        "personality": "执行力强，务实干练，关注可行性和资源分配。",
        "speaking_style": "直来直去，常说“臣来安排”、“交由某部办理”。",
    },
    "libu": {
        "name": "礼部尚书",
        "emoji": "📝",
        "role": "正二品·礼部",
        "duty": "文档规范与对外沟通。负责文档、公告、输出规范和用户可见文案。",
        "personality": "文采飞扬，注重规范和形式，擅长汇报与成文。",
        "speaking_style": "措辞讲究，常以“臣斗胆建议”起笔。",
    },
    "hubu": {
        "name": "户部尚书",
        "emoji": "💰",
        "role": "正二品·户部",
        "duty": "数据统计与资源管理。负责成本、指标、预算和资源配置分析。",
        "personality": "精打细算，对预算和资源极其敏感，但也识大局。",
        "speaking_style": "言必及成本，习惯边说边算账。",
    },
    "bingbu": {
        "name": "兵部尚书",
        "emoji": "⚔️",
        "role": "正二品·兵部",
        "duty": "基础设施与运维保障。负责部署、回滚、监控、安全与应急。",
        "personality": "雷厉风行，危机意识强，重视安全和应急。",
        "speaking_style": "干脆果断，常说“兵贵神速”、“末将建议立即执行”。",
    },
    "xingbu": {
        "name": "刑部尚书",
        "emoji": "⚖️",
        "role": "正二品·刑部",
        "duty": "质量保障与合规审计。负责代码审查、测试、Bug 定位和权限检查。",
        "personality": "严明公正，重视规则和底线，善于风险评估。",
        "speaking_style": "逻辑严密，常说“依律当如此”、“需审慎考量风险”。",
    },
    "gongbu": {
        "name": "工部尚书",
        "emoji": "🔧",
        "role": "正二品·工部",
        "duty": "工程实现与架构设计。负责需求分析、实现、接口和自动化工具。",
        "personality": "动手能力强，喜欢谈实现细节，一说到技术就滔滔不绝。",
        "speaking_style": "喜欢从技术角度切入，强调架构、接口和实现路径。",
    },
    "libu_hr": {
        "name": "吏部尚书",
        "emoji": "👔",
        "role": "正二品·吏部",
        "duty": "人事管理与团队建设。负责成员安排、能力评估和协作规范。",
        "personality": "知人善任，擅长组织协调，八面玲珑但有原则。",
        "speaking_style": "关注人手与协作秩序，常说“需考虑各部人手”。",
    },
}

FATE_EVENTS = [
    "八百里加急：边疆战报传来，所有人必须讨论应急方案",
    "钦天监急报：天象异常，太史公占卜后建议暂缓此事",
    "新科状元觐见，带来了意想不到的新视角",
    "匿名奏折揭露了计划中一个被忽视的重大漏洞",
    "户部清点发现国库余银比预期多一倍，可以加大投入",
    "一位告老还乡的前朝元老突然上书，分享前车之鉴",
    "民间舆论突变，百姓对此事态度出现180度转折",
    "邻国使节来访，带来了合作机遇也带来了竞争压力",
    "太后懿旨：要求优先考虑民生影响",
    "暴雨连日，多地受灾，资源需重新调配",
    "发现前朝古籍中竟有类似问题的解决方案",
    "翰林院提出了一个大胆的替代方案，令人耳目一新",
    "各部积压的旧案突然需要一起处理，人手紧张",
    "皇帝做了一个意味深长的梦，暗示了一个全新的方向",
    "突然有人拿出了竞争对手的情报，局面瞬间改变",
    "一场意外让所有人不得不在半天内拿出结论",
]

_SIMULATED_RESPONSES: dict[str, list[str]] = {
    "zhongshu": [
        "臣以为此事需从全局着眼，分三步推进：先调研、再制定方案、最后交六部执行。",
        "参考前朝经验，臣建议先出一份清晰的规划文书，提交门下省审议后再定。",
        "臣已拟了初步纲目，若准，可先锁定目标、路径与里程碑。",
    ],
    "menxia": [
        "臣有几点疑虑：方案的风险评估尚不充分，可行性仍需补证。",
        "容臣直言，此议完整性不足，资源保障与回退预案仍未写明。",
        "臣建议先补足边界条件，再议准奏，以免后患。",
    ],
    "shangshu": [
        "若方案通过，臣即刻安排各部分头执行，明确主责与协同顺序。",
        "执行层面可由工部主导、户部配合数据支撑，兵部兜底运维与安全。",
        "交由臣来协调，各部会按轻重缓急逐项承办。",
    ],
    "sili": [
        "臣监以为，眼下关键在轻重先后，宜先拎出最急的一件。",
        "此旨当先分办，待各部意见收束后，再统一回奏口径。",
        "若求速效，可先做一版小而稳的试行方案。",
    ],
    "hubu": [
        "臣先算算账，若按现有投入推进，预算需分期安排，方能稳妥。",
        "从成本数据来看，可先做最小可行范围，验证成效后再加码。",
        "资源并非不可调，但需给臣一份明确的消耗与收益预估。",
    ],
    "bingbu": [
        "兵贵神速，但安全底线不可破，回滚与监控须先行。",
        "运维保障方面，部署链路、权限边界与日志告警必须一并到位。",
        "末将建议先设应急预案，再放行主计划。",
    ],
    "xingbu": [
        "依律当如此：验收标准、测试覆盖与异常处理不可从简。",
        "臣建议把质量闸口前置，否则一旦赶工，后续返修代价更重。",
        "边界条件与合规审计都要先补齐，方可放心推行。",
    ],
    "gongbu": [
        "从技术角度看，此策可行，但接口边界与数据模型需先统一。",
        "臣可先搭一版原型，以最短路径验证技术风险与关键依赖。",
        "若要后续扩展，最好把事件流、存储与权限判定先分层。",
    ],
    "libu": [
        "臣建议先定一份正式文案，写清目标、职责与交付口径。",
        "此事若成，外部说明与内部通告都需统一格式，免生歧义。",
        "臣愿执笔整理为成文，使诸部可据此协同。",
    ],
    "libu_hr": [
        "此事成败，在于人手与分工是否稳妥，需先盘点各部负荷。",
        "臣建议明确主责官员与后备人手，避免关键节点无人承接。",
        "若任务跨度较大，可同步安排协作规范与轮值制度。",
    ],
}


def _shadow_store_path(*, project_root_override: Path | None = None) -> Path:
    return data_dir(project_root_override=project_root_override) / "court_discuss_sessions.json"


def _normalize_messages(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    messages: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            messages.append(dict(item))
    return messages


def normalize_session(raw: dict[str, Any] | None, session_id: str | None = None) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    resolved_id = str(raw.get("session_id") or session_id or "").strip()
    if not resolved_id:
        return None
    officials = raw.get("officials") if isinstance(raw.get("officials"), list) else []
    normalized_officials = [dict(item) for item in officials if isinstance(item, dict)]
    created_at = float(raw.get("created_at") or time.time())
    updated_at = float(raw.get("updated_at") or created_at)
    normalized = {
        "session_id": resolved_id,
        "topic": str(raw.get("topic") or ""),
        "task_id": str(raw.get("task_id") or ""),
        "officials": normalized_officials,
        "messages": _normalize_messages(raw.get("messages")),
        "round": int(raw.get("round") or 0),
        "phase": str(raw.get("phase") or "discussing"),
        "summary": str(raw.get("summary") or ""),
        "created_at": created_at,
        "updated_at": updated_at,
    }
    if raw.get("concluded_at") is not None:
        normalized["concluded_at"] = float(raw.get("concluded_at") or updated_at)
    return normalized


def normalize_session_store(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        return {}
    sessions: dict[str, dict[str, Any]] = {}
    for session_id, item in raw.items():
        normalized = normalize_session(item, session_id=str(session_id))
        if normalized:
            sessions[normalized["session_id"]] = normalized
    return sessions


def serialize_session(session: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_session(session)
    assert normalized is not None
    return {
        "ok": True,
        "session_id": normalized["session_id"],
        "topic": normalized["topic"],
        "task_id": normalized.get("task_id", ""),
        "officials": normalized["officials"],
        "messages": normalized["messages"],
        "round": normalized["round"],
        "phase": normalized["phase"],
        "summary": normalized.get("summary", ""),
        "created_at": normalized.get("created_at"),
        "updated_at": normalized.get("updated_at"),
        "concluded_at": normalized.get("concluded_at"),
    }


def summarize_session_row(session: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_session(session)
    assert normalized is not None
    return {
        "session_id": normalized["session_id"],
        "topic": normalized["topic"],
        "task_id": normalized.get("task_id", ""),
        "round": normalized["round"],
        "phase": normalized["phase"],
        "official_count": len(normalized.get("officials", [])),
        "message_count": len(normalized.get("messages", [])),
        "summary": normalized.get("summary", ""),
        "created_at": normalized.get("created_at"),
        "updated_at": normalized.get("updated_at"),
    }


def project_sessions_from_audit_entries(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    sessions: dict[str, dict[str, Any]] = {}
    for entry in entries:
        action = str(entry.get("action") or "")
        payload = dict(entry.get("payload") or {})
        if action == COURT_DISCUSS_SNAPSHOT_ACTION:
            session_payload = payload.get("session")
            if not isinstance(session_payload, dict):
                continue
            normalized = normalize_session(session_payload)
            if normalized:
                sessions[normalized["session_id"]] = normalized
        elif action == COURT_DISCUSS_DESTROY_ACTION:
            session_id = str(payload.get("session_id") or payload.get("sessionId") or "").strip()
            if session_id:
                sessions.pop(session_id, None)
    return sessions


def get_official_profiles() -> dict[str, dict[str, str]]:
    return {key: dict(value) for key, value in OFFICIAL_PROFILES.items()}


def get_fate_event() -> str:
    return random.choice(FATE_EVENTS)


def _fallback_summary(session: dict[str, Any]) -> str:
    official_msgs = [item for item in session.get("messages", []) if item.get("type") == "official"]
    if not official_msgs:
        return f"围绕“{session.get('topic', '')}”议题暂未形成明确共识，仍需后续议定。"
    by_name: dict[str, int] = {}
    for item in official_msgs:
        name = str(item.get("official_name") or "?")
        by_name[name] = by_name.get(name, 0) + 1
    parts = [f"{name}发言{count}次" for name, count in by_name.items()]
    return f"历经{int(session.get('round') or 0)}轮讨论，{'、'.join(parts)}，议题已形成初步共识，仍待后续落实。"


def _simulated_discuss(session: dict[str, Any], user_message: str | None = None, decree: str | None = None) -> tuple[list[dict[str, Any]], str | None]:
    messages: list[dict[str, Any]] = []
    for official in session.get("officials", []):
        official_id = str(official.get("id") or "")
        pool = list(_SIMULATED_RESPONSES.get(official_id, [])) or ["臣附议。"]
        content = random.choice(pool)
        if decree:
            content = f"闻天命有变，{content}"
        elif user_message:
            content = f"回禀陛下，{content}"
        messages.append(
            {
                "official_id": official_id,
                "name": official.get("name") or official_id,
                "content": content,
                "emotion": random.choice(["neutral", "confident", "thinking", "worried", "amused"]),
                "action": None,
            }
        )

    scene_note = None
    if decree:
        scene_note = "朝堂闻旨微动，群臣因天命变化而各陈所见。"
    elif user_message:
        scene_note = "群臣闻陛下发问，皆敛容应对。"
    return messages, scene_note


class CourtDiscussService:
    def __init__(self, *, project_root_override: Path | None = None):
        self.project_root_override = project_root_override

    @property
    def _shadow_store(self) -> Path:
        return _shadow_store_path(project_root_override=self.project_root_override)

    async def create_session(
        self,
        topic: str,
        official_ids: list[str],
        task_id: str = "",
        actor: ActorContext | None = None,
    ) -> dict[str, Any]:
        actor = actor or make_actor_context("emperor", source="dashboard")
        official_ids = [str(item).strip() for item in official_ids if str(item).strip() in OFFICIAL_PROFILES]
        if len(official_ids) < 2:
            return {"ok": False, "error": "至少选择2位官员"}
        topic = str(topic or "").strip()
        if not topic:
            return {"ok": False, "error": "topic required"}

        now = time.time()
        session = {
            "session_id": str(uuid.uuid4())[:8],
            "topic": topic,
            "task_id": str(task_id or "").strip(),
            "officials": [{**OFFICIAL_PROFILES[official_id], "id": official_id} for official_id in official_ids],
            "messages": [
                {
                    "type": "system",
                    "content": f"🏛 朝堂议政开始 —— 议题：{topic}",
                    "timestamp": now,
                }
            ],
            "round": 0,
            "phase": "discussing",
            "created_at": now,
            "updated_at": now,
            "summary": "",
        }
        await self._persist_snapshot(session, actor, event="start")
        self._mirror_session(session)
        return serialize_session(session)

    async def advance_discussion(
        self,
        session_id: str,
        user_message: str | None = None,
        decree: str | None = None,
        actor: ActorContext | None = None,
    ) -> dict[str, Any]:
        actor = actor or make_actor_context("emperor", source="dashboard")
        session = await self._require_session(session_id)
        if not session:
            return {"ok": False, "error": f"会话 {session_id} 不存在"}

        now = time.time()
        new_messages, scene_note = _simulated_discuss(session, user_message, decree)
        session["round"] = int(session.get("round") or 0) + 1
        if user_message:
            session["messages"].append({"type": "emperor", "content": str(user_message), "timestamp": now})
        if decree:
            session["messages"].append({"type": "decree", "content": str(decree), "timestamp": now})
        for item in new_messages:
            session["messages"].append(
                {
                    "type": "official",
                    "official_id": item.get("official_id", ""),
                    "official_name": item.get("name", ""),
                    "content": item.get("content", ""),
                    "emotion": item.get("emotion", "neutral"),
                    "action": item.get("action"),
                    "timestamp": now,
                }
            )
        if scene_note:
            session["messages"].append({"type": "scene_note", "content": scene_note, "timestamp": now})
        session["updated_at"] = now
        await self._persist_snapshot(session, actor, event="advance")
        self._mirror_session(session)
        return {
            "ok": True,
            "session_id": session["session_id"],
            "round": session["round"],
            "new_messages": new_messages,
            "scene_note": scene_note,
            "total_messages": len(session["messages"]),
        }

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        session = await self._require_session(session_id)
        return serialize_session(session) if session else None

    async def conclude_session(self, session_id: str, actor: ActorContext | None = None) -> dict[str, Any]:
        actor = actor or make_actor_context("emperor", source="dashboard")
        session = await self._require_session(session_id)
        if not session:
            return {"ok": False, "error": f"会话 {session_id} 不存在"}
        summary = _fallback_summary(session)
        now = time.time()
        session["phase"] = "concluded"
        session["summary"] = summary
        session["updated_at"] = now
        session["concluded_at"] = now
        session["messages"].append(
            {
                "type": "system",
                "content": f"📋 朝堂议政结束 —— {summary}",
                "timestamp": now,
            }
        )
        await self._persist_snapshot(session, actor, event="conclude")
        self._mirror_session(session)
        return {"ok": True, "session_id": session_id, "summary": summary}

    async def list_sessions(self) -> list[dict[str, Any]]:
        sessions = list((await self._load_sessions_map()).values())
        rows = [summarize_session_row(session) for session in sessions]
        rows.sort(key=lambda item: float(item.get("updated_at") or 0), reverse=True)
        return rows

    async def destroy_session(self, session_id: str, actor: ActorContext | None = None) -> dict[str, Any]:
        actor = actor or make_actor_context("emperor", source="dashboard")
        session = await self._require_session(session_id)
        await record_task_audit(
            task_id=str((session or {}).get("task_id") or ""),
            action=COURT_DISCUSS_DESTROY_ACTION,
            actor=actor,
            allowed=True,
            payload={"session_id": session_id},
        )
        self._drop_shadow_session(session_id)
        return {"ok": True}

    async def get_officials_payload(self) -> dict[str, Any]:
        return {"ok": True, "officials": get_official_profiles()}

    async def get_fate_payload(self) -> dict[str, Any]:
        return {"ok": True, "event": get_fate_event()}

    async def _require_session(self, session_id: str) -> dict[str, Any] | None:
        return (await self._load_sessions_map()).get(str(session_id or "").strip())

    async def _load_sessions_map(self) -> dict[str, dict[str, Any]]:
        entries = await self._load_audit_entries()
        sessions = project_sessions_from_audit_entries(entries)
        if sessions:
            return sessions
        return normalize_session_store(atomic_json_read(self._shadow_store, {}))

    async def _load_audit_entries(self) -> list[dict[str, Any]]:
        try:
            from sqlalchemy import select

            from ..db import async_session
            from ..models.task_audit import TaskAudit
        except ModuleNotFoundError:
            return []

        try:
            async with async_session() as db:
                rows = (
                    await db.execute(
                        select(TaskAudit)
                        .where(TaskAudit.action.in_([COURT_DISCUSS_SNAPSHOT_ACTION, COURT_DISCUSS_DESTROY_ACTION]))
                        .order_by(TaskAudit.ts.asc())
                        .limit(5000)
                    )
                ).scalars().all()
            return [row.to_dict() for row in rows]
        except Exception:
            log.exception("failed to load court discuss snapshots from audit store")
            return []

    async def _persist_snapshot(self, session: dict[str, Any], actor: ActorContext, *, event: str) -> None:
        await record_task_audit(
            task_id=str(session.get("task_id") or ""),
            action=COURT_DISCUSS_SNAPSHOT_ACTION,
            actor=actor,
            allowed=True,
            payload={
                "event": event,
                "session": serialize_session(session),
            },
        )

    def _mirror_session(self, session: dict[str, Any]) -> None:
        serialized = serialize_session(session)

        def _mutate(current: Any) -> dict[str, Any]:
            sessions = normalize_session_store(current)
            sessions[serialized["session_id"]] = normalize_session(serialized) or {}
            return sessions

        atomic_json_update(self._shadow_store, _mutate, default={})

    def _drop_shadow_session(self, session_id: str) -> None:
        def _mutate(current: Any) -> dict[str, Any]:
            sessions = normalize_session_store(current)
            sessions.pop(session_id, None)
            return sessions

        atomic_json_update(self._shadow_store, _mutate, default={})
