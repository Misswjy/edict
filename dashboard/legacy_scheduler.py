"""Legacy scheduler logic extracted from server.py."""

from __future__ import annotations

import json
import sys


def _srv():
    module = sys.modules.get("server")
    if module is not None and hasattr(module, "_with_task"):
        return module
    module = sys.modules.get("__main__")
    if module is not None and str(getattr(module, "__file__", "")).endswith("dashboard/server.py"):
        return module
    import server as module  # type: ignore

    return module


def _terminal_states():
    srv = _srv()
    return set(srv.TERMINAL_STATES)


def _parse_iso(ts):
    srv = _srv()
    if not ts or not isinstance(ts, str):
        return None
    try:
        return srv.datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _ensure_scheduler(task):
    srv = _srv()
    sched = task.setdefault("_scheduler", {})
    if not isinstance(sched, dict):
        sched = {}
        task["_scheduler"] = sched
    sched.setdefault("enabled", True)
    sched.setdefault("stallThresholdSec", 600)
    sched.setdefault("maxRetry", 2)
    sched.setdefault("retryCount", 0)
    sched.setdefault("escalationLevel", 0)
    sched.setdefault("autoRollback", True)
    if not sched.get("lastProgressAt"):
        sched["lastProgressAt"] = task.get("updatedAt") or srv.now_iso()
    if "stallSince" not in sched:
        sched["stallSince"] = None
    if "lastDispatchStatus" not in sched:
        sched["lastDispatchStatus"] = "idle"
    if "snapshot" not in sched:
        sched["snapshot"] = {
            "state": task.get("state", ""),
            "org": task.get("org", ""),
            "now": task.get("now", ""),
            "savedAt": srv.now_iso(),
            "note": "init",
        }
    return sched


def _scheduler_add_flow(task, remark, to=""):
    srv = _srv()
    task.setdefault("flow_log", []).append(
        {
            "at": srv.now_iso(),
            "from": "司礼监调度",
            "to": to or task.get("org", ""),
            "remark": f"🧭 {remark}",
        }
    )


def _scheduler_snapshot(task, note=""):
    srv = _srv()
    sched = _ensure_scheduler(task)
    sched["snapshot"] = {
        "state": task.get("state", ""),
        "org": task.get("org", ""),
        "now": task.get("now", ""),
        "savedAt": srv.now_iso(),
        "note": note or "snapshot",
    }


def _scheduler_mark_progress(task, note=""):
    srv = _srv()
    sched = _ensure_scheduler(task)
    recommended_sla = srv.queue_sla_seconds(task, task.get("state"))
    if recommended_sla:
        sched["stallThresholdSec"] = recommended_sla
    sched["lastProgressAt"] = srv.now_iso()
    sched["stallSince"] = None
    sched["retryCount"] = 0
    sched["escalationLevel"] = 0
    sched["lastEscalatedAt"] = None
    if note:
        _scheduler_add_flow(task, f"进展确认：{note}")


def _update_task_scheduler(task_id, updater):
    srv = _srv()
    result = srv._with_task(task_id, lambda task, _tasks: _update_task_scheduler_inner(task, updater))
    return result is not None


def _update_task_scheduler_inner(task, updater):
    srv = _srv()
    srv.ensure_task_shape(task)
    sched = _ensure_scheduler(task)
    updater(task, sched)
    task["updatedAt"] = srv.now_iso()
    return True


def get_scheduler_state(task_id):
    srv = _srv()
    tasks = srv.load_tasks()
    task = next((t for t in tasks if t.get("id") == task_id), None)
    if not task:
        return {"ok": False, "error": f"任务 {task_id} 不存在"}
    sched = _ensure_scheduler(task)
    last_progress = _parse_iso(sched.get("lastProgressAt") or task.get("updatedAt"))
    now_dt = srv.datetime.datetime.now(srv.datetime.timezone.utc)
    stalled_sec = 0
    if last_progress:
        stalled_sec = max(0, int((now_dt - last_progress).total_seconds()))
    return {
        "ok": True,
        "taskId": task_id,
        "state": task.get("state", ""),
        "org": task.get("org", ""),
        "scheduler": sched,
        "stalledSec": stalled_sec,
        "checkedAt": srv.now_iso(),
    }


def get_queue_metrics():
    srv = _srv()
    tasks = srv.load_tasks()
    now_dt = srv.datetime.datetime.now(srv.datetime.timezone.utc)
    queues = {
        "menxia": {"owner": "menxia", "label": "门下省", "states": ["Menxia"], "waiting": 0, "overdue": 0, "fastLane": 0, "oldestWaitSec": 0, "tasks": []},
        "shangshu": {"owner": "shangshu", "label": "尚书省", "states": ["Assigned", "Review"], "waiting": 0, "overdue": 0, "fastLane": 0, "oldestWaitSec": 0, "tasks": []},
    }
    for task in tasks:
        if not isinstance(task, dict):
            continue
        srv.ensure_task_shape(task)
        owner = srv.central_queue_owner(task.get("state"))
        if not owner or task.get("archived"):
            continue
        updated_at = _parse_iso(task.get("updatedAt")) or now_dt
        wait_sec = max(0, int((now_dt - updated_at).total_seconds()))
        sla_sec = srv.queue_sla_seconds(task, task.get("state"))
        item = {
            "taskId": task.get("id", ""),
            "state": task.get("state", ""),
            "lane": task.get("lane", "standard"),
            "waitSec": wait_sec,
            "slaSec": sla_sec,
            "overdue": bool(sla_sec and wait_sec > sla_sec),
        }
        queue = queues[owner]
        queue["waiting"] += 1
        queue["oldestWaitSec"] = max(queue["oldestWaitSec"], wait_sec)
        if item["lane"] == "fast":
            queue["fastLane"] += 1
        if item["overdue"]:
            queue["overdue"] += 1
        queue["tasks"].append(item)
    return {"ok": True, "queues": queues, "checkedAt": srv.now_iso()}


def handle_scheduler_retry(task_id, reason="", actor=None):
    srv = _srv()
    actor = actor or srv.make_actor_context("sili", source="scheduler")
    allowed, deny_reason = srv.authorize_scheduler(actor, "retry")
    if not allowed:
        srv._record_task_audit(task_id, "scheduler.retry", actor, False, deny_reason=deny_reason, payload={"reason": reason})
        return {"ok": False, "error": deny_reason}
    result = srv._with_task(task_id, lambda task, _tasks: _handle_scheduler_retry_update(task, reason, actor))
    if result is None:
        srv._record_task_audit(task_id, "scheduler.retry", actor, False, deny_reason="task not found", payload={"reason": reason})
        return {"ok": False, "error": f"任务 {task_id} 不存在"}
    if not result["allowed"]:
        return {"ok": False, "error": result["error"]}
    srv.dispatch_for_state(task_id, result["task"], result["state"], trigger="sili-retry", actor=actor)
    return {"ok": True, "message": result["message"], "retryCount": result["retryCount"]}


def _handle_scheduler_retry_update(task, reason, actor):
    srv = _srv()
    srv.ensure_task_shape(task)
    state = srv.canonicalize_state(task.get("state", ""))
    terminals = _terminal_states()
    if state in terminals or state == "Blocked":
        deny_reason = f'任务 {task.get("id", "")} 当前状态 {state} 不支持重试'
        srv._record_task_audit(task.get("id", ""), "scheduler.retry", actor, False, from_state=state, deny_reason=deny_reason, payload={"reason": reason})
        return {"allowed": False, "error": deny_reason}
    sched = _ensure_scheduler(task)
    sched["retryCount"] = int(sched.get("retryCount") or 0) + 1
    sched["lastRetryAt"] = srv.now_iso()
    sched["lastDispatchTrigger"] = "sili-retry"
    _scheduler_add_flow(task, f'触发重试第{sched["retryCount"]}次：{reason or "超时未推进"}')
    task["updatedAt"] = srv.now_iso()
    srv._record_task_audit(task.get("id", ""), "scheduler.retry", actor, True, from_state=state, to_state=state, payload={"reason": reason, "retryCount": sched["retryCount"]})
    return {
        "allowed": True,
        "message": f'{task.get("id", "")} 已触发重试派发',
        "retryCount": sched["retryCount"],
        "state": state,
        "task": json.loads(json.dumps(task, ensure_ascii=False)),
    }


def handle_scheduler_escalate(task_id, reason="", actor=None):
    srv = _srv()
    actor = actor or srv.make_actor_context("sili", source="scheduler")
    allowed, deny_reason = srv.authorize_scheduler(actor, "escalate")
    if not allowed:
        srv._record_task_audit(task_id, "scheduler.escalate", actor, False, deny_reason=deny_reason, payload={"reason": reason})
        return {"ok": False, "error": deny_reason}
    result = srv._with_task(task_id, lambda task, _tasks: _handle_scheduler_escalate_update(task, reason, actor))
    if result is None:
        srv._record_task_audit(task_id, "scheduler.escalate", actor, False, deny_reason="task not found", payload={"reason": reason})
        return {"ok": False, "error": f"任务 {task_id} 不存在"}
    if not result["allowed"]:
        return {"ok": False, "error": result["error"]}
    srv.wake_agent(result["target"], result["messageText"], actor=actor, task_id=task_id)
    return {"ok": True, "message": result["message"], "escalationLevel": result["escalationLevel"]}


def _handle_scheduler_escalate_update(task, reason, actor):
    srv = _srv()
    srv.ensure_task_shape(task)
    state = srv.canonicalize_state(task.get("state", ""))
    terminals = _terminal_states()
    if state in terminals:
        deny_reason = f'任务 {task.get("id", "")} 已结束，无需升级'
        srv._record_task_audit(task.get("id", ""), "scheduler.escalate", actor, False, from_state=state, deny_reason=deny_reason, payload={"reason": reason})
        return {"allowed": False, "error": deny_reason}
    sched = _ensure_scheduler(task)
    current_level = int(sched.get("escalationLevel") or 0)
    next_level = min(current_level + 1, 2)
    target = "menxia" if next_level == 1 else "shangshu"
    target_label = "门下省" if next_level == 1 else "尚书省"
    sched["escalationLevel"] = next_level
    sched["lastEscalatedAt"] = srv.now_iso()
    _scheduler_add_flow(task, f'升级到{target_label}协调：{reason or "任务停滞"}', to=target_label)
    task["updatedAt"] = srv.now_iso()
    srv._record_task_audit(task.get("id", ""), "scheduler.escalate", actor, True, from_state=state, to_state=state, target_agent=target, payload={"reason": reason, "level": next_level})
    msg = (
        "🧭 司礼监调度升级通知\n"
        f'任务ID: {task.get("id", "")}\n'
        f"当前状态: {state}\n"
        "停滞处理: 请你介入协调推进\n"
        f'原因: {reason or "任务超过阈值未推进"}\n'
        "⚠️ 看板已有任务，请勿重复创建。"
    )
    return {"allowed": True, "message": f'{task.get("id", "")} 已升级至{target_label}', "escalationLevel": next_level, "target": target, "messageText": msg}


def handle_scheduler_rollback(task_id, reason="", actor=None):
    srv = _srv()
    actor = actor or srv.make_actor_context("sili", source="scheduler")
    allowed, deny_reason = srv.authorize_scheduler(actor, "rollback")
    if not allowed:
        srv._record_task_audit(task_id, "scheduler.rollback", actor, False, deny_reason=deny_reason, payload={"reason": reason})
        return {"ok": False, "error": deny_reason}
    result = srv._with_task(task_id, lambda task, _tasks: _handle_scheduler_rollback_update(task, reason, actor))
    if result is None:
        srv._record_task_audit(task_id, "scheduler.rollback", actor, False, deny_reason="task not found", payload={"reason": reason})
        return {"ok": False, "error": f"任务 {task_id} 不存在"}
    if not result["allowed"]:
        return {"ok": False, "error": result["error"]}
    if result.get("dispatch_state"):
        srv.dispatch_for_state(task_id, result["task"], result["dispatch_state"], trigger="sili-rollback", actor=actor)
    return {"ok": True, "message": result["message"]}


def _handle_scheduler_rollback_update(task, reason, actor):
    srv = _srv()
    srv.ensure_task_shape(task)
    sched = _ensure_scheduler(task)
    snapshot = sched.get("snapshot") or {}
    snap_state = srv.canonicalize_state(snapshot.get("state"))
    if not snap_state:
        deny_reason = f'任务 {task.get("id", "")} 无可用回滚快照'
        srv._record_task_audit(task.get("id", ""), "scheduler.rollback", actor, False, from_state=task.get("state", ""), deny_reason=deny_reason, payload={"reason": reason})
        return {"allowed": False, "error": deny_reason}
    old_state = srv.canonicalize_state(task.get("state", ""))
    task["state"] = snap_state
    task["org"] = snapshot.get("org", task.get("org", ""))
    task["now"] = f'↩️ 司礼监调度自动回滚：{reason or "恢复到上个稳定节点"}'
    task["block"] = "无"
    sched["retryCount"] = 0
    sched["escalationLevel"] = 0
    sched["stallSince"] = None
    sched["lastProgressAt"] = srv.now_iso()
    _scheduler_add_flow(task, f'执行回滚：{old_state} → {snap_state}，原因：{reason or "停滞恢复"}')
    srv._bump_task_version(task)
    task["updatedAt"] = srv.now_iso()
    terminals = _terminal_states()
    srv._record_task_audit(task.get("id", ""), "scheduler.rollback", actor, True, from_state=old_state, to_state=snap_state, payload={"reason": reason})
    return {
        "allowed": True,
        "message": f'{task.get("id", "")} 已回滚到 {snap_state}',
        "dispatch_state": snap_state if snap_state not in terminals else "",
        "task": json.loads(json.dumps(task, ensure_ascii=False)),
    }


def handle_scheduler_scan(threshold_sec=600, actor=None):
    srv = _srv()
    actor = actor or srv.make_actor_context("sili", source="scheduler")
    allowed, deny_reason = srv.authorize_scheduler(actor, "scan")
    if not allowed:
        srv._record_task_audit("", "scheduler.scan", actor, False, deny_reason=deny_reason, payload={"thresholdSec": threshold_sec})
        return {"ok": False, "error": deny_reason}

    threshold_sec = max(60, int(threshold_sec or 600))
    now_dt = srv.datetime.datetime.now(srv.datetime.timezone.utc)
    pending_retries = []
    pending_escalates = []
    pending_rollbacks = []
    actions = []
    terminals = _terminal_states()

    def modifier(tasks):
        for task in tasks:
            if not isinstance(task, dict):
                continue
            srv.ensure_task_shape(task)
            task_id = task.get("id", "")
            state = srv.canonicalize_state(task.get("state", ""))
            if not task_id or state in terminals or task.get("archived") or state == "Blocked":
                continue
            sched = _ensure_scheduler(task)
            task_threshold = int(sched.get("stallThresholdSec") or threshold_sec)
            last_progress = _parse_iso(sched.get("lastProgressAt") or task.get("updatedAt"))
            if not last_progress:
                continue
            stalled_sec = max(0, int((now_dt - last_progress).total_seconds()))
            if stalled_sec < task_threshold:
                continue
            if not sched.get("stallSince"):
                sched["stallSince"] = srv.now_iso()
            retry_count = int(sched.get("retryCount") or 0)
            max_retry = max(0, int(sched.get("maxRetry") or 1))
            level = int(sched.get("escalationLevel") or 0)

            if retry_count < max_retry:
                sched["retryCount"] = retry_count + 1
                sched["lastRetryAt"] = srv.now_iso()
                sched["lastDispatchTrigger"] = "sili-scan-retry"
                _scheduler_add_flow(task, f'停滞{stalled_sec}秒，触发自动重试第{sched["retryCount"]}次')
                task["updatedAt"] = srv.now_iso()
                pending_retries.append({"taskId": task_id, "state": state, "task": json.loads(json.dumps(task, ensure_ascii=False))})
                actions.append({"taskId": task_id, "action": "retry", "stalledSec": stalled_sec})
                srv._record_task_audit(task_id, "scheduler.scan.retry", actor, True, from_state=state, to_state=state, payload={"stalledSec": stalled_sec, "retryCount": sched["retryCount"]})
                continue

            if level < 2:
                next_level = level + 1
                target = "menxia" if next_level == 1 else "shangshu"
                target_label = "门下省" if next_level == 1 else "尚书省"
                sched["escalationLevel"] = next_level
                sched["lastEscalatedAt"] = srv.now_iso()
                _scheduler_add_flow(task, f"停滞{stalled_sec}秒，升级至{target_label}协调", to=target_label)
                task["updatedAt"] = srv.now_iso()
                pending_escalates.append({"taskId": task_id, "state": state, "target": target, "targetLabel": target_label, "stalledSec": stalled_sec})
                actions.append({"taskId": task_id, "action": "escalate", "to": target_label, "stalledSec": stalled_sec})
                srv._record_task_audit(task_id, "scheduler.scan.escalate", actor, True, from_state=state, to_state=state, target_agent=target, payload={"stalledSec": stalled_sec, "level": next_level})
                continue

            if sched.get("autoRollback", True):
                snapshot = sched.get("snapshot") or {}
                snap_state = srv.canonicalize_state(snapshot.get("state"))
                if snap_state and snap_state != state:
                    task["state"] = snap_state
                    task["org"] = snapshot.get("org", task.get("org", ""))
                    task["now"] = "↩️ 司礼监调度自动回滚到稳定节点"
                    task["block"] = "无"
                    sched["retryCount"] = 0
                    sched["escalationLevel"] = 0
                    sched["stallSince"] = None
                    sched["lastProgressAt"] = srv.now_iso()
                    _scheduler_add_flow(task, f"连续停滞，自动回滚：{state} → {snap_state}")
                    srv._bump_task_version(task)
                    task["updatedAt"] = srv.now_iso()
                    pending_rollbacks.append({"taskId": task_id, "state": snap_state, "task": json.loads(json.dumps(task, ensure_ascii=False))})
                    actions.append({"taskId": task_id, "action": "rollback", "toState": snap_state})
                    srv._record_task_audit(task_id, "scheduler.scan.rollback", actor, True, from_state=state, to_state=snap_state, payload={"stalledSec": stalled_sec})
        return tasks

    srv._atomic_update_tasks(modifier)

    for item in pending_retries:
        srv.dispatch_for_state(item["taskId"], item["task"], item["state"], trigger="sili-scan-retry", actor=actor)
    for item in pending_escalates:
        msg = (
            "🧭 司礼监调度升级通知\n"
            f'任务ID: {item["taskId"]}\n'
            f'当前状态: {item["state"]}\n'
            f'已停滞: {item["stalledSec"]} 秒\n'
            "请立即介入协调推进\n"
            "⚠️ 看板已有任务，请勿重复创建。"
        )
        srv.wake_agent(item["target"], msg, actor=actor, task_id=item["taskId"])
    for item in pending_rollbacks:
        if item["state"] not in terminals:
            srv.dispatch_for_state(item["taskId"], item["task"], item["state"], trigger="sili-auto-rollback", actor=actor)
    return {"ok": True, "thresholdSec": threshold_sec, "actions": actions, "count": len(actions), "checkedAt": srv.now_iso()}


def _startup_recover_queued_dispatches():
    srv = _srv()
    tasks = srv.load_tasks()
    recovered = 0
    terminals = _terminal_states()
    for task in tasks:
        task_id = task.get("id", "")
        state = task.get("state", "")
        if not task_id or state in terminals or task.get("archived"):
            continue
        sched = task.get("_scheduler") or {}
        if sched.get("lastDispatchStatus") == "queued":
            srv.log.info(f"🔄 启动恢复: {task_id} 状态={state} 上次派发未完成，重新派发")
            sched["lastDispatchTrigger"] = "startup-recovery"
            srv.dispatch_for_state(task_id, task, state, trigger="startup-recovery")
            recovered += 1
    if recovered:
        srv.log.info(f"✅ 启动恢复完成: 重新派发 {recovered} 个任务")
    else:
        srv.log.info("✅ 启动恢复: 无需恢复")


def handle_repair_flow_order():
    srv = _srv()
    fixed = 0
    fixed_ids = []

    def modifier(tasks):
        nonlocal fixed, fixed_ids
        for task in tasks:
            if not isinstance(task, dict):
                continue
            srv.ensure_task_shape(task)
            task_id = task.get("id", "")
            if not task_id.startswith("JJC-"):
                continue
            flow_log = task.get("flow_log") or []
            if not flow_log:
                continue
            first = flow_log[0]
            if first.get("from") != "皇上" or first.get("to") != "中书省":
                continue
            first["to"] = "司礼监"
            remark = first.get("remark", "")
            if isinstance(remark, str) and remark.startswith("下旨："):
                first["remark"] = remark
            if task.get("state") == "Zhongshu" and task.get("org") == "中书省" and len(flow_log) == 1:
                task["state"] = "Sili"
                task["org"] = "司礼监"
                task["now"] = "等待司礼监接旨分办"
            task["updatedAt"] = srv.now_iso()
            fixed += 1
            fixed_ids.append(task_id)
        return tasks

    srv._atomic_update_tasks(modifier)
    return {"ok": True, "count": fixed, "taskIds": fixed_ids[:80], "more": max(0, fixed - 80), "checkedAt": srv.now_iso()}
