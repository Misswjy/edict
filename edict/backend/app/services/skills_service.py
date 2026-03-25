"""Compatibility write-side services for local and remote skill management."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from .agent_config_service import SAFE_NAME_RE, build_agent_config_payload, now_iso, openclaw_home, project_root, read_json
from .audit_service import record_task_audit
from .local_store import validate_url
from ..task_contract import ActorContext


class SkillsService:
    def __init__(self, *, project_root_override: Path | None = None, openclaw_home_override: Path | None = None):
        self.project_root_override = project_root_override
        self.openclaw_home_override = openclaw_home_override

    @property
    def _project_root(self) -> Path:
        return project_root() if self.project_root_override is None else self.project_root_override

    @property
    def _openclaw_home(self) -> Path:
        return openclaw_home(openclaw_home_override=self.openclaw_home_override)

    async def add_skill_to_agent(self, agent_id: str, skill_name: str, description: str, trigger: str, actor: ActorContext) -> dict[str, Any]:
        if not SAFE_NAME_RE.match(str(agent_id or "")):
            deny_reason = f"agentId 含非法字符: {agent_id}"
            await record_task_audit(task_id="", action="skill.add", actor=actor, allowed=False, target_agent=agent_id, deny_reason=deny_reason)
            return {"ok": False, "error": deny_reason}
        if not SAFE_NAME_RE.match(str(skill_name or "")):
            deny_reason = f"skill_name 含非法字符: {skill_name}"
            await record_task_audit(task_id="", action="skill.add", actor=actor, allowed=False, target_agent=agent_id, deny_reason=deny_reason)
            return {"ok": False, "error": deny_reason}

        workspace = self._openclaw_home / f"workspace-{agent_id}" / "skills" / skill_name
        workspace.mkdir(parents=True, exist_ok=True)
        skill_md = workspace / "SKILL.md"
        desc_line = description or skill_name
        trigger_section = f"\n## 触发条件\n{trigger}\n" if trigger else ""
        skill_md.write_text(
            (
                f"---\nname: {skill_name}\ndescription: {desc_line}\n---\n\n"
                f"# {skill_name}\n\n{desc_line}\n{trigger_section}\n"
                "## 输入\n\n<!-- 说明此技能接收什么输入 -->\n\n"
                "## 处理流程\n\n1. 步骤一\n2. 步骤二\n\n"
                "## 输出规范\n\n<!-- 说明产出物格式与交付要求 -->\n\n"
                "## 注意事项\n\n- (在此补充约束、限制或特殊规则)\n"
            ),
            encoding="utf-8",
        )
        await record_task_audit(task_id="", action="skill.add", actor=actor, allowed=True, target_agent=agent_id, payload={"skillName": skill_name, "path": str(skill_md)})
        return {"ok": True, "message": f"技能 {skill_name} 已添加到 {agent_id}", "path": str(skill_md)}

    async def add_remote_skill(self, agent_id: str, skill_name: str, source_url: str, description: str, actor: ActorContext) -> dict[str, Any]:
        agent_id = str(agent_id or "").strip()
        skill_name = str(skill_name or "").strip()
        source_url = str(source_url or "").strip()
        description = str(description or "").strip()

        if not SAFE_NAME_RE.match(agent_id):
            deny_reason = f"agentId 含非法字符: {agent_id}"
            await record_task_audit(task_id="", action="skill.remote.add", actor=actor, allowed=False, target_agent=agent_id, deny_reason=deny_reason)
            return {"ok": False, "error": deny_reason}
        if not SAFE_NAME_RE.match(skill_name):
            deny_reason = f"skillName 含非法字符: {skill_name}"
            await record_task_audit(task_id="", action="skill.remote.add", actor=actor, allowed=False, target_agent=agent_id, deny_reason=deny_reason)
            return {"ok": False, "error": deny_reason}
        if not source_url:
            deny_reason = "sourceUrl required"
            await record_task_audit(task_id="", action="skill.remote.add", actor=actor, allowed=False, target_agent=agent_id, deny_reason=deny_reason)
            return {"ok": False, "error": deny_reason}

        cfg = build_agent_config_payload(project_root_override=self.project_root_override, openclaw_home_override=self.openclaw_home_override)
        if not any(item.get("id") == agent_id for item in cfg.get("agents", [])):
            deny_reason = f"Agent {agent_id} 不存在"
            await record_task_audit(task_id="", action="skill.remote.add", actor=actor, allowed=False, target_agent=agent_id, deny_reason=deny_reason)
            return {"ok": False, "error": deny_reason}

        try:
            content = self._load_remote_skill_content(source_url)
        except ValueError as exc:
            await record_task_audit(task_id="", action="skill.remote.add", actor=actor, allowed=False, target_agent=agent_id, deny_reason=str(exc), payload={"sourceUrl": source_url})
            return {"ok": False, "error": str(exc)}

        validation_error = self._validate_skill_content(content)
        if validation_error:
            await record_task_audit(task_id="", action="skill.remote.add", actor=actor, allowed=False, target_agent=agent_id, deny_reason=validation_error, payload={"sourceUrl": source_url})
            return {"ok": False, "error": validation_error}

        workspace = self._openclaw_home / f"workspace-{agent_id}" / "skills" / skill_name
        workspace.mkdir(parents=True, exist_ok=True)
        skill_md = workspace / "SKILL.md"
        skill_md.write_text(content, encoding="utf-8")
        source_info = {
            "skillName": skill_name,
            "sourceUrl": source_url,
            "description": description,
            "addedAt": now_iso(),
            "lastUpdated": now_iso(),
            "checksum": hashlib.sha256(content.encode("utf-8")).hexdigest()[:16],
            "status": "valid",
        }
        (workspace / ".source.json").write_text(json.dumps(source_info, ensure_ascii=False, indent=2), encoding="utf-8")
        await record_task_audit(task_id="", action="skill.remote.add", actor=actor, allowed=True, target_agent=agent_id, payload={"skillName": skill_name, "sourceUrl": source_url})
        return {
            "ok": True,
            "message": f"技能 {skill_name} 已从远程源添加到 {agent_id}",
            "skillName": skill_name,
            "agentId": agent_id,
            "source": source_url,
            "localPath": str(skill_md),
            "size": len(content),
            "addedAt": now_iso(),
        }

    async def update_remote_skill(self, agent_id: str, skill_name: str, actor: ActorContext) -> dict[str, Any]:
        workspace = self._openclaw_home / f"workspace-{agent_id}" / "skills" / skill_name
        source_json = workspace / ".source.json"
        if not source_json.exists():
            deny_reason = f"技能 {skill_name} 不是远程 skill（无 .source.json）"
            await record_task_audit(task_id="", action="skill.remote.update", actor=actor, allowed=False, target_agent=agent_id, deny_reason=deny_reason)
            return {"ok": False, "error": deny_reason}
        source_info = read_json(source_json, {})
        result = await self.add_remote_skill(agent_id, skill_name, str(source_info.get("sourceUrl") or ""), str(source_info.get("description") or ""), actor)
        if result.get("ok"):
            result["message"] = "技能已更新"
            result["newVersion"] = read_json(source_json, {}).get("checksum", "unknown")
            await record_task_audit(task_id="", action="skill.remote.update", actor=actor, allowed=True, target_agent=agent_id, payload={"skillName": skill_name})
        return result

    async def remove_remote_skill(self, agent_id: str, skill_name: str, actor: ActorContext) -> dict[str, Any]:
        if not SAFE_NAME_RE.match(str(agent_id or "")):
            deny_reason = f"agentId 含非法字符: {agent_id}"
            await record_task_audit(task_id="", action="skill.remote.remove", actor=actor, allowed=False, target_agent=agent_id, deny_reason=deny_reason)
            return {"ok": False, "error": deny_reason}
        if not SAFE_NAME_RE.match(str(skill_name or "")):
            deny_reason = f"skillName 含非法字符: {skill_name}"
            await record_task_audit(task_id="", action="skill.remote.remove", actor=actor, allowed=False, target_agent=agent_id, deny_reason=deny_reason)
            return {"ok": False, "error": deny_reason}

        workspace = self._openclaw_home / f"workspace-{agent_id}" / "skills" / skill_name
        if not workspace.exists():
            deny_reason = f"技能不存在: {skill_name}"
            await record_task_audit(task_id="", action="skill.remote.remove", actor=actor, allowed=False, target_agent=agent_id, deny_reason=deny_reason)
            return {"ok": False, "error": deny_reason}
        if not (workspace / ".source.json").exists():
            deny_reason = f"技能 {skill_name} 不是远程 skill，无法通过此 API 移除"
            await record_task_audit(task_id="", action="skill.remote.remove", actor=actor, allowed=False, target_agent=agent_id, deny_reason=deny_reason)
            return {"ok": False, "error": deny_reason}
        shutil.rmtree(workspace)
        await record_task_audit(task_id="", action="skill.remote.remove", actor=actor, allowed=True, target_agent=agent_id, payload={"skillName": skill_name})
        return {"ok": True, "message": f"技能 {skill_name} 已从 {agent_id} 移除"}

    def _load_remote_skill_content(self, source_url: str) -> str:
        if source_url.startswith("http://") or source_url.startswith("https://"):
            if not validate_url(source_url, allowed_schemes=("https",)):
                raise ValueError("URL 无效或不安全（仅支持 HTTPS）")
            req = Request(source_url, headers={"User-Agent": "Edict-SkillManager/1.0"})
            with urlopen(req, timeout=10) as response:
                content = response.read(10 * 1024 * 1024).decode("utf-8")
            if len(content) > 10 * 1024 * 1024:
                raise ValueError("文件过大（最大 10MB）")
            return content
        if source_url.startswith("file://"):
            local_path = Path(source_url[7:]).expanduser().resolve()
            if not local_path.exists():
                raise ValueError(f"本地文件不存在: {local_path}")
            return local_path.read_text(encoding="utf-8")
        if source_url.startswith("/") or source_url.startswith("."):
            local_path = Path(source_url).expanduser().resolve()
            if not local_path.exists():
                raise ValueError(f"本地文件不存在: {local_path}")
            allowed_roots = (self._openclaw_home.resolve(), self._project_root.resolve())
            if not any(str(local_path).startswith(str(root)) for root in allowed_roots):
                raise ValueError("路径不在允许的目录范围内")
            return local_path.read_text(encoding="utf-8")
        raise ValueError("不支持的 URL 格式（仅支持 https://, file://, 或本地路径）")

    def _validate_skill_content(self, content: str) -> str:
        if not content.startswith("---"):
            return "文件格式无效（缺少 YAML frontmatter）"
        parts = content.split("---", 2)
        if len(parts) < 3:
            return "文件格式无效（YAML frontmatter 结构错误）"
        if "name:" not in content[:500]:
            return "文件格式无效：frontmatter 缺少 name 字段"
        return ""
