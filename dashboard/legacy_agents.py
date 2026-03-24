"""Legacy agent status and wake helpers extracted from server.py."""

from __future__ import annotations

import json
import sys

from app.generated.institution_schema import AGENT_DIRECTORY


def _srv():
    module = sys.modules.get("server")
    if module is not None and hasattr(module, "OCLAW_HOME"):
        return module
    module = sys.modules.get("__main__")
    if module is not None and str(getattr(module, "__file__", "")).endswith("dashboard/server.py"):
        return module
    import server as module  # type: ignore

    return module


def _check_gateway_alive():
    srv = _srv()
    try:
        result = srv.subprocess.run(["pgrep", "-f", "openclaw-gateway"], capture_output=True, text=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False


def _check_gateway_probe():
    try:
        from urllib.request import urlopen

        resp = urlopen("http://127.0.0.1:18789/", timeout=3)
        return resp.status == 200
    except Exception:
        return False


def _get_agent_session_status(agent_id):
    srv = _srv()
    sessions_file = srv.OCLAW_HOME / "agents" / agent_id / "sessions" / "sessions.json"
    if not sessions_file.exists():
        return 0, 0, False
    try:
        data = json.loads(sessions_file.read_text())
        if not isinstance(data, dict):
            return 0, 0, False
        session_count = len(data)
        last_ts = 0
        for value in data.values():
            ts = value.get("updatedAt", 0)
            if isinstance(ts, (int, float)) and ts > last_ts:
                last_ts = ts
        now_ms = int(srv.datetime.datetime.now().timestamp() * 1000)
        age_ms = now_ms - last_ts if last_ts else 9999999999
        is_busy = age_ms <= 2 * 60 * 1000
        return last_ts, session_count, is_busy
    except Exception:
        return 0, 0, False


def _check_agent_process(agent_id):
    srv = _srv()
    try:
        result = srv.subprocess.run(["pgrep", "-f", f"openclaw.*--agent.*{agent_id}"], capture_output=True, text=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False


def _check_agent_workspace(agent_id):
    srv = _srv()
    return (srv.OCLAW_HOME / f"workspace-{agent_id}").is_dir()


def get_agents_status():
    srv = _srv()
    gateway_alive = _check_gateway_alive()
    gateway_probe = _check_gateway_probe() if gateway_alive else False
    agents = []
    seen_ids = set()
    for dept in AGENT_DIRECTORY:
        aid = dept["id"]
        if aid in seen_ids:
            continue
        seen_ids.add(aid)
        has_workspace = _check_agent_workspace(aid)
        last_ts, sess_count, is_busy = _get_agent_session_status(aid)
        process_alive = _check_agent_process(aid)

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
            now_ms = int(srv.datetime.datetime.now().timestamp() * 1000)
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

        last_active_str = None
        if last_ts > 0:
            try:
                last_active_str = srv.datetime.datetime.fromtimestamp(last_ts / 1000).strftime("%m-%d %H:%M")
            except Exception:
                pass

        agents.append(
            {
                "id": aid,
                "label": dept["label"],
                "emoji": dept["emoji"],
                "role": dept["role"],
                "status": status,
                "statusLabel": status_label,
                "lastActive": last_active_str,
                "lastActiveTs": last_ts,
                "sessions": sess_count,
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
        "checkedAt": srv.now_iso(),
    }


def wake_agent(agent_id, message="", actor=None, task_id=""):
    srv = _srv()
    actor = actor or srv.make_actor_context("emperor", source="dashboard")
    if not srv._SAFE_NAME_RE.match(agent_id):
        srv._record_task_audit(task_id, "agent.wake", actor, False, target_agent=agent_id, deny_reason=f"agent_id 非法: {agent_id}")
        return {"ok": False, "error": f"agent_id 非法: {agent_id}"}
    allowed, deny_reason = srv.authorize_agent_wake(actor, agent_id)
    if not allowed:
        srv._record_task_audit(task_id, "agent.wake", actor, False, target_agent=agent_id, deny_reason=deny_reason)
        return {"ok": False, "error": deny_reason}
    if not _check_agent_workspace(agent_id):
        srv._record_task_audit(task_id, "agent.wake", actor, False, target_agent=agent_id, deny_reason=f"{agent_id} 工作空间不存在，请先配置")
        return {"ok": False, "error": f"{agent_id} 工作空间不存在，请先配置"}
    if not _check_gateway_alive():
        srv._record_task_audit(task_id, "agent.wake", actor, False, target_agent=agent_id, deny_reason="Gateway 未启动，请先运行 openclaw gateway start")
        return {"ok": False, "error": "Gateway 未启动，请先运行 openclaw gateway start"}

    runtime_id = agent_id
    msg = message or f"🔔 系统心跳检测 — 请回复 OK 确认在线。当前时间: {srv.now_iso()}"

    def do_wake():
        try:
            cmd = ["openclaw", "agent", "--agent", runtime_id, "-m", msg, "--timeout", "120"]
            srv.log.info(f"🔔 唤醒 {agent_id}...")
            for attempt in range(1, 3):
                result = srv.subprocess.run(cmd, capture_output=True, text=True, timeout=130)
                if result.returncode == 0:
                    srv.log.info(f"✅ {agent_id} 已唤醒")
                    return
                err_msg = result.stderr[:200] if result.stderr else result.stdout[:200]
                srv.log.warning(f"⚠️ {agent_id} 唤醒失败(第{attempt}次): {err_msg}")
                if attempt < 2:
                    import time

                    time.sleep(5)
            srv.log.error(f"❌ {agent_id} 唤醒最终失败")
        except srv.subprocess.TimeoutExpired:
            srv.log.error(f"❌ {agent_id} 唤醒超时(130s)")
        except Exception as exc:
            srv.log.warning(f"⚠️ {agent_id} 唤醒异常: {exc}")

    srv.threading.Thread(target=do_wake, daemon=True).start()
    srv._record_task_audit(task_id, "agent.wake", actor, True, target_agent=agent_id, payload={"message": msg[:200]})
    return {"ok": True, "message": f"{agent_id} 唤醒指令已发出，约10-30秒后生效"}
