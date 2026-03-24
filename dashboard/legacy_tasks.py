"""Legacy task control logic extracted from server.py."""

from __future__ import annotations

import json
import re
import sys

_MIN_TITLE_LEN = 6
_JUNK_TITLES = {
    "?",
    "？",
    "好",
    "好的",
    "是",
    "否",
    "不",
    "不是",
    "对",
    "了解",
    "收到",
    "嗯",
    "哦",
    "知道了",
    "开启了么",
    "可以",
    "不行",
    "行",
    "ok",
    "yes",
    "no",
    "你去开启",
    "测试",
    "试试",
    "看看",
}


def _srv():
    module = sys.modules.get("server")
    if module is not None and hasattr(module, "_with_task"):
        return module
    module = sys.modules.get("__main__")
    if module is not None and str(getattr(module, "__file__", "")).endswith("dashboard/server.py"):
        return module
    import server as module  # type: ignore

    return module


def _maybe_fast_lane_transition(task):
    srv = _srv()
    srv.ensure_task_shape(task)
    if srv.canonicalize_state(task.get("state")) != "Assigned":
        return False
    if not srv.fast_lane_ready(task):
        return False
    execution_org = srv.resolve_execution_org(task)
    task["state"] = "Next"
    task["org"] = execution_org
    task["now"] = "⚡ 快车道：已自动进入执行队列"
    task.setdefault("flow_log", []).append(
        {
            "at": srv.now_iso(),
            "from": "尚书省",
            "to": execution_org,
            "remark": "⚡ 快车道自动分流到执行队列",
        }
    )
    srv._scheduler_mark_progress(task, f"快车道自动进入 {execution_org} 执行队列")
    srv._bump_task_version(task)
    task["updatedAt"] = srv.now_iso()
    return True


def handle_task_action(task_id, action, reason, actor=None):
    srv = _srv()
    actor = actor or srv.make_actor_context("emperor", source="dashboard")
    result = srv._with_task(task_id, lambda task, _tasks: _handle_task_action_update(task, action, reason, actor))
    if result is None:
        srv._record_task_audit(task_id, f"task.{action}", actor, False, deny_reason="task not found", payload={"reason": reason})
        return {"ok": False, "error": f"任务 {task_id} 不存在"}
    if not result["allowed"]:
        return {"ok": False, "error": result["error"]}
    if result.get("dispatch_state"):
        srv.dispatch_for_state(task_id, result["task"], result["dispatch_state"], trigger="resume", actor=actor)
    return {"ok": True, "message": result["message"]}


def _handle_task_action_update(task, action, reason, actor):
    srv = _srv()
    srv.ensure_task_shape(task)
    allowed, deny_reason = srv.authorize_task_action(actor, task, action)
    if not allowed:
        srv._record_task_audit(task.get("id", ""), f"task.{action}", actor, False, from_state=task.get("state", ""), deny_reason=deny_reason, payload={"reason": reason})
        return {"allowed": False, "error": deny_reason}

    old_state = srv.canonicalize_state(task.get("state", ""))
    srv._ensure_scheduler(task)
    srv._scheduler_snapshot(task, f"task-action-before-{action}")

    if action == "stop":
        if old_state in srv.TERMINAL_STATES or old_state == "Blocked":
            deny_reason = f"任务当前状态 {old_state} 不支持 stop"
            srv._record_task_audit(task.get("id", ""), "task.stop", actor, False, from_state=old_state, deny_reason=deny_reason, payload={"reason": reason})
            return {"allowed": False, "error": deny_reason}
        task["_prev_state"] = old_state
        task["state"] = "Blocked"
        task["block"] = reason or "皇上叫停"
        task["now"] = f'⏸️ 已暂停：{reason or "皇上叫停"}'
    elif action == "cancel":
        if old_state in srv.TERMINAL_STATES:
            deny_reason = f"任务当前状态 {old_state} 不支持 cancel"
            srv._record_task_audit(task.get("id", ""), "task.cancel", actor, False, from_state=old_state, deny_reason=deny_reason, payload={"reason": reason})
            return {"allowed": False, "error": deny_reason}
        task["_prev_state"] = old_state
        task["state"] = "Cancelled"
        task["org"] = "皇上"
        task["block"] = reason or "皇上取消"
        task["now"] = f'🚫 已取消：{reason or "皇上取消"}'
    elif action == "resume":
        if old_state != "Blocked":
            deny_reason = f"任务当前状态 {old_state} 不支持 resume"
            srv._record_task_audit(task.get("id", ""), "task.resume", actor, False, from_state=old_state, deny_reason=deny_reason, payload={"reason": reason})
            return {"allowed": False, "error": deny_reason}
        previous_state = srv.canonicalize_state(task.get("_prev_state") or "")
        if not previous_state or previous_state in srv.TERMINAL_STATES:
            deny_reason = "Blocked 任务没有可恢复的非终态快照"
            srv._record_task_audit(task.get("id", ""), "task.resume", actor, False, from_state=old_state, deny_reason=deny_reason, payload={"reason": reason})
            return {"allowed": False, "error": deny_reason}
        ok, reason_text = srv.ensure_execution_assignment(task, previous_state)
        if not ok:
            srv._record_task_audit(task.get("id", ""), "task.resume", actor, False, from_state=old_state, to_state=previous_state, deny_reason=reason_text, payload={"reason": reason})
            return {"allowed": False, "error": reason_text}
        task["state"] = previous_state
        task["block"] = "无"
        task["now"] = "▶️ 已恢复执行"
    else:
        deny_reason = f"未知任务动作: {action}"
        srv._record_task_audit(task.get("id", ""), f"task.{action}", actor, False, from_state=old_state, deny_reason=deny_reason)
        return {"allowed": False, "error": deny_reason}

    task.setdefault("flow_log", []).append(
        {
            "at": srv.now_iso(),
            "from": "皇上",
            "to": task.get("org", ""),
            "remark": f'{"⏸️ 叫停" if action == "stop" else "🚫 取消" if action == "cancel" else "▶️ 恢复"}：{reason}',
        }
    )

    if action == "resume":
        srv._scheduler_mark_progress(task, f'恢复到 {task.get("state", "Doing")}')
    else:
        srv._scheduler_add_flow(task, f'皇上{action}：{reason or "无"}')

    srv._bump_task_version(task)
    task["updatedAt"] = srv.now_iso()
    label = {"stop": "已叫停", "cancel": "已取消", "resume": "已恢复"}[action]
    srv._record_task_audit(
        task.get("id", ""),
        f"task.{action}",
        actor,
        True,
        from_state=old_state,
        to_state=task.get("state", ""),
        payload={"reason": reason},
    )
    return {
        "allowed": True,
        "message": f'{task.get("id", "")} {label}',
        "dispatch_state": task.get("state") if action == "resume" else "",
        "task": json.loads(json.dumps(task, ensure_ascii=False)),
    }


def handle_archive_task(task_id, archived, archive_all_done=False, actor=None):
    srv = _srv()
    actor = actor or srv.make_actor_context("emperor", source="dashboard")
    if srv.canonicalize_actor(actor.actor_id) not in {"emperor", "system"}:
        deny_reason = f"actor {actor.actor_id} 无权执行归档"
        srv._record_task_audit(task_id, "task.archive_all_done" if archive_all_done else "task.archive", actor, False, deny_reason=deny_reason, payload={"archived": archived, "archiveAllDone": archive_all_done})
        return {"ok": False, "error": deny_reason}
    if archive_all_done:
        count_box = {"count": 0}

        def modifier(tasks):
            for task in tasks:
                if not isinstance(task, dict):
                    continue
                srv.ensure_task_shape(task)
                if task.get("state") in ("Done", "Cancelled") and not task.get("archived"):
                    task["archived"] = True
                    task["archivedAt"] = srv.now_iso()
                    task["updatedAt"] = srv.now_iso()
                    count_box["count"] += 1
            return tasks

        srv._atomic_update_tasks(modifier)
        srv._record_task_audit("", "task.archive_all_done", actor, True, payload={"count": count_box["count"]})
        return {"ok": True, "message": f'{count_box["count"]} 道旨意已归档', "count": count_box["count"]}
    result = srv._with_task(task_id, lambda task, _tasks: _handle_archive_task_update(task, archived, actor))
    if result is None:
        srv._record_task_audit(task_id, "task.archive", actor, False, deny_reason="task not found", payload={"archived": archived})
        return {"ok": False, "error": f"任务 {task_id} 不存在"}
    return result


def _handle_archive_task_update(task, archived, actor):
    srv = _srv()
    srv.ensure_task_shape(task)
    task["archived"] = bool(archived)
    if archived:
        task["archivedAt"] = srv.now_iso()
    else:
        task.pop("archivedAt", None)
    task["updatedAt"] = srv.now_iso()
    srv._record_task_audit(task.get("id", ""), "task.archive", actor, True, from_state=task.get("state", ""), payload={"archived": archived})
    label = "已归档" if archived else "已取消归档"
    return {"ok": True, "message": f'{task.get("id", "")} {label}'}


def update_task_todos(task_id, todos, actor=None):
    srv = _srv()
    actor = actor or srv.make_actor_context("emperor", source="dashboard")
    result = srv._with_task(task_id, lambda task, _tasks: _update_task_todos_update(task, todos, actor))
    if result is None:
        srv._record_task_audit(task_id, "task.todos", actor, False, deny_reason="task not found", payload={"todos": todos})
        return {"ok": False, "error": f"任务 {task_id} 不存在"}
    return result


def _update_task_todos_update(task, todos, actor):
    srv = _srv()
    srv.ensure_task_shape(task)
    allowed, deny_reason = srv.authorize_todos(actor, task)
    if not allowed:
        srv._record_task_audit(task.get("id", ""), "task.todos", actor, False, from_state=task.get("state", ""), deny_reason=deny_reason, payload={"todos": todos})
        return {"ok": False, "error": deny_reason}
    task["todos"] = todos
    task["updatedAt"] = srv.now_iso()
    srv._record_task_audit(task.get("id", ""), "task.todos", actor, True, from_state=task.get("state", ""), payload={"todo_count": len(todos)})
    return {"ok": True, "message": f'{task.get("id", "")} todos 已更新'}


def handle_create_task(title, org="中书省", official="中书令", priority="normal", lane="standard", template_id="", params=None, target_dept="", actor=None):
    srv = _srv()
    actor = actor or srv.make_actor_context("emperor", source="dashboard")
    if not title or not title.strip():
        return {"ok": False, "error": "任务标题不能为空"}
    title = title.strip()
    title = re.split(r"\n*Conversation info\s*\(", title, maxsplit=1)[0].strip()
    title = re.split(r"\n*```", title, maxsplit=1)[0].strip()
    title = re.sub(r"^(传旨|下旨)[：:\uff1a]\s*", "", title)
    if len(title) > 100:
        title = title[:100] + "…"
    if len(title) < _MIN_TITLE_LEN:
        return {"ok": False, "error": f"标题过短（{len(title)}<{_MIN_TITLE_LEN}字），不像是旨意"}
    if title.lower() in _JUNK_TITLES:
        return {"ok": False, "error": f"「{title}」不是有效旨意，请输入具体工作指令"}

    result_box = {}

    def modifier(tasks):
        today = srv.datetime.datetime.now().strftime("%Y%m%d")
        today_ids = [t.get("id", "") for t in tasks if isinstance(t, dict) and t.get("id", "").startswith(f"JJC-{today}-")]
        seq = 1
        if today_ids:
            nums = [int(tid.split("-")[-1]) for tid in today_ids if tid.split("-")[-1].isdigit()]
            seq = max(nums) + 1 if nums else 1
        task_id = f"JJC-{today}-{seq:03d}"
        initial_org = "司礼监"
        new_task = {
            "id": task_id,
            "title": title,
            "official": official,
            "org": initial_org,
            "state": "Sili",
            "now": "等待司礼监接旨分办",
            "eta": "-",
            "block": "无",
            "output": "",
            "ac": "",
            "priority": priority,
            "lane": lane or "standard",
            "templateId": template_id,
            "templateParams": params or {},
            "flow_log": [
                {
                    "at": srv.now_iso(),
                    "from": "皇上",
                    "to": initial_org,
                    "remark": f"下旨：{title}",
                }
            ],
            "updatedAt": srv.now_iso(),
        }
        if target_dept:
            new_task["targetDept"] = target_dept
        srv.ensure_task_shape(new_task)
        new_task["_stateVersion"] = max(1, int(new_task.get("_stateVersion") or 1))
        srv._ensure_scheduler(new_task)
        srv._scheduler_snapshot(new_task, "create-task-initial")
        srv._scheduler_mark_progress(new_task, "任务创建")
        tasks.insert(0, new_task)
        result_box["task"] = json.loads(json.dumps(new_task, ensure_ascii=False))
        result_box["taskId"] = task_id
        return tasks

    srv._atomic_update_tasks(modifier)
    task_id = result_box.get("taskId", "")
    if not task_id:
        srv._record_task_audit("", "task.create", actor, False, deny_reason="create failed", payload={"title": title})
        return {"ok": False, "error": "创建任务失败"}

    srv._record_task_audit(task_id, "task.create", actor, True, to_state="Sili", payload={"title": title, "targetDept": target_dept})
    srv.log.info(f'创建任务: {task_id} | {title[:40]}')
    srv.dispatch_for_state(task_id, result_box["task"], "Sili", trigger="imperial-edict", actor=actor)
    return {"ok": True, "taskId": task_id, "message": f"旨意 {task_id} 已下达，正在派发给司礼监"}


def handle_review_action(task_id, action, comment="", actor=None):
    srv = _srv()
    actor = actor or srv.make_actor_context("emperor", source="dashboard")
    result = srv._with_task(task_id, lambda task, _tasks: _handle_review_action_update(task, action, comment, actor))
    if result is None:
        srv._record_task_audit(task_id, f"review.{action}", actor, False, deny_reason="task not found", payload={"comment": comment})
        return {"ok": False, "error": f"任务 {task_id} 不存在"}
    if not result["allowed"]:
        return {"ok": False, "error": result["error"]}
    if result.get("dispatch_state"):
        srv.dispatch_for_state(task_id, result["task"], result["dispatch_state"], trigger=f"review-{action}", actor=actor)
    return {"ok": True, "message": result["message"]}


def _handle_review_action_update(task, action, comment, actor):
    srv = _srv()
    srv.ensure_task_shape(task)
    allowed, deny_reason = srv.authorize_review(actor, task, action)
    if not allowed:
        srv._record_task_audit(task.get("id", ""), f"review.{action}", actor, False, from_state=task.get("state", ""), deny_reason=deny_reason, payload={"comment": comment})
        return {"allowed": False, "error": deny_reason}

    srv._ensure_scheduler(task)
    srv._scheduler_snapshot(task, f"review-before-{action}")
    current_state = srv.canonicalize_state(task.get("state", ""))
    ok, transition = srv.review_transition(task, action, comment)
    if not ok:
        srv._record_task_audit(task.get("id", ""), f"review.{action}", actor, False, from_state=current_state, deny_reason=transition["reason"], payload={"comment": comment})
        return {"allowed": False, "error": transition["reason"]}

    new_state = transition["new_state"]
    task["state"] = new_state
    task["org"] = transition["to_org"]
    if transition.get("increment_review_round"):
        next_round = int(task.get("review_round") or 0) + 1
        task["review_round"] = next_round
        task["now"] = f'{transition["now"]}（第{next_round}轮）'
    else:
        task["now"] = transition["now"]
    task.setdefault("flow_log", []).append(
        {
            "at": srv.now_iso(),
            "from": transition["from_org"],
            "to": transition["to_org"],
            "remark": transition["remark"],
        }
    )
    srv._scheduler_mark_progress(task, f"审议动作 {action} -> {new_state}")
    fast_tracked = _maybe_fast_lane_transition(task)
    if not fast_tracked:
        srv._bump_task_version(task)
    task["updatedAt"] = srv.now_iso()
    srv._record_task_audit(
        task.get("id", ""),
        f"review.{action}",
        actor,
        True,
        from_state=current_state,
        to_state=task.get("state", ""),
        payload={"comment": comment},
    )

    label = "已准奏" if action == "approve" else "已驳回"
    dispatched = " (已自动派发 Agent)" if new_state not in srv.TERMINAL_STATES else ""
    return {
        "allowed": True,
        "message": f'{task.get("id", "")} {label}{dispatched}',
        "dispatch_state": task.get("state") if task.get("state") not in srv.TERMINAL_STATES else "",
        "task": json.loads(json.dumps(task, ensure_ascii=False)),
    }


def handle_task_consult(task_id, target_agent, note="", actor=None):
    srv = _srv()
    actor = actor or srv.make_actor_context("system", source="dashboard")
    result = srv._with_task(task_id, lambda task, _tasks: _handle_task_consult_update(task, target_agent, note, actor))
    if result is None:
        srv._record_task_audit(task_id, "task.consult", actor, False, deny_reason="task not found", target_agent=target_agent, payload={"note": note})
        return {"ok": False, "error": f"任务 {task_id} 不存在"}
    if result.get("ok"):
        srv.wake_agent(target_agent, f"横向咨询，请保持主状态不变：{note or task_id}", actor=actor, task_id=task_id)
    return result


def _handle_task_consult_update(task, target_agent, note, actor):
    srv = _srv()
    srv.ensure_task_shape(task)
    allowed, deny_reason = srv.authorize_consultation(actor, task, target_agent)
    if not allowed:
        srv._record_task_audit(task.get("id", ""), "task.consult", actor, False, from_state=task.get("state", ""), deny_reason=deny_reason, target_agent=target_agent, payload={"note": note})
        return {"ok": False, "error": deny_reason}
    consult_key = srv.build_consult_key(task.get("id", ""), task.get("state", ""), int(task.get("_stateVersion") or 1), actor.actor_id, target_agent, actor.request_id)
    task.setdefault("consultLog", []).append(
        {
            "at": srv.now_iso(),
            "from": actor.actor_id,
            "to": target_agent,
            "state": task.get("state", ""),
            "note": note or "横向咨询",
            "consultKey": consult_key,
        }
    )
    task["updatedAt"] = srv.now_iso()
    srv._record_task_audit(task.get("id", ""), "task.consult", actor, True, from_state=task.get("state", ""), to_state=task.get("state", ""), target_agent=target_agent, payload={"note": note})
    return {"ok": True, "message": f'{task.get("id", "")} 已向 {target_agent} 发起横向咨询', "consultKey": consult_key}


def handle_advance_state(task_id, comment="", actor=None):
    srv = _srv()
    actor = actor or srv.make_actor_context("emperor", source="dashboard")
    result = srv._with_task(task_id, lambda task, _tasks: _handle_advance_state_update(task, comment, actor))
    if result is None:
        srv._record_task_audit(task_id, "task.advance", actor, False, deny_reason="task not found", payload={"comment": comment})
        return {"ok": False, "error": f"任务 {task_id} 不存在"}
    if not result["allowed"]:
        return {"ok": False, "error": result["error"]}
    if result.get("dispatch_state"):
        srv.dispatch_for_state(task_id, result["task"], result["dispatch_state"], trigger="manual-advance", actor=actor)
    return {"ok": True, "message": result["message"]}


def _handle_advance_state_update(task, comment, actor):
    srv = _srv()
    srv.ensure_task_shape(task)
    current_state = srv.canonicalize_state(task.get("state", ""))
    ok, transition = srv.next_manual_transition(task)
    if not ok:
        srv._record_task_audit(task.get("id", ""), "task.advance", actor, False, from_state=current_state, deny_reason=transition["reason"], payload={"comment": comment})
        return {"allowed": False, "error": transition["reason"]}
    next_state = transition["next_state"]
    allowed, deny_reason = srv.authorize_transition(actor, task, next_state)
    if not allowed:
        srv._record_task_audit(task.get("id", ""), "task.advance", actor, False, from_state=current_state, to_state=next_state, deny_reason=deny_reason, payload={"comment": comment})
        return {"allowed": False, "error": deny_reason}

    srv._ensure_scheduler(task)
    srv._scheduler_snapshot(task, f"advance-before-{current_state}")
    remark = comment or transition["remark"]
    task["state"] = next_state
    task["org"] = transition["to_org"]
    task["now"] = f"⬇️ 手动推进：{remark}"
    task.setdefault("flow_log", []).append(
        {
            "at": srv.now_iso(),
            "from": transition["from_org"],
            "to": transition["to_org"],
            "remark": f"⬇️ 手动推进：{remark}",
        }
    )
    srv._scheduler_mark_progress(task, f"手动推进 {current_state} -> {next_state}")
    fast_tracked = _maybe_fast_lane_transition(task)
    if not fast_tracked:
        srv._bump_task_version(task)
    task["updatedAt"] = srv.now_iso()
    srv._record_task_audit(
        task.get("id", ""),
        "task.advance",
        actor,
        True,
        from_state=current_state,
        to_state=task.get("state", ""),
        payload={"comment": comment},
    )

    from_label = srv._STATE_LABELS.get(current_state, current_state)
    to_label = srv._STATE_LABELS.get(next_state, next_state)
    dispatched = " (已自动派发 Agent)" if next_state not in srv.TERMINAL_STATES else ""
    return {
        "allowed": True,
        "message": f'{task.get("id", "")} {from_label} → {to_label}{dispatched}',
        "dispatch_state": task.get("state") if task.get("state") not in srv.TERMINAL_STATES else "",
        "task": json.loads(json.dumps(task, ensure_ascii=False)),
    }
