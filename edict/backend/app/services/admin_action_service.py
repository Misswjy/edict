"""Compatibility write-side services for model/channel/wake admin actions."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from .agent_config_service import (
    SAFE_NAME_RE,
    build_agent_config_payload,
    data_dir,
    now_iso,
    openclaw_home,
    read_json,
)
from .audit_service import record_task_audit
from .local_store import atomic_json_update, atomic_json_write
from ..task_contract import ActorContext, authorize_agent_wake


class AdminActionService:
    def __init__(
        self,
        *,
        project_root_override: Path | None = None,
        openclaw_home_override: Path | None = None,
        runner=None,
        thread_factory=None,
    ):
        self.project_root_override = project_root_override
        self.openclaw_home_override = openclaw_home_override
        self.runner = runner or subprocess.run
        self.thread_factory = thread_factory or threading.Thread

    @property
    def _data_dir(self) -> Path:
        return data_dir(project_root_override=self.project_root_override)

    @property
    def _openclaw_home(self) -> Path:
        return openclaw_home(openclaw_home_override=self.openclaw_home_override)

    @property
    def _openclaw_cfg(self) -> Path:
        return self._openclaw_home / "openclaw.json"

    async def set_model(self, agent_id: str, model: str, actor: ActorContext) -> dict[str, Any]:
        agent_id = str(agent_id or "").strip()
        model = str(model or "").strip()
        if not SAFE_NAME_RE.match(agent_id):
            deny_reason = f"agentId 含非法字符: {agent_id}"
            await record_task_audit(task_id="", action="config.set_model", actor=actor, allowed=False, target_agent=agent_id, deny_reason=deny_reason)
            return {"ok": False, "error": deny_reason}
        if not model:
            deny_reason = "model required"
            await record_task_audit(task_id="", action="config.set_model", actor=actor, allowed=False, target_agent=agent_id, deny_reason=deny_reason)
            return {"ok": False, "error": deny_reason}

        cfg = read_json(self._openclaw_cfg, {})
        agents_list = cfg.get("agents", {}).get("list", [])
        default_model = str(cfg.get("agents", {}).get("defaults", {}).get("model", {}).get("primary", "") or "")

        selected = None
        for agent in agents_list:
            if agent.get("id") == agent_id:
                selected = agent
                break
        if selected is None:
            deny_reason = f"agent {agent_id} not found"
            await record_task_audit(task_id="", action="config.set_model", actor=actor, allowed=False, target_agent=agent_id, deny_reason=deny_reason)
            return {"ok": False, "error": deny_reason}

        old_model = selected.get("model", default_model)
        if model == default_model:
            selected.pop("model", None)
        else:
            selected["model"] = model

        self._openclaw_cfg.parent.mkdir(parents=True, exist_ok=True)
        backup_path = self._openclaw_cfg.parent / f"openclaw.json.bak.model-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        restart_ok = False
        rolled_back = False
        errors: list[dict[str, Any]] = []

        if self._openclaw_cfg.exists():
            shutil.copy2(self._openclaw_cfg, backup_path)
        cfg.setdefault("agents", {})["list"] = agents_list
        atomic_json_write(self._openclaw_cfg, cfg)
        self._cleanup_model_backups()

        try:
            result = self.runner(["openclaw", "gateway", "restart"], capture_output=True, text=True, timeout=30)
            restart_ok = getattr(result, "returncode", 1) == 0
            if not restart_ok:
                errors.append({"stage": "gateway_restart", "stderr": (getattr(result, "stderr", "") or getattr(result, "stdout", ""))[:200]})
        except Exception as exc:
            errors.append({"stage": "gateway_restart", "error": str(exc)})

        if errors:
            if backup_path.exists():
                shutil.copy2(backup_path, self._openclaw_cfg)
            rolled_back = True

        entry = {
            "at": datetime.now().isoformat(),
            "agentId": agent_id,
            "oldModel": old_model,
            "newModel": model,
            "rolledBack": rolled_back,
        }
        atomic_json_update(self._data_dir / "model_change_log.json", lambda current: (list(current or []) + [entry])[-200:], default=[])
        atomic_json_write(
            self._data_dir / "last_model_change_result.json",
            {
                "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "applied": [entry],
                "errors": errors,
                "gatewayRestarted": restart_ok,
                "rolledBack": rolled_back,
            },
        )

        await record_task_audit(
            task_id="",
            action="config.set_model",
            actor=actor,
            allowed=not rolled_back,
            target_agent=agent_id,
            deny_reason="gateway restart failed" if rolled_back else "",
            payload={"oldModel": old_model, "newModel": model, "gatewayRestarted": restart_ok, "rolledBack": rolled_back},
        )
        if rolled_back:
            return {"ok": False, "error": "模型变更已回滚，请检查 gateway 状态", "rolledBack": True}
        return {"ok": True, "message": f"Queued: {agent_id} → {model}", "gatewayRestarted": restart_ok}

    async def set_dispatch_channel(self, channel: str, actor: ActorContext) -> dict[str, Any]:
        channel = str(channel or "").strip()
        allowed_channels = {"feishu", "telegram", "wecom", "signal", "tui", "discord", "slack"}
        if channel not in allowed_channels:
            deny_reason = f"channel must be one of: {', '.join(sorted(allowed_channels))}"
            await record_task_audit(task_id="", action="config.set_dispatch_channel", actor=actor, allowed=False, deny_reason=deny_reason, payload={"channel": channel})
            return {"ok": False, "error": deny_reason}

        payload = build_agent_config_payload(
            project_root_override=self.project_root_override,
            openclaw_home_override=self.openclaw_home_override,
        )
        payload["dispatchChannel"] = channel
        atomic_json_write(self._data_dir / "agent_config.json", payload)
        await record_task_audit(task_id="", action="config.set_dispatch_channel", actor=actor, allowed=True, payload={"channel": channel})
        return {"ok": True, "message": f"派发渠道已切换为 {channel}"}

    async def wake_agent(self, agent_id: str, message: str, actor: ActorContext, task_id: str = "") -> dict[str, Any]:
        agent_id = str(agent_id or "").strip()
        message = str(message or "").strip()
        if not SAFE_NAME_RE.match(agent_id):
            deny_reason = f"agent_id 非法: {agent_id}"
            await record_task_audit(task_id=task_id, action="agent.wake", actor=actor, allowed=False, target_agent=agent_id, deny_reason=deny_reason)
            return {"ok": False, "error": deny_reason}

        allowed, deny_reason = authorize_agent_wake(actor, agent_id)
        if not allowed:
            await record_task_audit(task_id=task_id, action="agent.wake", actor=actor, allowed=False, target_agent=agent_id, deny_reason=deny_reason)
            return {"ok": False, "error": deny_reason}

        workspace = self._openclaw_home / f"workspace-{agent_id}"
        if not workspace.exists():
            deny_reason = f"{agent_id} 工作空间不存在，请先配置"
            await record_task_audit(task_id=task_id, action="agent.wake", actor=actor, allowed=False, target_agent=agent_id, deny_reason=deny_reason)
            return {"ok": False, "error": deny_reason}

        if not self._gateway_alive():
            deny_reason = "Gateway 未启动，请先运行 openclaw gateway start"
            await record_task_audit(task_id=task_id, action="agent.wake", actor=actor, allowed=False, target_agent=agent_id, deny_reason=deny_reason)
            return {"ok": False, "error": deny_reason}

        wake_message = message or f"🔔 系统心跳检测 — 请回复 OK 确认在线。当前时间: {now_iso()}"

        def _do_wake() -> None:
            try:
                self.runner(["openclaw", "agent", "--agent", agent_id, "-m", wake_message, "--timeout", "120"], capture_output=True, text=True, timeout=130)
            except Exception:
                return

        self.thread_factory(target=_do_wake, daemon=True).start()
        await record_task_audit(task_id=task_id, action="agent.wake", actor=actor, allowed=True, target_agent=agent_id, payload={"message": wake_message[:200]})
        return {"ok": True, "message": f"{agent_id} 唤醒指令已发出，约10-30秒后生效"}

    def _gateway_alive(self) -> bool:
        try:
            result = self.runner(["pgrep", "-f", "openclaw-gateway"], capture_output=True, text=True, timeout=5)
            return getattr(result, "returncode", 1) == 0
        except Exception:
            return False

    def _cleanup_model_backups(self, max_backups: int = 10) -> None:
        backups = sorted(self._openclaw_cfg.parent.glob("openclaw.json.bak.model-*"))
        for old in backups[:-max_backups]:
            try:
                old.unlink()
            except OSError:
                pass
