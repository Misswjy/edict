#!/usr/bin/env python3
"""
三省六部 · 看板本地 API 服务器
Port: 7891 (可通过 --port 修改)

Endpoints:
  GET  /                       → dashboard.html
  GET  /api/live-status        → data/live_status.json
  GET  /api/agent-config       → data/agent_config.json
  POST /api/set-model          → {agentId, model}
  GET  /api/model-change-log   → data/model_change_log.json
  GET  /api/last-result        → data/last_model_change_result.json
"""
import json, pathlib, subprocess, sys, threading, argparse, datetime, logging, re, os
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# 引入文件锁工具，确保与其他脚本并发安全
backend_dir = str(pathlib.Path(__file__).parent.parent / 'edict' / 'backend')
scripts_dir = str(pathlib.Path(__file__).parent.parent / 'scripts')
sys.path.insert(0, backend_dir)
sys.path.insert(0, scripts_dir)
from file_lock import atomic_json_read, atomic_json_write, atomic_json_update
from utils import validate_url, read_json, now_iso
from court_discuss import (
    create_session as cd_create, advance_discussion as cd_advance,
    get_session as cd_get, conclude_session as cd_conclude,
    list_sessions as cd_list, destroy_session as cd_destroy,
    get_fate_event as cd_fate, OFFICIAL_PROFILES as CD_PROFILES,
    set_session_store_path as cd_set_store_path,
)
from app.task_contract import (
    ActorContext,
    ORG_AGENT_MAP,
    STATE_LABELS,
    STATE_OWNER_AGENT,
    TERMINAL_STATES,
    VALID_TRANSITIONS,
    authorize_consultation,
    authorize_agent_wake,
    authorize_dispatch,
    authorize_review,
    authorize_scheduler,
    authorize_task_action,
    authorize_todos,
    authorize_transition,
    build_audit_entry,
    build_consult_key,
    canonicalize_actor,
    canonicalize_state,
    central_queue_owner,
    ensure_execution_assignment,
    ensure_task_shape,
    fast_lane_ready,
    make_actor_context,
    next_manual_transition,
    queue_sla_seconds,
    build_dispatch_key,
    resolve_dispatch_agent,
    resolve_execution_org,
    review_transition,
)
from app.security import (
    extract_admin_token,
    is_loopback_host,
    normalize_activity_sensitivity,
    redact_sensitive_data,
)
import legacy_agents
import legacy_morning
import legacy_scheduler
import legacy_skills
import legacy_tasks

log = logging.getLogger('server')
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(message)s', datefmt='%H:%M:%S')

OCLAW_HOME = pathlib.Path.home() / '.openclaw'
MAX_REQUEST_BODY = 1 * 1024 * 1024  # 1 MB
ALLOWED_ORIGIN = None  # Set via --cors; None means restrict to localhost
_DEFAULT_ORIGINS = {
    'http://127.0.0.1:7891', 'http://localhost:7891',
    'http://127.0.0.1:5173', 'http://localhost:5173',  # Vite dev server
}
_SAFE_NAME_RE = re.compile(r'^[a-zA-Z0-9_\-\u4e00-\u9fff]+$')

BASE = pathlib.Path(__file__).parent
DIST = BASE / 'dist'          # React 构建产物 (npm run build)
DATA = BASE.parent / "data"
SCRIPTS = BASE.parent / 'scripts'
TASKS_PATH = DATA / 'tasks_source.json'
TASK_AUDIT_PATH = DATA / 'task_audit_log.json'
CONTROL_PLANE_URL = (os.environ.get('EDICT_BACKEND_URL') or '').strip().rstrip('/')
cd_set_store_path(DATA / 'court_discuss_sessions.json')

# 静态资源 MIME 类型
_MIME_TYPES = {
    '.html': 'text/html; charset=utf-8',
    '.js':   'application/javascript; charset=utf-8',
    '.css':  'text/css; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.png':  'image/png',
    '.jpg':  'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif':  'image/gif',
    '.svg':  'image/svg+xml',
    '.ico':  'image/x-icon',
    '.woff': 'font/woff',
    '.woff2': 'font/woff2',
    '.ttf':  'font/ttf',
    '.map':  'application/json',
}


def cors_headers(h):
    req_origin = h.headers.get('Origin', '')
    if ALLOWED_ORIGIN:
        origin = ALLOWED_ORIGIN
    elif req_origin in _DEFAULT_ORIGINS:
        origin = req_origin
    else:
        origin = 'http://127.0.0.1:7891'
    h.send_header('Access-Control-Allow-Origin', origin)
    h.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
    h.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization, X-Edict-Admin-Token')


def _legacy_admin_token():
    return (os.environ.get('EDICT_DASHBOARD_ADMIN_TOKEN') or os.environ.get('EDICT_ADMIN_API_TOKEN') or '').strip()


def _activity_sensitivity():
    return normalize_activity_sensitivity(os.environ.get('EDICT_ACTIVITY_SENSITIVITY'))


def _legacy_client_host(handler) -> str:
    client = getattr(handler, 'client_address', None) or ('', 0)
    return str(client[0] or '').strip()


def _legacy_write_allowed(handler) -> bool:
    expected = _legacy_admin_token()
    provided = extract_admin_token(
        handler.headers.get('Authorization'),
        handler.headers.get('X-Edict-Admin-Token'),
    )
    if expected:
        return provided == expected
    return is_loopback_host(_legacy_client_host(handler))


def load_tasks():
    tasks = atomic_json_read(TASKS_PATH, [])
    if not isinstance(tasks, list):
        return []
    for task in tasks:
        if isinstance(task, dict):
            ensure_task_shape(task)
    return tasks


def save_tasks(tasks):
    atomic_json_write(TASKS_PATH, tasks)
    # Trigger refresh (异步，不阻塞，避免僵尸进程)
    _trigger_refresh_async()


def _trigger_refresh_async():
    def _refresh():
        try:
            subprocess.run(['python3', str(SCRIPTS / 'refresh_live_data.py')], timeout=30)
        except Exception as e:
            log.warning(f'refresh_live_data.py 触发失败: {e}')
    threading.Thread(target=_refresh, daemon=True).start()


def _append_task_audit(entry):
    def modifier(entries):
        if not isinstance(entries, list):
            entries = []
        entries.append(entry)
        if len(entries) > 2000:
            entries = entries[-2000:]
        return entries

    atomic_json_update(TASK_AUDIT_PATH, modifier, [])


def _record_task_audit(task_id, action, actor, allowed, from_state='', to_state='', target_agent='', deny_reason='', payload=None):
    entry = build_audit_entry(
        task_id=task_id,
        action=action,
        actor=actor,
        allowed=allowed,
        from_state=from_state,
        to_state=to_state,
        target_agent=target_agent,
        deny_reason=deny_reason,
        payload=payload,
    )
    _append_task_audit(entry)


def _atomic_update_tasks(modifier, trigger_refresh=True):
    tasks = atomic_json_update(TASKS_PATH, modifier, [])
    if trigger_refresh:
        _trigger_refresh_async()
    return tasks


def _with_task(task_id, updater, *, trigger_refresh=True):
    result = {"payload": None}

    def modifier(tasks):
        for task in tasks:
            if isinstance(task, dict) and task.get('id') == task_id:
                ensure_task_shape(task)
                result["payload"] = updater(task, tasks)
                return tasks
        result["payload"] = None
        return tasks

    _atomic_update_tasks(modifier, trigger_refresh=trigger_refresh)
    return result["payload"]


def _actor_from_request(headers, body, default_actor='emperor', default_source='dashboard'):
    actor_id = (
        body.get('actor')
        or headers.get('X-Edict-Actor', '')
        or default_actor
    )
    source = (
        body.get('source')
        or headers.get('X-Edict-Source', '')
        or default_source
    )
    request_id = (
        body.get('requestId')
        or headers.get('X-Request-Id', '')
        or headers.get('X-Edict-Request-Id', '')
    )
    signature = body.get('signature') or headers.get('X-Edict-Signature', '')
    timestamp = body.get('timestamp') or headers.get('X-Edict-Timestamp', '')
    return make_actor_context(actor_id, source=source, request_id=request_id, signature=signature, timestamp=timestamp)


def _should_proxy_control_plane(path):
    if not CONTROL_PLANE_URL:
        return False
    if path in {
        '/api/live-status',
        '/api/queue-metrics',
        '/api/create-task',
        '/api/task-action',
        '/api/task-consult',
        '/api/review-action',
        '/api/advance-state',
        '/api/archive-task',
        '/api/task-todos',
        '/api/scheduler-scan',
        '/api/scheduler-retry',
        '/api/scheduler-escalate',
        '/api/scheduler-rollback',
    }:
        return True
    return path.startswith('/api/task-activity/') or path.startswith('/api/scheduler-state/')


def _proxy_control_plane(method, path, body=None):
    data = None
    headers = {'Content-Type': 'application/json'}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode('utf-8')
    req = Request(f'{CONTROL_PLANE_URL}{path}', data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=15) as resp:
            payload = resp.read()
            data = json.loads(payload or b'{}')
            if isinstance(data, dict) and 'detail' in data and 'error' not in data:
                data['error'] = data['detail']
            return resp.status, data
    except HTTPError as exc:
        payload = exc.read()
        try:
            data = json.loads(payload or b'{}')
        except Exception:
            data = {'ok': False, 'error': f'backend proxy failed: {exc.reason}'}
        if isinstance(data, dict) and 'detail' in data and 'error' not in data:
            data['error'] = data['detail']
        return exc.code, data
    except URLError as exc:
        return 502, {'ok': False, 'error': f'backend proxy failed: {exc.reason}'}


def _bump_task_version(task):
    ensure_task_shape(task)
    task['_stateVersion'] = max(1, int(task.get('_stateVersion') or 1)) + 1
    return task['_stateVersion']


def _maybe_fast_lane_transition(task):
    return legacy_tasks._maybe_fast_lane_transition(task)


def handle_task_action(task_id, action, reason, actor=None):
    return legacy_tasks.handle_task_action(task_id, action, reason, actor=actor)


def _handle_task_action_update(task, action, reason, actor):
    return legacy_tasks._handle_task_action_update(task, action, reason, actor)


def handle_archive_task(task_id, archived, archive_all_done=False, actor=None):
    return legacy_tasks.handle_archive_task(task_id, archived, archive_all_done=archive_all_done, actor=actor)


def _handle_archive_task_update(task, archived, actor):
    return legacy_tasks._handle_archive_task_update(task, archived, actor)


def update_task_todos(task_id, todos, actor=None):
    return legacy_tasks.update_task_todos(task_id, todos, actor=actor)


def _update_task_todos_update(task, todos, actor):
    return legacy_tasks._update_task_todos_update(task, todos, actor)


def read_skill_content(agent_id, skill_name):
    return legacy_skills.read_skill_content(agent_id, skill_name)


def add_skill_to_agent(agent_id, skill_name, description, trigger=''):
    return legacy_skills.add_skill_to_agent(agent_id, skill_name, description, trigger=trigger)


def add_remote_skill(agent_id, skill_name, source_url, description=''):
    return legacy_skills.add_remote_skill(agent_id, skill_name, source_url, description=description)


def get_remote_skills_list():
    return legacy_skills.get_remote_skills_list()


def update_remote_skill(agent_id, skill_name):
    return legacy_skills.update_remote_skill(agent_id, skill_name)


def remove_remote_skill(agent_id, skill_name):
    return legacy_skills.remove_remote_skill(agent_id, skill_name)


def push_to_feishu():
    return legacy_morning.push_to_feishu()


def handle_create_task(title, org='中书省', official='中书令', priority='normal', lane='standard', template_id='', params=None, target_dept='', actor=None):
    return legacy_tasks.handle_create_task(
        title,
        org=org,
        official=official,
        priority=priority,
        lane=lane,
        template_id=template_id,
        params=params,
        target_dept=target_dept,
        actor=actor,
    )


def handle_review_action(task_id, action, comment='', actor=None):
    return legacy_tasks.handle_review_action(task_id, action, comment=comment, actor=actor)


def _handle_review_action_update(task, action, comment, actor):
    return legacy_tasks._handle_review_action_update(task, action, comment, actor)


# ══ Agent 在线状态检测 ══


def _check_gateway_alive():
    return legacy_agents._check_gateway_alive()


def _check_gateway_probe():
    return legacy_agents._check_gateway_probe()


def _get_agent_session_status(agent_id):
    return legacy_agents._get_agent_session_status(agent_id)


def _check_agent_process(agent_id):
    return legacy_agents._check_agent_process(agent_id)


def _check_agent_workspace(agent_id):
    return legacy_agents._check_agent_workspace(agent_id)


def get_agents_status():
    return legacy_agents.get_agents_status()


def wake_agent(agent_id, message='', actor=None, task_id=''):
    return legacy_agents.wake_agent(agent_id, message=message, actor=actor, task_id=task_id)


# ══ Agent 实时活动读取 ══

# 状态 → agent_id 映射
_STATE_AGENT_MAP = dict(STATE_OWNER_AGENT)
_ORG_AGENT_MAP = dict(ORG_AGENT_MAP)

def _parse_iso(ts):
    return legacy_scheduler._parse_iso(ts)


def _ensure_scheduler(task):
    return legacy_scheduler._ensure_scheduler(task)


def _scheduler_add_flow(task, remark, to=''):
    return legacy_scheduler._scheduler_add_flow(task, remark, to=to)


def _scheduler_snapshot(task, note=''):
    return legacy_scheduler._scheduler_snapshot(task, note=note)


def _scheduler_mark_progress(task, note=''):
    return legacy_scheduler._scheduler_mark_progress(task, note=note)


def _update_task_scheduler(task_id, updater):
    return legacy_scheduler._update_task_scheduler(task_id, updater)


def _update_task_scheduler_inner(task, updater):
    return legacy_scheduler._update_task_scheduler_inner(task, updater)


def get_scheduler_state(task_id):
    return legacy_scheduler.get_scheduler_state(task_id)


def get_queue_metrics():
    return legacy_scheduler.get_queue_metrics()


def handle_task_consult(task_id, target_agent, note='', actor=None):
    return legacy_tasks.handle_task_consult(task_id, target_agent, note=note, actor=actor)


def _handle_task_consult_update(task, target_agent, note, actor):
    return legacy_tasks._handle_task_consult_update(task, target_agent, note, actor)


def handle_scheduler_retry(task_id, reason='', actor=None):
    return legacy_scheduler.handle_scheduler_retry(task_id, reason=reason, actor=actor)


def _handle_scheduler_retry_update(task, reason, actor):
    return legacy_scheduler._handle_scheduler_retry_update(task, reason, actor)


def handle_scheduler_escalate(task_id, reason='', actor=None):
    return legacy_scheduler.handle_scheduler_escalate(task_id, reason=reason, actor=actor)


def _handle_scheduler_escalate_update(task, reason, actor):
    return legacy_scheduler._handle_scheduler_escalate_update(task, reason, actor)


def handle_scheduler_rollback(task_id, reason='', actor=None):
    return legacy_scheduler.handle_scheduler_rollback(task_id, reason=reason, actor=actor)


def _handle_scheduler_rollback_update(task, reason, actor):
    return legacy_scheduler._handle_scheduler_rollback_update(task, reason, actor)


def handle_scheduler_scan(threshold_sec=600, actor=None):
    return legacy_scheduler.handle_scheduler_scan(threshold_sec=threshold_sec, actor=actor)


def _startup_recover_queued_dispatches():
    return legacy_scheduler._startup_recover_queued_dispatches()


def handle_repair_flow_order():
    return legacy_scheduler.handle_repair_flow_order()


def _collect_message_text(msg):
    """收集消息中的可检索文本，用于 task_id/关键词过滤。"""
    parts = []
    for c in msg.get('content', []) or []:
        ctype = c.get('type')
        if ctype == 'text' and c.get('text'):
            parts.append(str(c.get('text', '')))
        elif ctype == 'thinking' and c.get('thinking'):
            parts.append(str(c.get('thinking', '')))
        elif ctype == 'tool_use':
            parts.append(json.dumps(c.get('input', {}), ensure_ascii=False))
    details = msg.get('details') or {}
    for key in ('output', 'stdout', 'stderr', 'message'):
        val = details.get(key)
        if isinstance(val, str) and val:
            parts.append(val)
    return ''.join(parts)


def _parse_activity_entry(item):
    """将 session jsonl 的 message 统一解析成看板活动条目。"""
    msg = item.get('message') or {}
    role = str(msg.get('role', '')).strip().lower()
    ts = item.get('timestamp', '')

    if role == 'assistant':
        text = ''
        thinking = ''
        tool_calls = []
        for c in msg.get('content', []) or []:
            if c.get('type') == 'text' and c.get('text') and not text:
                text = str(c.get('text', '')).strip()
            elif c.get('type') == 'thinking' and c.get('thinking') and not thinking:
                thinking = str(c.get('thinking', '')).strip()[:200]
            elif c.get('type') == 'tool_use':
                tool_calls.append({
                    'name': c.get('name', ''),
                    'input_preview': json.dumps(c.get('input', {}), ensure_ascii=False)[:100]
                })
        if not (text or thinking or tool_calls):
            return None
        entry = {'at': ts, 'kind': 'assistant'}
        if text:
            entry['text'] = text[:300]
        if thinking:
            entry['thinking'] = thinking
        if tool_calls:
            entry['tools'] = tool_calls
        return redact_sensitive_data(entry, _activity_sensitivity())

    if role in ('toolresult', 'tool_result'):
        details = msg.get('details') or {}
        code = details.get('exitCode')
        if code is None:
            code = details.get('code', details.get('status'))
        output = ''
        for c in msg.get('content', []) or []:
            if c.get('type') == 'text' and c.get('text'):
                output = str(c.get('text', '')).strip()[:200]
                break
        if not output:
            for key in ('output', 'stdout', 'stderr', 'message'):
                val = details.get(key)
                if isinstance(val, str) and val.strip():
                    output = val.strip()[:200]
                    break

        entry = {
            'at': ts,
            'kind': 'tool_result',
            'tool': msg.get('toolName', msg.get('name', '')),
            'exitCode': code,
            'output': output,
        }
        duration_ms = details.get('durationMs')
        if isinstance(duration_ms, (int, float)):
            entry['durationMs'] = int(duration_ms)
        return redact_sensitive_data(entry, _activity_sensitivity())

    if role == 'user':
        text = ''
        for c in msg.get('content', []) or []:
            if c.get('type') == 'text' and c.get('text'):
                text = str(c.get('text', '')).strip()
                break
        if not text:
            return None
        return redact_sensitive_data({'at': ts, 'kind': 'user', 'text': text[:200]}, _activity_sensitivity())

    return None


def get_agent_activity(agent_id, limit=30, task_id=None):
    """从 Agent 的 session jsonl 读取最近活动。
    如果 task_id 不为空，只返回提及该 task_id 的相关条目。
    """
    sessions_dir = OCLAW_HOME / 'agents' / agent_id / 'sessions'
    if not sessions_dir.exists():
        return []

    # 扫描所有 jsonl（按修改时间倒序），优先最新
    jsonl_files = sorted(sessions_dir.glob('*.jsonl'), key=lambda f: f.stat().st_mtime, reverse=True)
    if not jsonl_files:
        return []

    entries = []
    # 如果需要按 task_id 过滤，可能需要扫描多个文件
    files_to_scan = jsonl_files[:3] if task_id else jsonl_files[:1]

    for session_file in files_to_scan:
        try:
            lines = session_file.read_text(errors='ignore').splitlines()
        except Exception:
            continue

        # 正向扫描以保持时间顺序；如果有 task_id，收集提及 task_id 的条目
        for ln in lines:
            try:
                item = json.loads(ln)
            except Exception:
                continue
            msg = item.get('message') or {}
            all_text = _collect_message_text(msg)

            # task_id 过滤：只保留提及 task_id 的条目
            if task_id and task_id not in all_text:
                continue
            entry = _parse_activity_entry(item)
            if entry:
                entries.append(entry)

            if len(entries) >= limit:
                break
        if len(entries) >= limit:
            break

    # 只保留最后 limit 条
    return entries[-limit:]


def _extract_keywords(title):
    """从任务标题中提取有意义的关键词（用于 session 内容匹配）。"""
    stop = {'的', '了', '在', '是', '有', '和', '与', '或', '一个', '一篇', '关于', '进行',
            '写', '做', '请', '把', '给', '用', '要', '需要', '面向', '风格', '包含',
            '出', '个', '不', '可以', '应该', '如何', '怎么', '什么', '这个', '那个'}
    # 提取英文词
    en_words = re.findall(r'[a-zA-Z][\w.-]{1,}', title)
    # 提取 2-4 字中文词组（更短的颗粒度）
    cn_words = re.findall(r'[\u4e00-\u9fff]{2,4}', title)
    all_words = en_words + cn_words
    kws = [w for w in all_words if w not in stop and len(w) >= 2]
    # 去重保序
    seen = set()
    unique = []
    for w in kws:
        if w.lower() not in seen:
            seen.add(w.lower())
            unique.append(w)
    return unique[:8]  # 最多 8 个关键词


def get_agent_activity_by_keywords(agent_id, keywords, limit=20):
    """从 agent session 中按关键词匹配获取活动条目。
    找到包含关键词的 session 文件，只读该文件的活动。
    """
    sessions_dir = OCLAW_HOME / 'agents' / agent_id / 'sessions'
    if not sessions_dir.exists():
        return []

    jsonl_files = sorted(sessions_dir.glob('*.jsonl'), key=lambda f: f.stat().st_mtime, reverse=True)
    if not jsonl_files:
        return []

    # 找到包含关键词的 session 文件
    target_file = None
    for sf in jsonl_files[:5]:
        try:
            content = sf.read_text(errors='ignore')
        except Exception:
            continue
        hits = sum(1 for kw in keywords if kw.lower() in content.lower())
        if hits >= min(2, len(keywords)):
            target_file = sf
            break

    if not target_file:
        return []

    # 解析 session 文件，按 user 消息分割为对话段
    # 找到包含关键词的对话段，只返回该段的活动
    try:
        lines = target_file.read_text(errors='ignore').splitlines()
    except Exception:
        return []

    # 第一遍：找到关键词匹配的 user 消息位置
    user_msg_indices = []  # (line_index, user_text)
    for i, ln in enumerate(lines):
        try:
            item = json.loads(ln)
        except Exception:
            continue
        msg = item.get('message') or {}
        if msg.get('role') == 'user':
            text = ''
            for c in msg.get('content', []):
                if c.get('type') == 'text' and c.get('text'):
                    text += c['text']
            user_msg_indices.append((i, text))

    # 找到与关键词匹配度最高的 user 消息
    best_idx = -1
    best_hits = 0
    for line_idx, utext in user_msg_indices:
        hits = sum(1 for kw in keywords if kw.lower() in utext.lower())
        if hits > best_hits:
            best_hits = hits
            best_idx = line_idx

    # 确定对话段的行范围：从匹配的 user 消息到下一个 user 消息之前
    if best_idx >= 0 and best_hits >= min(2, len(keywords)):
        # 找下一个 user 消息的位置
        next_user_idx = len(lines)
        for line_idx, _ in user_msg_indices:
            if line_idx > best_idx:
                next_user_idx = line_idx
                break
        start_line = best_idx
        end_line = next_user_idx
    else:
        # 没找到匹配的对话段，返回空
        return []

    # 第二遍：只解析对话段内的行
    entries = []
    for ln in lines[start_line:end_line]:
        try:
            item = json.loads(ln)
        except Exception:
            continue
        entry = _parse_activity_entry(item)
        if entry:
            entries.append(entry)

    return entries[-limit:]


def get_agent_latest_segment(agent_id, limit=20):
    """获取 Agent 最新一轮对话段（最后一条 user 消息起的所有内容）。
    用于活跃任务没有精确匹配时，展示 Agent 的实时工作状态。
    """
    sessions_dir = OCLAW_HOME / 'agents' / agent_id / 'sessions'
    if not sessions_dir.exists():
        return []

    jsonl_files = sorted(sessions_dir.glob('*.jsonl'),
                         key=lambda f: f.stat().st_mtime, reverse=True)
    if not jsonl_files:
        return []

    # 读取最新的 session 文件
    target_file = jsonl_files[0]
    try:
        lines = target_file.read_text(errors='ignore').splitlines()
    except Exception:
        return []

    # 找到最后一条 user 消息的行号
    last_user_idx = -1
    for i, ln in enumerate(lines):
        try:
            item = json.loads(ln)
        except Exception:
            continue
        msg = item.get('message') or {}
        if msg.get('role') == 'user':
            last_user_idx = i

    if last_user_idx < 0:
        return []

    # 从最后一条 user 消息开始，解析到文件末尾
    entries = []
    for ln in lines[last_user_idx:]:
        try:
            item = json.loads(ln)
        except Exception:
            continue
        entry = _parse_activity_entry(item)
        if entry:
            entries.append(entry)

    return entries[-limit:]


def _compute_phase_durations(flow_log):
    """从 flow_log 计算每个阶段的停留时长。"""
    if not flow_log or len(flow_log) < 1:
        return []
    phases = []
    for i, fl in enumerate(flow_log):
        start_at = fl.get('at', '')
        to_dept = fl.get('to', '')
        remark = fl.get('remark', '')
        # 下一阶段的起始时间就是本阶段的结束时间
        if i + 1 < len(flow_log):
            end_at = flow_log[i + 1].get('at', '')
            ongoing = False
        else:
            end_at = now_iso()
            ongoing = True
        # 计算时长
        dur_sec = 0
        try:
            from_dt = datetime.datetime.fromisoformat(start_at.replace('Z', '+00:00'))
            to_dt = datetime.datetime.fromisoformat(end_at.replace('Z', '+00:00'))
            dur_sec = max(0, int((to_dt - from_dt).total_seconds()))
        except Exception:
            pass
        # 人类可读时长
        if dur_sec < 60:
            dur_text = f'{dur_sec}秒'
        elif dur_sec < 3600:
            dur_text = f'{dur_sec // 60}分{dur_sec % 60}秒'
        elif dur_sec < 86400:
            h, rem = divmod(dur_sec, 3600)
            dur_text = f'{h}小时{rem // 60}分'
        else:
            d, rem = divmod(dur_sec, 86400)
            dur_text = f'{d}天{rem // 3600}小时'
        phases.append({
            'phase': to_dept,
            'from': start_at,
            'to': end_at,
            'durationSec': dur_sec,
            'durationText': dur_text,
            'ongoing': ongoing,
            'remark': remark,
        })
    return phases


def _compute_todos_summary(todos):
    """计算 todos 完成率汇总。"""
    if not todos:
        return None
    total = len(todos)
    completed = sum(1 for t in todos if t.get('status') == 'completed')
    in_progress = sum(1 for t in todos if t.get('status') == 'in-progress')
    not_started = total - completed - in_progress
    percent = round(completed / total * 100) if total else 0
    return {
        'total': total,
        'completed': completed,
        'inProgress': in_progress,
        'notStarted': not_started,
        'percent': percent,
    }


def _compute_todos_diff(prev_todos, curr_todos):
    """计算两个 todos 快照之间的差异。"""
    prev_map = {str(t.get('id', '')): t for t in (prev_todos or [])}
    curr_map = {str(t.get('id', '')): t for t in (curr_todos or [])}
    changed, added, removed = [], [], []
    for tid, ct in curr_map.items():
        if tid in prev_map:
            pt = prev_map[tid]
            if pt.get('status') != ct.get('status'):
                changed.append({
                    'id': tid, 'title': ct.get('title', ''),
                    'from': pt.get('status', ''), 'to': ct.get('status', ''),
                })
        else:
            added.append({'id': tid, 'title': ct.get('title', '')})
    for tid, pt in prev_map.items():
        if tid not in curr_map:
            removed.append({'id': tid, 'title': pt.get('title', '')})
    if not changed and not added and not removed:
        return None
    return {'changed': changed, 'added': added, 'removed': removed}


def get_task_activity(task_id):
    """获取任务的实时进展数据。
    数据来源：
    1. 任务自身的 now / todos / flow_log 字段（由 Agent 通过 progress 命令主动上报）
    2. Agent session JSONL 中的对话日志（thinking / tool_result / user，用于展示思考过程）

    增强字段:
    - taskMeta: 任务元信息 (title/state/org/output/block/priority/reviewRound/archived)
    - phaseDurations: 各阶段停留时长
    - todosSummary: todos 完成率汇总
    - resourceSummary: Agent 资源消耗汇总 (tokens/cost/elapsed)
    - activity 条目中 progress/todos 保留 state/org 快照
    - activity 中 todos 条目含 diff 字段
    """
    tasks = load_tasks()
    task = next((t for t in tasks if t.get('id') == task_id), None)
    if not task:
        return {'ok': False, 'error': f'任务 {task_id} 不存在'}

    state = task.get('state', '')
    org = task.get('org', '')
    now_text = task.get('now', '')
    todos = task.get('todos', [])
    updated_at = task.get('updatedAt', '')

    # ── 任务元信息 ──
    task_meta = {
        'title': task.get('title', ''),
        'state': state,
        'org': org,
        'output': task.get('output', ''),
        'block': task.get('block', ''),
        'priority': task.get('priority', 'normal'),
        'reviewRound': task.get('review_round', 0),
        'archived': task.get('archived', False),
    }

    # 当前负责 Agent（兼容旧逻辑）
    agent_id = _STATE_AGENT_MAP.get(state)
    if agent_id is None and state in ('Doing', 'Next'):
        agent_id = _ORG_AGENT_MAP.get(org)

    # ── 构建活动条目列表（flow_log + progress_log）──
    activity = []
    flow_log = task.get('flow_log', [])

    # 1. flow_log 转为活动条目
    for fl in flow_log:
        activity.append({
            'at': fl.get('at', ''),
            'kind': 'flow',
            'from': fl.get('from', ''),
            'to': fl.get('to', ''),
            'remark': fl.get('remark', ''),
        })

    progress_log = task.get('progress_log', [])
    related_agents = set()

    # 资源消耗累加
    total_tokens = 0
    total_cost = 0.0
    total_elapsed = 0
    has_resource_data = False

    # 用于 todos diff 计算
    prev_todos_snapshot = None

    if progress_log:
        # 2. 多 Agent 实时进展日志（每条 progress 都保留自己的 todo 快照）
        for pl in progress_log:
            p_at = pl.get('at', '')
            p_agent = pl.get('agent', '')
            p_text = pl.get('text', '')
            p_todos = pl.get('todos', [])
            p_state = pl.get('state', '')
            p_org = pl.get('org', '')
            if p_agent:
                related_agents.add(p_agent)
            # 累加资源消耗
            if pl.get('tokens'):
                total_tokens += pl['tokens']
                has_resource_data = True
            if pl.get('cost'):
                total_cost += pl['cost']
                has_resource_data = True
            if pl.get('elapsed'):
                total_elapsed += pl['elapsed']
                has_resource_data = True
            if p_text:
                entry = {
                    'at': p_at,
                    'kind': 'progress',
                    'text': p_text,
                    'agent': p_agent,
                    'agentLabel': pl.get('agentLabel', ''),
                    'state': p_state,
                    'org': p_org,
                }
                # 单条资源数据
                if pl.get('tokens'):
                    entry['tokens'] = pl['tokens']
                if pl.get('cost'):
                    entry['cost'] = pl['cost']
                if pl.get('elapsed'):
                    entry['elapsed'] = pl['elapsed']
                activity.append(entry)
            if p_todos:
                todos_entry = {
                    'at': p_at,
                    'kind': 'todos',
                    'items': p_todos,
                    'agent': p_agent,
                    'agentLabel': pl.get('agentLabel', ''),
                    'state': p_state,
                    'org': p_org,
                }
                # 计算 diff
                diff = _compute_todos_diff(prev_todos_snapshot, p_todos)
                if diff:
                    todos_entry['diff'] = diff
                activity.append(todos_entry)
                prev_todos_snapshot = p_todos

        # 仅当无法通过状态确定 Agent 时，才回退到最后一次上报的 Agent
        if not agent_id:
            last_pl = progress_log[-1]
            if last_pl.get('agent'):
                agent_id = last_pl.get('agent')
    else:
        # 兼容旧数据：仅使用 now/todos
        if now_text:
            activity.append({
                'at': updated_at,
                'kind': 'progress',
                'text': now_text,
                'agent': agent_id or '',
                'state': state,
                'org': org,
            })
        if todos:
            activity.append({
                'at': updated_at,
                'kind': 'todos',
                'items': todos,
                'agent': agent_id or '',
                'state': state,
                'org': org,
            })

    # 按时间排序，保证流转/进展穿插正确
    activity.sort(key=lambda x: x.get('at', ''))

    if agent_id:
        related_agents.add(agent_id)

    # ── 融合 Agent Session 活动（thinking / tool_result / user）──
    # 从 session JSONL 中提取 Agent 的思考过程和工具调用记录
    try:
        session_entries = []
        # 活跃任务：尝试按 task_id 精确匹配
        if state not in ('Done', 'Cancelled'):
            if agent_id:
                entries = get_agent_activity(agent_id, limit=30, task_id=task_id)
                session_entries.extend(entries)
            # 也从其他相关 Agent 获取
            for ra in related_agents:
                if ra != agent_id:
                    entries = get_agent_activity(ra, limit=20, task_id=task_id)
                    session_entries.extend(entries)
        else:
            # 已完成任务：基于关键词匹配
            title = task.get('title', '')
            keywords = _extract_keywords(title)
            if keywords:
                agents_to_scan = list(related_agents) if related_agents else ([agent_id] if agent_id else [])
                for ra in agents_to_scan[:5]:
                    entries = get_agent_activity_by_keywords(ra, keywords, limit=15)
                    session_entries.extend(entries)
        # 去重（通过 at+kind 去重避免重复）
        existing_keys = {(a.get('at', ''), a.get('kind', '')) for a in activity}
        for se in session_entries:
            key = (se.get('at', ''), se.get('kind', ''))
            if key not in existing_keys:
                activity.append(se)
                existing_keys.add(key)
        # 重新排序
        activity.sort(key=lambda x: x.get('at', ''))
    except Exception as e:
        log.warning(f'Session JSONL 融合失败 (task={task_id}): {e}')

    # ── 阶段耗时统计 ──
    phase_durations = _compute_phase_durations(flow_log)

    # ── Todos 汇总 ──
    todos_summary = _compute_todos_summary(todos)

    # ── 总耗时（首条 flow_log 到最后一条/当前） ──
    total_duration = None
    if flow_log:
        try:
            first_at = datetime.datetime.fromisoformat(flow_log[0].get('at', '').replace('Z', '+00:00'))
            if state in ('Done', 'Cancelled') and len(flow_log) >= 2:
                last_at = datetime.datetime.fromisoformat(flow_log[-1].get('at', '').replace('Z', '+00:00'))
            else:
                last_at = datetime.datetime.now(datetime.timezone.utc)
            dur = max(0, int((last_at - first_at).total_seconds()))
            if dur < 60:
                total_duration = f'{dur}秒'
            elif dur < 3600:
                total_duration = f'{dur // 60}分{dur % 60}秒'
            elif dur < 86400:
                h, rem = divmod(dur, 3600)
                total_duration = f'{h}小时{rem // 60}分'
            else:
                d, rem = divmod(dur, 86400)
                total_duration = f'{d}天{rem // 3600}小时'
        except Exception:
            pass

    result = {
        'ok': True,
        'taskId': task_id,
        'taskMeta': task_meta,
        'agentId': agent_id,
        'agentLabel': _STATE_LABELS.get(state, state),
        'lastActive': updated_at[:19].replace('T', ' ') if updated_at else None,
        'activity': activity,
        'activitySource': 'progress+session',
        'relatedAgents': sorted(list(related_agents)),
        'phaseDurations': phase_durations,
        'totalDuration': total_duration,
    }
    if todos_summary:
        result['todosSummary'] = todos_summary
    if has_resource_data:
        result['resourceSummary'] = {
            'totalTokens': total_tokens,
            'totalCost': round(total_cost, 4),
            'totalElapsedSec': total_elapsed,
        }
    if _activity_sensitivity() != 'full':
        result['taskMeta'] = redact_sensitive_data(result['taskMeta'], _activity_sensitivity())
        result['activity'] = redact_sensitive_data(result['activity'], _activity_sensitivity())
    return result


# 状态推进顺序（手动推进用）
_STATE_LABELS = dict(STATE_LABELS)


def dispatch_for_state(task_id, task, new_state, trigger='state-transition', actor=None):
    """推进/审批后自动派发对应 Agent（后台异步，不阻塞响应）。"""
    actor = actor or make_actor_context('system', source='scheduler')
    ensure_task_shape(task)
    new_state = canonicalize_state(new_state)
    agent_id = resolve_dispatch_agent(task, new_state)
    if not agent_id:
        log.info(f'ℹ️ {task_id} 新状态 {new_state} 无对应 Agent，跳过自动派发')
        return
    allowed, deny_reason = authorize_dispatch(actor, task, agent_id)
    if not allowed:
        _record_task_audit(task_id, 'task.dispatch', actor, False, from_state=task.get('state', ''), to_state=new_state, target_agent=agent_id, deny_reason=deny_reason, payload={'trigger': trigger})
        log.warning(f'🚫 {task_id} 派发被拒绝: {deny_reason}')
        return
    sched = dict((task.get('_scheduler') or {}))
    version = max(1, int(task.get('_stateVersion') or 1))
    dispatch_key = build_dispatch_key(task_id, new_state, version, agent_id, manual=False)
    if sched.get('lastDispatchKey') == dispatch_key and sched.get('lastDispatchStatus') in {'queued', 'success'}:
        log.info(f'🛑 {task_id} 跳过重复派发 {dispatch_key}')
        return

    _update_task_scheduler(task_id, lambda t, s: (
        s.update({
            'lastDispatchAt': now_iso(),
            'lastDispatchStatus': 'queued',
            'lastDispatchAgent': agent_id,
            'lastDispatchTrigger': trigger,
            'lastDispatchKey': dispatch_key,
        }),
        _scheduler_add_flow(t, f'已入队派发：{new_state} → {agent_id}（{trigger}）', to=_STATE_LABELS.get(new_state, new_state))
    ))
    _record_task_audit(task_id, 'task.dispatch', actor, True, from_state=task.get('state', ''), to_state=new_state, target_agent=agent_id, payload={'trigger': trigger, 'dispatchKey': dispatch_key, 'version': version})

    title = task.get('title', '(无标题)')
    target_dept = task.get('targetDept', '')

    # 根据 agent_id 构造针对性消息
    _msgs = {
        'sili': (
            f'📜 皇上旨意需要你处理\n'
            f'任务ID: {task_id}\n'
            f'旨意: {title}\n'
            f'⚠️ 看板已有此任务，请勿重复创建。直接用 kanban_update.py 更新状态。\n'
            f'请立即转交中书省起草执行方案。'
        ),
        'zhongshu': (
            f'📜 旨意已到中书省，请起草方案\n'
            f'任务ID: {task_id}\n'
            f'旨意: {title}\n'
            f'⚠️ 看板已有此任务记录，请勿重复创建。直接用 kanban_update.py state 更新状态。\n'
            f'请立即起草执行方案，走完完整三省流程（中书起草→门下审议→尚书派发→六部执行）。'
        ),
        'menxia': (
            f'📋 中书省方案提交审议\n'
            f'任务ID: {task_id}\n'
            f'旨意: {title}\n'
            f'⚠️ 看板已有此任务，请勿重复创建。\n'
            f'请审议中书省方案，给出准奏或封驳意见。'
        ),
        'shangshu': (
            f'📮 门下省已准奏，请派发执行\n'
            f'任务ID: {task_id}\n'
            f'旨意: {title}\n'
            f'{"建议派发部门: " + target_dept if target_dept else ""}\n'
            f'⚠️ 看板已有此任务，请勿重复创建。\n'
            f'请分析方案并派发给六部执行。'
        ),
    }
    msg = _msgs.get(agent_id, (
        f'📌 请处理任务\n'
        f'任务ID: {task_id}\n'
        f'旨意: {title}\n'
        f'⚠️ 看板已有此任务，请勿重复创建。直接用 kanban_update.py 更新状态。'
    ))

    def _do_dispatch():
        try:
            if not _check_gateway_alive():
                log.warning(f'⚠️ {task_id} 自动派发跳过: Gateway 未启动')
                _update_task_scheduler(task_id, lambda t, s: s.update({
                    'lastDispatchAt': now_iso(),
                    'lastDispatchStatus': 'gateway-offline',
                    'lastDispatchAgent': agent_id,
                    'lastDispatchTrigger': trigger,
                    'lastDispatchKey': dispatch_key,
                }))
                return
            # Fix #139: dispatch channel 可配置（默认 feishu，支持 telegram/wecom/signal 等）
            _agent_cfg = read_json(DATA / 'agent_config.json', {})
            _channel = (_agent_cfg.get('dispatchChannel') or 'feishu').strip()
            cmd = ['openclaw', 'agent', '--agent', agent_id, '-m', msg,
                   '--deliver', '--channel', _channel, '--timeout', '300']
            max_retries = 2
            err = ''
            for attempt in range(1, max_retries + 1):
                log.info(f'🔄 自动派发 {task_id} → {agent_id} (第{attempt}次)...')
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=310)
                if result.returncode == 0:
                    log.info(f'✅ {task_id} 自动派发成功 → {agent_id}')
                    _update_task_scheduler(task_id, lambda t, s: (
                        s.update({
                            'lastDispatchAt': now_iso(),
                            'lastDispatchStatus': 'success',
                            'lastDispatchAgent': agent_id,
                            'lastDispatchTrigger': trigger,
                            'lastDispatchKey': dispatch_key,
                            'lastDispatchError': '',
                        }),
                        _scheduler_add_flow(t, f'派发成功：{agent_id}（{trigger}）', to=t.get('org', ''))
                    ))
                    return
                err = result.stderr[:200] if result.stderr else result.stdout[:200]
                log.warning(f'⚠️ {task_id} 自动派发失败(第{attempt}次): {err}')
                if attempt < max_retries:
                    import time
                    time.sleep(5)
            log.error(f'❌ {task_id} 自动派发最终失败 → {agent_id}')
            _update_task_scheduler(task_id, lambda t, s: (
                s.update({
                    'lastDispatchAt': now_iso(),
                    'lastDispatchStatus': 'failed',
                    'lastDispatchAgent': agent_id,
                    'lastDispatchTrigger': trigger,
                    'lastDispatchKey': dispatch_key,
                    'lastDispatchError': err,
                }),
                _scheduler_add_flow(t, f'派发失败：{agent_id}（{trigger}）', to=t.get('org', ''))
            ))
        except subprocess.TimeoutExpired:
            log.error(f'❌ {task_id} 自动派发超时 → {agent_id}')
            _update_task_scheduler(task_id, lambda t, s: (
                s.update({
                    'lastDispatchAt': now_iso(),
                    'lastDispatchStatus': 'timeout',
                    'lastDispatchAgent': agent_id,
                    'lastDispatchTrigger': trigger,
                    'lastDispatchKey': dispatch_key,
                    'lastDispatchError': 'timeout',
                }),
                _scheduler_add_flow(t, f'派发超时：{agent_id}（{trigger}）', to=t.get('org', ''))
            ))
        except Exception as e:
            log.warning(f'⚠️ {task_id} 自动派发异常: {e}')
            _update_task_scheduler(task_id, lambda t, s: (
                s.update({
                    'lastDispatchAt': now_iso(),
                    'lastDispatchStatus': 'error',
                    'lastDispatchAgent': agent_id,
                    'lastDispatchTrigger': trigger,
                    'lastDispatchKey': dispatch_key,
                    'lastDispatchError': str(e)[:200],
                }),
                _scheduler_add_flow(t, f'派发异常：{agent_id}（{trigger}）', to=t.get('org', ''))
            ))

    threading.Thread(target=_do_dispatch, daemon=True).start()
    log.info(f'🚀 {task_id} 推进后自动派发 → {agent_id}')


def handle_advance_state(task_id, comment='', actor=None):
    return legacy_tasks.handle_advance_state(task_id, comment=comment, actor=actor)


def _handle_advance_state_update(task, comment, actor):
    return legacy_tasks._handle_advance_state_update(task, comment, actor)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # 只记录 4xx/5xx 错误请求
        if args and len(args) >= 1:
            status = str(args[0]) if args else ''
            if status.startswith('4') or status.startswith('5'):
                log.warning(f'{self.client_address[0]} {fmt % args}')

    def handle_error(self):
        pass  # 静默处理连接错误，避免 BrokenPipe 崩溃

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            pass  # 客户端断开连接，忽略

    def do_OPTIONS(self):
        self.send_response(200)
        cors_headers(self)
        self.end_headers()

    def send_json(self, data, code=200):
        try:
            body = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            cors_headers(self)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_file(self, path: pathlib.Path, mime='text/html; charset=utf-8'):
        if not path.exists():
            self.send_error(404)
            return
        try:
            body = path.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            cors_headers(self)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _serve_static(self, rel_path):
        """从 dist/ 目录提供静态文件。"""
        safe = rel_path.replace('\\', '/').lstrip('/')
        if '..' in safe:
            self.send_error(403)
            return True
        fp = DIST / safe
        if fp.is_file():
            mime = _MIME_TYPES.get(fp.suffix.lower(), 'application/octet-stream')
            self.send_file(fp, mime)
            return True
        return False

    def do_GET(self):
        p = urlparse(self.path).path.rstrip('/')
        if p in ('', '/dashboard', '/dashboard.html'):
            self.send_file(DIST / 'index.html')
        elif _should_proxy_control_plane(p):
            status, payload = _proxy_control_plane('GET', p)
            self.send_json(payload, status)
        elif p == '/healthz':
            checks = {'dataDir': DATA.is_dir(), 'tasksReadable': (DATA / 'tasks_source.json').exists()}
            checks['dataWritable'] = os.access(str(DATA), os.W_OK)
            all_ok = all(checks.values())
            self.send_json({'status': 'ok' if all_ok else 'degraded', 'ts': now_iso(), 'checks': checks})
        elif p == '/api/live-status':
            self.send_json(read_json(DATA / 'live_status.json'))
        elif p == '/api/queue-metrics':
            self.send_json(get_queue_metrics())
        elif p == '/api/agent-config':
            self.send_json(read_json(DATA / 'agent_config.json'))
        elif p == '/api/model-change-log':
            self.send_json(read_json(DATA / 'model_change_log.json', []))
        elif p == '/api/last-result':
            self.send_json(read_json(DATA / 'last_model_change_result.json', {}))
        elif p == '/api/officials-stats':
            self.send_json(read_json(DATA / 'officials_stats.json', {}))
        elif p == '/api/morning-brief':
            self.send_json(read_json(DATA / 'morning_brief.json', {}))
        elif p == '/api/morning-config':
            self.send_json(read_json(DATA / 'morning_brief_config.json', {
                'categories': [
                    {'name': '政治', 'enabled': True},
                    {'name': '军事', 'enabled': True},
                    {'name': '经济', 'enabled': True},
                    {'name': 'AI大模型', 'enabled': True},
                ],
                'keywords': [], 'custom_feeds': [], 'feishu_webhook': '',
            }))
        elif p.startswith('/api/morning-brief/'):
            date = p.split('/')[-1]
            # 标准化日期格式为 YYYYMMDD（兼容 YYYY-MM-DD 输入）
            date_clean = date.replace('-', '')
            if not date_clean.isdigit() or len(date_clean) != 8:
                self.send_json({'ok': False, 'error': f'日期格式无效: {date}，请使用 YYYYMMDD'}, 400)
                return
            self.send_json(read_json(DATA / f'morning_brief_{date_clean}.json', {}))
        elif p == '/api/remote-skills-list':
            self.send_json(get_remote_skills_list())
        elif p.startswith('/api/skill-content/'):
            # /api/skill-content/{agentId}/{skillName}
            parts = p.replace('/api/skill-content/', '').split('/', 1)
            if len(parts) == 2:
                self.send_json(read_skill_content(parts[0], parts[1]))
            else:
                self.send_json({'ok': False, 'error': 'Usage: /api/skill-content/{agentId}/{skillName}'}, 400)
        elif p.startswith('/api/task-activity/'):
            task_id = p.replace('/api/task-activity/', '')
            if not task_id:
                self.send_json({'ok': False, 'error': 'task_id required'}, 400)
            else:
                self.send_json(get_task_activity(task_id))
        elif p.startswith('/api/scheduler-state/'):
            task_id = p.replace('/api/scheduler-state/', '')
            if not task_id:
                self.send_json({'ok': False, 'error': 'task_id required'}, 400)
            else:
                self.send_json(get_scheduler_state(task_id))
        elif p == '/api/agents-status':
            self.send_json(get_agents_status())
        elif p.startswith('/api/agent-activity/'):
            agent_id = p.replace('/api/agent-activity/', '')
            if not agent_id or not _SAFE_NAME_RE.match(agent_id):
                self.send_json({'ok': False, 'error': 'invalid agent_id'}, 400)
            else:
                self.send_json({'ok': True, 'agentId': agent_id, 'activity': get_agent_activity(agent_id)})
        # ── 朝堂议政 ──
        elif p == '/api/court-discuss/list':
            self.send_json({'ok': True, 'sessions': cd_list()})
        elif p == '/api/court-discuss/officials':
            self.send_json({'ok': True, 'officials': CD_PROFILES})
        elif p.startswith('/api/court-discuss/session/'):
            sid = p.replace('/api/court-discuss/session/', '')
            data = cd_get(sid)
            self.send_json(data if data else {'ok': False, 'error': 'session not found'}, 200 if data else 404)
        elif p == '/api/court-discuss/fate':
            self.send_json({'ok': True, 'event': cd_fate()})
        elif self._serve_static(p):
            pass  # 已由 _serve_static 处理 (JS/CSS/图片等)
        else:
            # SPA fallback：非 /api/ 路径返回 index.html
            if not p.startswith('/api/'):
                idx = DIST / 'index.html'
                if idx.exists():
                    self.send_file(idx)
                    return
            self.send_error(404)

    def do_POST(self):
        p = urlparse(self.path).path.rstrip('/')
        length = int(self.headers.get('Content-Length', 0))
        if length > MAX_REQUEST_BODY:
            self.send_json({'ok': False, 'error': f'Request body too large (max {MAX_REQUEST_BODY} bytes)'}, 413)
            return
        raw = self.rfile.read(length) if length else b''
        try:
            body = json.loads(raw) if raw else {}
        except Exception:
            self.send_json({'ok': False, 'error': 'invalid JSON'}, 400)
            return

        if p.startswith('/api/') and not _legacy_write_allowed(self):
            self.send_json({'ok': False, 'error': 'remote write access requires admin token'}, 403)
            return

        if _should_proxy_control_plane(p):
            status, payload = _proxy_control_plane('POST', p, body)
            self.send_json(payload, status)
            return

        if p == '/api/morning-config':
            # 字段校验
            if not isinstance(body, dict):
                self.send_json({'ok': False, 'error': '请求体必须是 JSON 对象'}, 400)
                return
            allowed_keys = {'categories', 'keywords', 'custom_feeds', 'feishu_webhook'}
            unknown = set(body.keys()) - allowed_keys
            if unknown:
                self.send_json({'ok': False, 'error': f'未知字段: {", ".join(unknown)}'}, 400)
                return
            if 'categories' in body and not isinstance(body['categories'], list):
                self.send_json({'ok': False, 'error': 'categories 必须是数组'}, 400)
                return
            if 'keywords' in body and not isinstance(body['keywords'], list):
                self.send_json({'ok': False, 'error': 'keywords 必须是数组'}, 400)
                return
            # 飞书 Webhook 校验
            webhook = body.get('feishu_webhook', '').strip()
            if webhook and not validate_url(webhook, allowed_schemes=('https',), allowed_domains=('open.feishu.cn', 'open.larksuite.com')):
                self.send_json({'ok': False, 'error': '飞书 Webhook URL 无效，仅支持 https://open.feishu.cn 或 open.larksuite.com 域名'}, 400)
                return
            cfg_path = DATA / 'morning_brief_config.json'
            cfg_path.write_text(json.dumps(body, ensure_ascii=False, indent=2))
            self.send_json({'ok': True, 'message': '订阅配置已保存'})
            return

        if p == '/api/scheduler-scan':
            threshold_sec = body.get('thresholdSec', 180)
            try:
                actor = _actor_from_request(self.headers, body, default_actor='sili', default_source='scheduler')
                result = handle_scheduler_scan(threshold_sec, actor=actor)
                self.send_json(result)
            except Exception as e:
                self.send_json({'ok': False, 'error': f'scheduler scan failed: {e}'}, 500)
            return

        if p == '/api/repair-flow-order':
            try:
                self.send_json(handle_repair_flow_order())
            except Exception as e:
                self.send_json({'ok': False, 'error': f'repair flow order failed: {e}'}, 500)
            return

        if p == '/api/scheduler-retry':
            task_id = body.get('taskId', '').strip()
            reason = body.get('reason', '').strip()
            if not task_id:
                self.send_json({'ok': False, 'error': 'taskId required'}, 400)
                return
            actor = _actor_from_request(self.headers, body, default_actor='sili', default_source='scheduler')
            self.send_json(handle_scheduler_retry(task_id, reason, actor=actor))
            return

        if p == '/api/scheduler-escalate':
            task_id = body.get('taskId', '').strip()
            reason = body.get('reason', '').strip()
            if not task_id:
                self.send_json({'ok': False, 'error': 'taskId required'}, 400)
                return
            actor = _actor_from_request(self.headers, body, default_actor='sili', default_source='scheduler')
            self.send_json(handle_scheduler_escalate(task_id, reason, actor=actor))
            return

        if p == '/api/scheduler-rollback':
            task_id = body.get('taskId', '').strip()
            reason = body.get('reason', '').strip()
            if not task_id:
                self.send_json({'ok': False, 'error': 'taskId required'}, 400)
                return
            actor = _actor_from_request(self.headers, body, default_actor='sili', default_source='scheduler')
            self.send_json(handle_scheduler_rollback(task_id, reason, actor=actor))
            return

        if p == '/api/morning-brief/refresh':
            force = body.get('force', True)  # 从看板手动触发默认强制
            def do_refresh():
                try:
                    cmd = ['python3', str(SCRIPTS / 'fetch_morning_news.py')]
                    if force:
                        cmd.append('--force')
                    subprocess.run(cmd, timeout=120)
                    push_to_feishu()
                except Exception as e:
                    print(f'[refresh error] {e}', file=sys.stderr)
            threading.Thread(target=do_refresh, daemon=True).start()
            self.send_json({'ok': True, 'message': '采集已触发，约30-60秒后刷新'})
            return

        if p == '/api/add-skill':
            agent_id = body.get('agentId', '').strip()
            skill_name = body.get('skillName', body.get('name', '')).strip()
            desc = body.get('description', '').strip() or skill_name
            trigger = body.get('trigger', '').strip()
            if not agent_id or not skill_name:
                self.send_json({'ok': False, 'error': 'agentId and skillName required'}, 400)
                return
            result = add_skill_to_agent(agent_id, skill_name, desc, trigger)
            self.send_json(result)
            return

        if p == '/api/add-remote-skill':
            agent_id = body.get('agentId', '').strip()
            skill_name = body.get('skillName', '').strip()
            source_url = body.get('sourceUrl', '').strip()
            description = body.get('description', '').strip()
            if not agent_id or not skill_name or not source_url:
                self.send_json({'ok': False, 'error': 'agentId, skillName, and sourceUrl required'}, 400)
                return
            result = add_remote_skill(agent_id, skill_name, source_url, description)
            self.send_json(result)
            return

        if p == '/api/remote-skills-list':
            result = get_remote_skills_list()
            self.send_json(result)
            return

        if p == '/api/update-remote-skill':
            agent_id = body.get('agentId', '').strip()
            skill_name = body.get('skillName', '').strip()
            if not agent_id or not skill_name:
                self.send_json({'ok': False, 'error': 'agentId and skillName required'}, 400)
                return
            result = update_remote_skill(agent_id, skill_name)
            self.send_json(result)
            return

        if p == '/api/remove-remote-skill':
            agent_id = body.get('agentId', '').strip()
            skill_name = body.get('skillName', '').strip()
            if not agent_id or not skill_name:
                self.send_json({'ok': False, 'error': 'agentId and skillName required'}, 400)
                return
            result = remove_remote_skill(agent_id, skill_name)
            self.send_json(result)
            return

        if p == '/api/task-action':
            task_id = body.get('taskId', '').strip()
            action = body.get('action', '').strip()  # stop, cancel, resume
            reason = body.get('reason', '').strip() or f'皇上从看板{action}'
            if not task_id or action not in ('stop', 'cancel', 'resume'):
                self.send_json({'ok': False, 'error': 'taskId and action(stop/cancel/resume) required'}, 400)
                return
            actor = _actor_from_request(self.headers, body)
            result = handle_task_action(task_id, action, reason, actor=actor)
            self.send_json(result)
            return

        if p == '/api/task-consult':
            task_id = body.get('taskId', '').strip()
            target_agent = body.get('targetAgent', '').strip()
            note = body.get('note', '').strip()
            if not task_id or not target_agent:
                self.send_json({'ok': False, 'error': 'taskId and targetAgent required'}, 400)
                return
            actor = _actor_from_request(self.headers, body)
            result = handle_task_consult(task_id, target_agent, note, actor=actor)
            self.send_json(result)
            return

        if p == '/api/archive-task':
            task_id = body.get('taskId', '').strip() if body.get('taskId') else ''
            archived = body.get('archived', True)
            archive_all = body.get('archiveAllDone', False)
            if not task_id and not archive_all:
                self.send_json({'ok': False, 'error': 'taskId or archiveAllDone required'}, 400)
                return
            actor = _actor_from_request(self.headers, body)
            result = handle_archive_task(task_id, archived, archive_all, actor=actor)
            self.send_json(result)
            return

        if p == '/api/task-todos':
            task_id = body.get('taskId', '').strip()
            todos = body.get('todos', [])  # [{id, title, status}]
            if not task_id:
                self.send_json({'ok': False, 'error': 'taskId required'}, 400)
                return
            # todos 输入校验
            if not isinstance(todos, list) or len(todos) > 200:
                self.send_json({'ok': False, 'error': 'todos must be a list (max 200 items)'}, 400)
                return
            valid_statuses = {'not-started', 'in-progress', 'completed'}
            for td in todos:
                if not isinstance(td, dict) or 'id' not in td or 'title' not in td:
                    self.send_json({'ok': False, 'error': 'each todo must have id and title'}, 400)
                    return
                if td.get('status', 'not-started') not in valid_statuses:
                    td['status'] = 'not-started'
            actor = _actor_from_request(self.headers, body)
            result = update_task_todos(task_id, todos, actor=actor)
            self.send_json(result)
            return

        if p == '/api/create-task':
            title = body.get('title', '').strip()
            org = body.get('org', '中书省').strip()
            official = body.get('official', '中书令').strip()
            priority = body.get('priority', 'normal').strip()
            lane = body.get('lane', 'standard').strip()
            template_id = body.get('templateId', '')
            params = body.get('params', {})
            if not title:
                self.send_json({'ok': False, 'error': 'title required'}, 400)
                return
            target_dept = body.get('targetDept', '').strip()
            actor = _actor_from_request(self.headers, body)
            result = handle_create_task(title, org, official, priority, lane, template_id, params, target_dept, actor=actor)
            self.send_json(result)
            return

        if p == '/api/review-action':
            task_id = body.get('taskId', '').strip()
            action = body.get('action', '').strip()  # approve, reject
            comment = body.get('comment', '').strip()
            if not task_id or action not in ('approve', 'reject'):
                self.send_json({'ok': False, 'error': 'taskId and action(approve/reject) required'}, 400)
                return
            actor = _actor_from_request(self.headers, body)
            result = handle_review_action(task_id, action, comment, actor=actor)
            self.send_json(result)
            return

        if p == '/api/advance-state':
            task_id = body.get('taskId', '').strip()
            comment = body.get('comment', '').strip()
            if not task_id:
                self.send_json({'ok': False, 'error': 'taskId required'}, 400)
                return
            actor = _actor_from_request(self.headers, body)
            result = handle_advance_state(task_id, comment, actor=actor)
            self.send_json(result)
            return

        if p == '/api/agent-wake':
            agent_id = body.get('agentId', '').strip()
            message = body.get('message', '').strip()
            if not agent_id:
                self.send_json({'ok': False, 'error': 'agentId required'}, 400)
                return
            actor = _actor_from_request(self.headers, body)
            result = wake_agent(agent_id, message, actor=actor)
            self.send_json(result)
            return

        if p == '/api/set-model':
            agent_id = body.get('agentId', '').strip()
            model = body.get('model', '').strip()
            if not agent_id or not model:
                self.send_json({'ok': False, 'error': 'agentId and model required'}, 400)
                return

            # Write to pending (atomic)
            pending_path = DATA / 'pending_model_changes.json'
            def update_pending(current):
                current = [x for x in current if x.get('agentId') != agent_id]
                current.append({'agentId': agent_id, 'model': model})
                return current
            atomic_json_update(pending_path, update_pending, [])

            # Async apply
            def apply_async():
                try:
                    subprocess.run(['python3', str(SCRIPTS / 'apply_model_changes.py')], timeout=30)
                    subprocess.run(['python3', str(SCRIPTS / 'sync_agent_config.py')], timeout=10)
                except Exception as e:
                    print(f'[apply error] {e}', file=sys.stderr)

            threading.Thread(target=apply_async, daemon=True).start()
            self.send_json({'ok': True, 'message': f'Queued: {agent_id} → {model}'})

        # Fix #139: 设置派发渠道（feishu/telegram/wecom/signal/tui）
        elif p == '/api/set-dispatch-channel':
            channel = body.get('channel', '').strip()
            allowed = {'feishu', 'telegram', 'wecom', 'signal', 'tui', 'discord', 'slack'}
            if not channel or channel not in allowed:
                self.send_json({'ok': False, 'error': f'channel must be one of: {", ".join(sorted(allowed))}'}, 400)
                return
            def _set_channel(cfg):
                cfg['dispatchChannel'] = channel
                return cfg
            atomic_json_update(DATA / 'agent_config.json', _set_channel, {})
            self.send_json({'ok': True, 'message': f'派发渠道已切换为 {channel}'})

        # ── 朝堂议政 POST ──
        elif p == '/api/court-discuss/start':
            topic = body.get('topic', '').strip()
            officials = body.get('officials', [])
            task_id = body.get('taskId', '').strip()
            if not topic:
                self.send_json({'ok': False, 'error': 'topic required'}, 400)
                return
            if not officials or not isinstance(officials, list):
                self.send_json({'ok': False, 'error': 'officials list required'}, 400)
                return
            # 校验官员 ID
            valid_ids = set(CD_PROFILES.keys())
            officials = [o for o in officials if o in valid_ids]
            if len(officials) < 2:
                self.send_json({'ok': False, 'error': '至少选择2位官员'}, 400)
                return
            self.send_json(cd_create(topic, officials, task_id))

        elif p == '/api/court-discuss/advance':
            sid = body.get('sessionId', '').strip()
            user_msg = body.get('userMessage', '').strip() or None
            decree = body.get('decree', '').strip() or None
            if not sid:
                self.send_json({'ok': False, 'error': 'sessionId required'}, 400)
                return
            self.send_json(cd_advance(sid, user_msg, decree))

        elif p == '/api/court-discuss/conclude':
            sid = body.get('sessionId', '').strip()
            if not sid:
                self.send_json({'ok': False, 'error': 'sessionId required'}, 400)
                return
            self.send_json(cd_conclude(sid))

        elif p == '/api/court-discuss/destroy':
            sid = body.get('sessionId', '').strip()
            if sid:
                cd_destroy(sid)
            self.send_json({'ok': True})

        else:
            self.send_error(404)


def main():
    parser = argparse.ArgumentParser(description='三省六部看板服务器')
    parser.add_argument('--port', type=int, default=7891)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--cors', default=None, help='Allowed CORS origin (default: reflect request Origin header)')
    args = parser.parse_args()

    global ALLOWED_ORIGIN
    ALLOWED_ORIGIN = args.cors

    server = HTTPServer((args.host, args.port), Handler)
    log.info(f'三省六部看板启动 → http://{args.host}:{args.port}')
    print(f'   按 Ctrl+C 停止')

    # 启动恢复：重新派发上次被 kill 中断的 queued 任务
    threading.Timer(3.0, _startup_recover_queued_dispatches).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n已停止')


if __name__ == '__main__':
    main()
