"""Legacy skill management extracted from server.py."""

from __future__ import annotations

import json
import sys


def _srv():
    module = sys.modules.get("server")
    if module is not None and hasattr(module, "OCLAW_HOME"):
        return module
    module = sys.modules.get("__main__")
    if module is not None and str(getattr(module, "__file__", "")).endswith("dashboard/server.py"):
        return module
    import server as module  # type: ignore

    return module


def read_skill_content(agent_id, skill_name):
    srv = _srv()
    if not srv._SAFE_NAME_RE.match(agent_id) or not srv._SAFE_NAME_RE.match(skill_name):
        return {"ok": False, "error": "参数含非法字符"}
    cfg = srv.read_json(srv.DATA / "agent_config.json", {})
    agents = cfg.get("agents", [])
    agent = next((item for item in agents if item.get("id") == agent_id), None)
    if not agent:
        return {"ok": False, "error": f"Agent {agent_id} 不存在"}
    skill = next((item for item in agent.get("skills", []) if item.get("name") == skill_name), None)
    if not skill:
        return {"ok": False, "error": f"技能 {skill_name} 不存在"}
    skill_path = srv.pathlib.Path(skill.get("path", "")).resolve()
    allowed_roots = (srv.OCLAW_HOME.resolve(), srv.BASE.parent.resolve())
    if not any(str(skill_path).startswith(str(root)) for root in allowed_roots):
        return {"ok": False, "error": "路径不在允许的目录范围内"}
    if not skill_path.exists():
        return {"ok": True, "name": skill_name, "agent": agent_id, "content": "(SKILL.md 文件不存在)", "path": str(skill_path)}
    try:
        content = skill_path.read_text()
        return {"ok": True, "name": skill_name, "agent": agent_id, "content": content, "path": str(skill_path)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def add_skill_to_agent(agent_id, skill_name, description, trigger=""):
    srv = _srv()
    if not srv._SAFE_NAME_RE.match(skill_name):
        return {"ok": False, "error": f"skill_name 含非法字符: {skill_name}"}
    if not srv._SAFE_NAME_RE.match(agent_id):
        return {"ok": False, "error": f"agentId 含非法字符: {agent_id}"}
    workspace = srv.OCLAW_HOME / f"workspace-{agent_id}" / "skills" / skill_name
    workspace.mkdir(parents=True, exist_ok=True)
    skill_md = workspace / "SKILL.md"
    desc_line = description or skill_name
    trigger_section = f"\n## 触发条件\n{trigger}\n" if trigger else ""
    template = (
        f"---\nname: {skill_name}\ndescription: {desc_line}\n---\n\n"
        f"# {skill_name}\n\n{desc_line}\n{trigger_section}\n"
        "## 输入\n\n<!-- 说明此技能接收什么输入 -->\n\n"
        "## 处理流程\n\n1. 步骤一\n2. 步骤二\n\n"
        "## 输出规范\n\n<!-- 说明产出物格式与交付要求 -->\n\n"
        "## 注意事项\n\n- (在此补充约束、限制或特殊规则)\n"
    )
    skill_md.write_text(template)
    try:
        srv.subprocess.run(["python3", str(srv.SCRIPTS / "sync_agent_config.py")], timeout=10)
    except Exception:
        pass
    return {"ok": True, "message": f"技能 {skill_name} 已添加到 {agent_id}", "path": str(skill_md)}


def add_remote_skill(agent_id, skill_name, source_url, description=""):
    srv = _srv()
    if not srv._SAFE_NAME_RE.match(agent_id):
        return {"ok": False, "error": f"agentId 含非法字符: {agent_id}"}
    if not srv._SAFE_NAME_RE.match(skill_name):
        return {"ok": False, "error": f"skillName 含非法字符: {skill_name}"}
    if not source_url or not isinstance(source_url, str):
        return {"ok": False, "error": "sourceUrl 必须是有效的字符串"}
    source_url = source_url.strip()
    cfg = srv.read_json(srv.DATA / "agent_config.json", {})
    agents = cfg.get("agents", [])
    if not any(item.get("id") == agent_id for item in agents):
        return {"ok": False, "error": f"Agent {agent_id} 不存在"}

    try:
        if source_url.startswith("http://") or source_url.startswith("https://"):
            if not srv.validate_url(source_url, allowed_schemes=("https",)):
                return {"ok": False, "error": "URL 无效或不安全（仅支持 HTTPS）"}
            req = srv.Request(source_url, headers={"User-Agent": "OpenClaw-SkillManager/1.0"})
            try:
                resp = srv.urlopen(req, timeout=10)
                content = resp.read(10 * 1024 * 1024).decode("utf-8")
                if len(content) > 10 * 1024 * 1024:
                    return {"ok": False, "error": "文件过大（最大 10MB）"}
            except Exception as exc:
                return {"ok": False, "error": f"URL 无法访问: {str(exc)[:100]}"}
        elif source_url.startswith("file://"):
            local_path = srv.pathlib.Path(source_url[7:])
            if not local_path.exists():
                return {"ok": False, "error": f"本地文件不存在: {local_path}"}
            content = local_path.read_text()
        elif source_url.startswith("/") or source_url.startswith("."):
            local_path = srv.pathlib.Path(source_url).resolve()
            if not local_path.exists():
                return {"ok": False, "error": f"本地文件不存在: {local_path}"}
            allowed_roots = (srv.OCLAW_HOME.resolve(), srv.BASE.parent.resolve())
            if not any(str(local_path).startswith(str(root)) for root in allowed_roots):
                return {"ok": False, "error": "路径不在允许的目录范围内"}
            content = local_path.read_text()
        else:
            return {"ok": False, "error": "不支持的 URL 格式（仅支持 https://, file://, 或本地路径）"}
    except Exception as exc:
        return {"ok": False, "error": f"文件读取失败: {str(exc)[:100]}"}

    if not content.startswith("---"):
        return {"ok": False, "error": "文件格式无效（缺少 YAML frontmatter）"}
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {"ok": False, "error": "文件格式无效（YAML frontmatter 结构错误）"}
    if "name:" not in content[:500]:
        return {"ok": False, "error": "文件格式无效：frontmatter 缺少 name 字段"}
    try:
        import yaml

        yaml.safe_load(parts[1])
    except ImportError:
        pass
    except Exception as exc:
        return {"ok": False, "error": f"YAML 格式无效: {str(exc)[:100]}"}

    workspace = srv.OCLAW_HOME / f"workspace-{agent_id}" / "skills" / skill_name
    workspace.mkdir(parents=True, exist_ok=True)
    skill_md = workspace / "SKILL.md"
    skill_md.write_text(content)
    source_info = {
        "skillName": skill_name,
        "sourceUrl": source_url,
        "description": description,
        "addedAt": srv.now_iso(),
        "lastUpdated": srv.now_iso(),
        "checksum": _compute_checksum(content),
        "status": "valid",
    }
    source_json = workspace / ".source.json"
    source_json.write_text(json.dumps(source_info, ensure_ascii=False, indent=2))
    try:
        srv.subprocess.run(["python3", str(srv.SCRIPTS / "sync_agent_config.py")], timeout=10)
    except Exception:
        pass
    return {
        "ok": True,
        "message": f"技能 {skill_name} 已从远程源添加到 {agent_id}",
        "skillName": skill_name,
        "agentId": agent_id,
        "source": source_url,
        "localPath": str(skill_md),
        "size": len(content),
        "addedAt": srv.now_iso(),
    }


def get_remote_skills_list():
    srv = _srv()
    remote_skills = []
    for ws_dir in srv.OCLAW_HOME.glob("workspace-*"):
        agent_id = ws_dir.name.replace("workspace-", "")
        skills_dir = ws_dir / "skills"
        if not skills_dir.exists():
            continue
        for skill_dir in skills_dir.iterdir():
            if not skill_dir.is_dir():
                continue
            skill_name = skill_dir.name
            source_json = skill_dir / ".source.json"
            skill_md = skill_dir / "SKILL.md"
            if not source_json.exists():
                continue
            try:
                source_info = json.loads(source_json.read_text())
                status = "valid" if skill_md.exists() else "not-found"
                remote_skills.append(
                    {
                        "skillName": skill_name,
                        "agentId": agent_id,
                        "sourceUrl": source_info.get("sourceUrl", ""),
                        "description": source_info.get("description", ""),
                        "localPath": str(skill_md),
                        "addedAt": source_info.get("addedAt", ""),
                        "lastUpdated": source_info.get("lastUpdated", ""),
                        "status": status,
                    }
                )
            except Exception:
                pass
    return {"ok": True, "remoteSkills": remote_skills, "count": len(remote_skills), "listedAt": srv.now_iso()}


def update_remote_skill(agent_id, skill_name):
    srv = _srv()
    if not srv._SAFE_NAME_RE.match(agent_id):
        return {"ok": False, "error": f"agentId 含非法字符: {agent_id}"}
    if not srv._SAFE_NAME_RE.match(skill_name):
        return {"ok": False, "error": f"skillName 含非法字符: {skill_name}"}
    workspace = srv.OCLAW_HOME / f"workspace-{agent_id}" / "skills" / skill_name
    source_json = workspace / ".source.json"
    if not source_json.exists():
        return {"ok": False, "error": f"技能 {skill_name} 不是远程 skill（无 .source.json）"}
    try:
        source_info = json.loads(source_json.read_text())
        source_url = source_info.get("sourceUrl", "")
        if not source_url:
            return {"ok": False, "error": "源 URL 不存在"}
        result = add_remote_skill(agent_id, skill_name, source_url, source_info.get("description", ""))
        if result["ok"]:
            result["message"] = "技能已更新"
            source_info_updated = json.loads(source_json.read_text())
            result["newVersion"] = source_info_updated.get("checksum", "unknown")
        return result
    except Exception as exc:
        return {"ok": False, "error": f"更新失败: {str(exc)[:100]}"}


def remove_remote_skill(agent_id, skill_name):
    srv = _srv()
    if not srv._SAFE_NAME_RE.match(agent_id):
        return {"ok": False, "error": f"agentId 含非法字符: {agent_id}"}
    if not srv._SAFE_NAME_RE.match(skill_name):
        return {"ok": False, "error": f"skillName 含非法字符: {skill_name}"}
    workspace = srv.OCLAW_HOME / f"workspace-{agent_id}" / "skills" / skill_name
    if not workspace.exists():
        return {"ok": False, "error": f"技能不存在: {skill_name}"}
    source_json = workspace / ".source.json"
    if not source_json.exists():
        return {"ok": False, "error": f"技能 {skill_name} 不是远程 skill，无法通过此 API 移除"}
    try:
        import shutil

        shutil.rmtree(workspace)
        try:
            srv.subprocess.run(["python3", str(srv.SCRIPTS / "sync_agent_config.py")], timeout=10)
        except Exception:
            pass
        return {"ok": True, "message": f"技能 {skill_name} 已从 {agent_id} 移除"}
    except Exception as exc:
        return {"ok": False, "error": f"移除失败: {str(exc)[:100]}"}


def _compute_checksum(content: str) -> str:
    import hashlib

    return hashlib.sha256(content.encode()).hexdigest()[:16]
