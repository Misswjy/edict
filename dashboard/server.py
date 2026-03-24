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
)
from app.task_contract import (
    ActorContext,
    AGENT_ORG_MAP,
    ORG_AGENT_MAP,
    STATE_LABELS,
    TERMINAL_STATES,
    VALID_TRANSITIONS,
    authorize_agent_wake,
    authorize_dispatch,
    authorize_review,
    authorize_scheduler,
    authorize_task_action,
    authorize_todos,
    authorize_transition,
    build_audit_entry,
    canonicalize_actor,
    canonicalize_state,
    ensure_execution_assignment,
    ensure_task_shape,
    make_actor_context,
    next_manual_transition,
    build_dispatch_key,
    resolve_dispatch_agent,
    resolve_execution_org,
    review_transition,
)

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
    h.send_header('Access-Control-Allow-Headers', 'Content-Type')


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
        '/api/create-task',
        '/api/task-action',
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


def handle_task_action(task_id, action, reason, actor=None):
    """Stop/cancel/resume a task from the dashboard."""
    actor = actor or make_actor_context('emperor', source='dashboard')
    result = _with_task(task_id, lambda task, _tasks: _handle_task_action_update(task, action, reason, actor))
    if result is None:
        _record_task_audit(task_id, f'task.{action}', actor, False, deny_reason='task not found', payload={'reason': reason})
        return {'ok': False, 'error': f'任务 {task_id} 不存在'}
    if not result['allowed']:
        return {'ok': False, 'error': result['error']}
    if result.get('dispatch_state'):
        dispatch_for_state(task_id, result['task'], result['dispatch_state'], trigger='resume', actor=actor)
    return {'ok': True, 'message': result['message']}


def _handle_task_action_update(task, action, reason, actor):
    ensure_task_shape(task)
    allowed, deny_reason = authorize_task_action(actor, task, action)
    if not allowed:
        _record_task_audit(task.get('id', ''), f'task.{action}', actor, False, from_state=task.get('state', ''), deny_reason=deny_reason, payload={'reason': reason})
        return {'allowed': False, 'error': deny_reason}

    old_state = canonicalize_state(task.get('state', ''))
    _ensure_scheduler(task)
    _scheduler_snapshot(task, f'task-action-before-{action}')

    if action == 'stop':
        if old_state in TERMINAL_STATES or old_state == 'Blocked':
            deny_reason = f'任务当前状态 {old_state} 不支持 stop'
            _record_task_audit(task.get('id', ''), 'task.stop', actor, False, from_state=old_state, deny_reason=deny_reason, payload={'reason': reason})
            return {'allowed': False, 'error': deny_reason}
        task['_prev_state'] = old_state
        task['state'] = 'Blocked'
        task['block'] = reason or '皇上叫停'
        task['now'] = f'⏸️ 已暂停：{reason or "皇上叫停"}'
    elif action == 'cancel':
        if old_state in TERMINAL_STATES:
            deny_reason = f'任务当前状态 {old_state} 不支持 cancel'
            _record_task_audit(task.get('id', ''), 'task.cancel', actor, False, from_state=old_state, deny_reason=deny_reason, payload={'reason': reason})
            return {'allowed': False, 'error': deny_reason}
        task['_prev_state'] = old_state
        task['state'] = 'Cancelled'
        task['org'] = '皇上'
        task['block'] = reason or '皇上取消'
        task['now'] = f'🚫 已取消：{reason or "皇上取消"}'
    elif action == 'resume':
        if old_state != 'Blocked':
            deny_reason = f'任务当前状态 {old_state} 不支持 resume'
            _record_task_audit(task.get('id', ''), 'task.resume', actor, False, from_state=old_state, deny_reason=deny_reason, payload={'reason': reason})
            return {'allowed': False, 'error': deny_reason}
        previous_state = canonicalize_state(task.get('_prev_state') or '')
        if not previous_state or previous_state in TERMINAL_STATES:
            deny_reason = 'Blocked 任务没有可恢复的非终态快照'
            _record_task_audit(task.get('id', ''), 'task.resume', actor, False, from_state=old_state, deny_reason=deny_reason, payload={'reason': reason})
            return {'allowed': False, 'error': deny_reason}
        ok, reason_text = ensure_execution_assignment(task, previous_state)
        if not ok:
            _record_task_audit(task.get('id', ''), 'task.resume', actor, False, from_state=old_state, to_state=previous_state, deny_reason=reason_text, payload={'reason': reason})
            return {'allowed': False, 'error': reason_text}
        task['state'] = previous_state
        task['block'] = '无'
        task['now'] = '▶️ 已恢复执行'
    else:
        deny_reason = f'未知任务动作: {action}'
        _record_task_audit(task.get('id', ''), f'task.{action}', actor, False, from_state=old_state, deny_reason=deny_reason)
        return {'allowed': False, 'error': deny_reason}

    task.setdefault('flow_log', []).append({
        'at': now_iso(),
        'from': '皇上',
        'to': task.get('org', ''),
        'remark': f'{"⏸️ 叫停" if action == "stop" else "🚫 取消" if action == "cancel" else "▶️ 恢复"}：{reason}'
    })

    if action == 'resume':
        _scheduler_mark_progress(task, f'恢复到 {task.get("state", "Doing")}')
    else:
        _scheduler_add_flow(task, f'皇上{action}：{reason or "无"}')

    _bump_task_version(task)
    task['updatedAt'] = now_iso()
    label = {'stop': '已叫停', 'cancel': '已取消', 'resume': '已恢复'}[action]
    _record_task_audit(
        task.get('id', ''),
        f'task.{action}',
        actor,
        True,
        from_state=old_state,
        to_state=task.get('state', ''),
        payload={'reason': reason},
    )
    return {
        'allowed': True,
        'message': f'{task.get("id", "")} {label}',
        'dispatch_state': task.get('state') if action == 'resume' else '',
        'task': json.loads(json.dumps(task, ensure_ascii=False)),
    }


def handle_archive_task(task_id, archived, archive_all_done=False, actor=None):
    """Archive or unarchive a task, or batch-archive all Done/Cancelled tasks."""
    actor = actor or make_actor_context('emperor', source='dashboard')
    if archive_all_done:
        count_box = {'count': 0}

        def modifier(tasks):
            for task in tasks:
                if not isinstance(task, dict):
                    continue
                ensure_task_shape(task)
                if task.get('state') in ('Done', 'Cancelled') and not task.get('archived'):
                    task['archived'] = True
                    task['archivedAt'] = now_iso()
                    task['updatedAt'] = now_iso()
                    count_box['count'] += 1
            return tasks

        _atomic_update_tasks(modifier)
        _record_task_audit('', 'task.archive_all_done', actor, True, payload={'count': count_box['count']})
        return {'ok': True, 'message': f'{count_box["count"]} 道旨意已归档', 'count': count_box['count']}
    if canonicalize_actor(actor.actor_id) not in {'emperor', 'system'}:
        deny_reason = f"actor {actor.actor_id} 无权执行归档"
        _record_task_audit(task_id, 'task.archive', actor, False, deny_reason=deny_reason, payload={'archived': archived})
        return {'ok': False, 'error': deny_reason}
    result = _with_task(task_id, lambda task, _tasks: _handle_archive_task_update(task, archived, actor))
    if result is None:
        _record_task_audit(task_id, 'task.archive', actor, False, deny_reason='task not found', payload={'archived': archived})
        return {'ok': False, 'error': f'任务 {task_id} 不存在'}
    return result


def _handle_archive_task_update(task, archived, actor):
    ensure_task_shape(task)
    task['archived'] = bool(archived)
    if archived:
        task['archivedAt'] = now_iso()
    else:
        task.pop('archivedAt', None)
    task['updatedAt'] = now_iso()
    _record_task_audit(task.get('id', ''), 'task.archive', actor, True, from_state=task.get('state', ''), payload={'archived': archived})
    label = '已归档' if archived else '已取消归档'
    return {'ok': True, 'message': f'{task.get("id", "")} {label}'}


def update_task_todos(task_id, todos, actor=None):
    """Update the todos list for a task."""
    actor = actor or make_actor_context('emperor', source='dashboard')
    result = _with_task(task_id, lambda task, _tasks: _update_task_todos_update(task, todos, actor))
    if result is None:
        _record_task_audit(task_id, 'task.todos', actor, False, deny_reason='task not found', payload={'todos': todos})
        return {'ok': False, 'error': f'任务 {task_id} 不存在'}
    return result


def _update_task_todos_update(task, todos, actor):
    ensure_task_shape(task)
    allowed, deny_reason = authorize_todos(actor, task)
    if not allowed:
        _record_task_audit(task.get('id', ''), 'task.todos', actor, False, from_state=task.get('state', ''), deny_reason=deny_reason, payload={'todos': todos})
        return {'ok': False, 'error': deny_reason}
    task['todos'] = todos
    task['updatedAt'] = now_iso()
    _record_task_audit(task.get('id', ''), 'task.todos', actor, True, from_state=task.get('state', ''), payload={'todo_count': len(todos)})
    return {'ok': True, 'message': f'{task.get("id", "")} todos 已更新'}


def read_skill_content(agent_id, skill_name):
    """Read SKILL.md content for a specific skill."""
    # 输入校验：防止路径遍历
    if not _SAFE_NAME_RE.match(agent_id) or not _SAFE_NAME_RE.match(skill_name):
        return {'ok': False, 'error': '参数含非法字符'}
    cfg = read_json(DATA / 'agent_config.json', {})
    agents = cfg.get('agents', [])
    ag = next((a for a in agents if a.get('id') == agent_id), None)
    if not ag:
        return {'ok': False, 'error': f'Agent {agent_id} 不存在'}
    sk = next((s for s in ag.get('skills', []) if s.get('name') == skill_name), None)
    if not sk:
        return {'ok': False, 'error': f'技能 {skill_name} 不存在'}
    skill_path = pathlib.Path(sk.get('path', '')).resolve()
    # 路径遍历保护：确保路径在 OCLAW_HOME 或项目目录下
    allowed_roots = (OCLAW_HOME.resolve(), BASE.parent.resolve())
    if not any(str(skill_path).startswith(str(root)) for root in allowed_roots):
        return {'ok': False, 'error': '路径不在允许的目录范围内'}
    if not skill_path.exists():
        return {'ok': True, 'name': skill_name, 'agent': agent_id, 'content': '(SKILL.md 文件不存在)', 'path': str(skill_path)}
    try:
        content = skill_path.read_text()
        return {'ok': True, 'name': skill_name, 'agent': agent_id, 'content': content, 'path': str(skill_path)}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def add_skill_to_agent(agent_id, skill_name, description, trigger=''):
    """Create a new skill for an agent with a standardised SKILL.md template."""
    if not _SAFE_NAME_RE.match(skill_name):
        return {'ok': False, 'error': f'skill_name 含非法字符: {skill_name}'}
    if not _SAFE_NAME_RE.match(agent_id):
        return {'ok': False, 'error': f'agentId 含非法字符: {agent_id}'}
    workspace = OCLAW_HOME / f'workspace-{agent_id}' / 'skills' / skill_name
    workspace.mkdir(parents=True, exist_ok=True)
    skill_md = workspace / 'SKILL.md'
    desc_line = description or skill_name
    trigger_section = f'\n## 触发条件\n{trigger}\n' if trigger else ''
    template = (f'---\n'
                f'name: {skill_name}\n'
                f'description: {desc_line}\n'
                f'---\n\n'
                f'# {skill_name}\n\n'
                f'{desc_line}\n'
                f'{trigger_section}\n'
                f'## 输入\n\n'
                f'<!-- 说明此技能接收什么输入 -->\n\n'
                f'## 处理流程\n\n'
                f'1. 步骤一\n'
                f'2. 步骤二\n\n'
                f'## 输出规范\n\n'
                f'<!-- 说明产出物格式与交付要求 -->\n\n'
                f'## 注意事项\n\n'
                f'- (在此补充约束、限制或特殊规则)\n')
    skill_md.write_text(template)
    # Re-sync agent config
    try:
        subprocess.run(['python3', str(SCRIPTS / 'sync_agent_config.py')], timeout=10)
    except Exception:
        pass
    return {'ok': True, 'message': f'技能 {skill_name} 已添加到 {agent_id}', 'path': str(skill_md)}


def add_remote_skill(agent_id, skill_name, source_url, description=''):
    """从远程 URL 或本地路径为 Agent 添加 skill SKILL.md 文件。
    
    支持的源：
    - HTTPS URLs: https://raw.githubusercontent.com/...
    - 本地路径: /path/to/SKILL.md 或 file:///path/to/SKILL.md
    """
    # 输入校验
    if not _SAFE_NAME_RE.match(agent_id):
        return {'ok': False, 'error': f'agentId 含非法字符: {agent_id}'}
    if not _SAFE_NAME_RE.match(skill_name):
        return {'ok': False, 'error': f'skillName 含非法字符: {skill_name}'}
    if not source_url or not isinstance(source_url, str):
        return {'ok': False, 'error': 'sourceUrl 必须是有效的字符串'}
    
    source_url = source_url.strip()
    
    # 检查 Agent 是否存在
    cfg = read_json(DATA / 'agent_config.json', {})
    agents = cfg.get('agents', [])
    if not any(a.get('id') == agent_id for a in agents):
        return {'ok': False, 'error': f'Agent {agent_id} 不存在'}
    
    # 下载或读取文件内容
    try:
        if source_url.startswith('http://') or source_url.startswith('https://'):
            # HTTPS URL 校验
            if not validate_url(source_url, allowed_schemes=('https',)):
                return {'ok': False, 'error': 'URL 无效或不安全（仅支持 HTTPS）'}
            
            # 从 URL 下载，带超时保护
            req = Request(source_url, headers={'User-Agent': 'OpenClaw-SkillManager/1.0'})
            try:
                resp = urlopen(req, timeout=10)
                content = resp.read(10 * 1024 * 1024).decode('utf-8')  # 最多 10MB
                if len(content) > 10 * 1024 * 1024:
                    return {'ok': False, 'error': '文件过大（最大 10MB）'}
            except Exception as e:
                return {'ok': False, 'error': f'URL 无法访问: {str(e)[:100]}'}
        
        elif source_url.startswith('file://'):
            # file:// URL 格式
            local_path = pathlib.Path(source_url[7:])
            if not local_path.exists():
                return {'ok': False, 'error': f'本地文件不存在: {local_path}'}
            content = local_path.read_text()
        
        elif source_url.startswith('/') or source_url.startswith('.'):
            # 本地绝对或相对路径
            local_path = pathlib.Path(source_url).resolve()
            if not local_path.exists():
                return {'ok': False, 'error': f'本地文件不存在: {local_path}'}
            # 路径遍历防护
            allowed_roots = (OCLAW_HOME.resolve(), BASE.parent.resolve())
            if not any(str(local_path).startswith(str(root)) for root in allowed_roots):
                return {'ok': False, 'error': '路径不在允许的目录范围内'}
            content = local_path.read_text()
        
        else:
            return {'ok': False, 'error': '不支持的 URL 格式（仅支持 https://, file://, 或本地路径）'}
    except Exception as e:
        return {'ok': False, 'error': f'文件读取失败: {str(e)[:100]}'}
    
    # 基础验证：检查是否为 Markdown 且包含 YAML frontmatter
    if not content.startswith('---'):
        return {'ok': False, 'error': '文件格式无效（缺少 YAML frontmatter）'}
    
    # 验证 frontmatter 结构（先做字符串检查，再尝试 YAML 解析）
    parts = content.split('---', 2)
    if len(parts) < 3:
        return {'ok': False, 'error': '文件格式无效（YAML frontmatter 结构错误）'}
    if 'name:' not in content[:500]:
        return {'ok': False, 'error': '文件格式无效：frontmatter 缺少 name 字段'}
    try:
        import yaml
        yaml.safe_load(parts[1])  # 严格校验 YAML 语法
    except ImportError:
        pass  # PyYAML 未安装，跳过严格验证，字符串检查已通过
    except Exception as e:
        return {'ok': False, 'error': f'YAML 格式无效: {str(e)[:100]}'}
    
    # 创建本地目录
    workspace = OCLAW_HOME / f'workspace-{agent_id}' / 'skills' / skill_name
    workspace.mkdir(parents=True, exist_ok=True)
    skill_md = workspace / 'SKILL.md'
    
    # 写入 SKILL.md
    skill_md.write_text(content)
    
    # 保存源信息到 .source.json
    source_info = {
        'skillName': skill_name,
        'sourceUrl': source_url,
        'description': description,
        'addedAt': now_iso(),
        'lastUpdated': now_iso(),
        'checksum': _compute_checksum(content),
        'status': 'valid',
    }
    source_json = workspace / '.source.json'
    source_json.write_text(json.dumps(source_info, ensure_ascii=False, indent=2))
    
    # Re-sync agent config
    try:
        subprocess.run(['python3', str(SCRIPTS / 'sync_agent_config.py')], timeout=10)
    except Exception:
        pass
    
    return {
        'ok': True,
        'message': f'技能 {skill_name} 已从远程源添加到 {agent_id}',
        'skillName': skill_name,
        'agentId': agent_id,
        'source': source_url,
        'localPath': str(skill_md),
        'size': len(content),
        'addedAt': now_iso(),
    }


def get_remote_skills_list():
    """列表所有已添加的远程 skills 及其源信息"""
    remote_skills = []
    
    # 遍历所有 workspace
    for ws_dir in OCLAW_HOME.glob('workspace-*'):
        agent_id = ws_dir.name.replace('workspace-', '')
        skills_dir = ws_dir / 'skills'
        if not skills_dir.exists():
            continue
        
        for skill_dir in skills_dir.iterdir():
            if not skill_dir.is_dir():
                continue
            skill_name = skill_dir.name
            source_json = skill_dir / '.source.json'
            skill_md = skill_dir / 'SKILL.md'
            
            if not source_json.exists():
                # 本地创建的 skill，跳过
                continue
            
            try:
                source_info = json.loads(source_json.read_text())
                # 检查 SKILL.md 是否存在
                status = 'valid' if skill_md.exists() else 'not-found'
                remote_skills.append({
                    'skillName': skill_name,
                    'agentId': agent_id,
                    'sourceUrl': source_info.get('sourceUrl', ''),
                    'description': source_info.get('description', ''),
                    'localPath': str(skill_md),
                    'addedAt': source_info.get('addedAt', ''),
                    'lastUpdated': source_info.get('lastUpdated', ''),
                    'status': status,
                })
            except Exception:
                pass
    
    return {
        'ok': True,
        'remoteSkills': remote_skills,
        'count': len(remote_skills),
        'listedAt': now_iso(),
    }


def update_remote_skill(agent_id, skill_name):
    """更新已添加的远程 skill 为最新版本（重新从源 URL 下载）"""
    if not _SAFE_NAME_RE.match(agent_id):
        return {'ok': False, 'error': f'agentId 含非法字符: {agent_id}'}
    if not _SAFE_NAME_RE.match(skill_name):
        return {'ok': False, 'error': f'skillName 含非法字符: {skill_name}'}
    
    workspace = OCLAW_HOME / f'workspace-{agent_id}' / 'skills' / skill_name
    source_json = workspace / '.source.json'
    skill_md = workspace / 'SKILL.md'
    
    if not source_json.exists():
        return {'ok': False, 'error': f'技能 {skill_name} 不是远程 skill（无 .source.json）'}
    
    try:
        source_info = json.loads(source_json.read_text())
        source_url = source_info.get('sourceUrl', '')
        if not source_url:
            return {'ok': False, 'error': '源 URL 不存在'}
        
        # 重新下载
        result = add_remote_skill(agent_id, skill_name, source_url, 
                                  source_info.get('description', ''))
        if result['ok']:
            result['message'] = f'技能已更新'
            source_info_updated = json.loads(source_json.read_text())
            result['newVersion'] = source_info_updated.get('checksum', 'unknown')
        return result
    except Exception as e:
        return {'ok': False, 'error': f'更新失败: {str(e)[:100]}'}


def remove_remote_skill(agent_id, skill_name):
    """移除已添加的远程 skill"""
    if not _SAFE_NAME_RE.match(agent_id):
        return {'ok': False, 'error': f'agentId 含非法字符: {agent_id}'}
    if not _SAFE_NAME_RE.match(skill_name):
        return {'ok': False, 'error': f'skillName 含非法字符: {skill_name}'}
    
    workspace = OCLAW_HOME / f'workspace-{agent_id}' / 'skills' / skill_name
    if not workspace.exists():
        return {'ok': False, 'error': f'技能不存在: {skill_name}'}
    
    # 检查是否为远程 skill
    source_json = workspace / '.source.json'
    if not source_json.exists():
        return {'ok': False, 'error': f'技能 {skill_name} 不是远程 skill，无法通过此 API 移除'}
    
    try:
        # 删除整个 skill 目录
        import shutil
        shutil.rmtree(workspace)
        
        # Re-sync agent config
        try:
            subprocess.run(['python3', str(SCRIPTS / 'sync_agent_config.py')], timeout=10)
        except Exception:
            pass
        
        return {'ok': True, 'message': f'技能 {skill_name} 已从 {agent_id} 移除'}
    except Exception as e:
        return {'ok': False, 'error': f'移除失败: {str(e)[:100]}'}


def _compute_checksum(content: str) -> str:
    """计算内容的简单校验和（SHA256 的前16字符）"""
    import hashlib
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def push_to_feishu():
    """Push morning brief link to Feishu via webhook."""
    cfg = read_json(DATA / 'morning_brief_config.json', {})
    webhook = cfg.get('feishu_webhook', '').strip()
    if not webhook:
        return
    if not validate_url(webhook, allowed_schemes=('https',), allowed_domains=('open.feishu.cn', 'open.larksuite.com')):
        log.warning(f'飞书 Webhook URL 不合法: {webhook}')
        return
    brief = read_json(DATA / 'morning_brief.json', {})
    date_str = brief.get('date', '')
    total = sum(len(v) for v in (brief.get('categories') or {}).values())
    if not total:
        return
    cat_lines = []
    for cat, items in (brief.get('categories') or {}).items():
        if items:
            cat_lines.append(f'  {cat}: {len(items)} 条')
    summary = '\n'.join(cat_lines)
    date_fmt = date_str[:4] + '年' + date_str[4:6] + '月' + date_str[6:] + '日' if len(date_str) == 8 else date_str
    payload = json.dumps({
        'msg_type': 'interactive',
        'card': {
            'header': {'title': {'tag': 'plain_text', 'content': f'📰 天下要闻 · {date_fmt}'}, 'template': 'blue'},
            'elements': [
                {'tag': 'div', 'text': {'tag': 'lark_md', 'content': f'共 **{total}** 条要闻已更新\n{summary}'}},
                {'tag': 'action', 'actions': [{'tag': 'button', 'text': {'tag': 'plain_text', 'content': '🔗 查看完整简报'}, 'url': 'http://127.0.0.1:7891', 'type': 'primary'}]},
                {'tag': 'note', 'elements': [{'tag': 'plain_text', 'content': f"采集于 {brief.get('generated_at', '')}"}]}
            ]
        }
    }).encode()
    try:
        req = Request(webhook, data=payload, headers={'Content-Type': 'application/json'})
        resp = urlopen(req, timeout=10)
        print(f'[飞书] 推送成功 ({resp.status})')
    except Exception as e:
        print(f'[飞书] 推送失败: {e}', file=sys.stderr)


# 旨意标题最低要求
_MIN_TITLE_LEN = 6
_JUNK_TITLES = {
    '?', '？', '好', '好的', '是', '否', '不', '不是', '对', '了解', '收到',
    '嗯', '哦', '知道了', '开启了么', '可以', '不行', '行', 'ok', 'yes', 'no',
    '你去开启', '测试', '试试', '看看',
}


def handle_create_task(title, org='中书省', official='中书令', priority='normal', template_id='', params=None, target_dept='', actor=None):
    """从看板创建新任务（圣旨模板下旨）。"""
    actor = actor or make_actor_context('emperor', source='dashboard')
    if not title or not title.strip():
        return {'ok': False, 'error': '任务标题不能为空'}
    title = title.strip()
    # 剥离 Conversation info 元数据
    title = re.split(r'\n*Conversation info\s*\(', title, maxsplit=1)[0].strip()
    title = re.split(r'\n*```', title, maxsplit=1)[0].strip()
    # 清理常见前缀: "传旨:" "下旨:" 等
    title = re.sub(r'^(传旨|下旨)[：:\uff1a]\s*', '', title)
    if len(title) > 100:
        title = title[:100] + '…'
    # 标题质量校验：防止闲聊被误建为旨意
    if len(title) < _MIN_TITLE_LEN:
        return {'ok': False, 'error': f'标题过短（{len(title)}<{_MIN_TITLE_LEN}字），不像是旨意'}
    if title.lower() in _JUNK_TITLES:
        return {'ok': False, 'error': f'「{title}」不是有效旨意，请输入具体工作指令'}
    result_box = {}

    def modifier(tasks):
        today = datetime.datetime.now().strftime('%Y%m%d')
        today_ids = [t.get('id', '') for t in tasks if isinstance(t, dict) and t.get('id', '').startswith(f'JJC-{today}-')]
        seq = 1
        if today_ids:
            nums = [int(tid.split('-')[-1]) for tid in today_ids if tid.split('-')[-1].isdigit()]
            seq = max(nums) + 1 if nums else 1
        task_id = f'JJC-{today}-{seq:03d}'
        initial_org = '司礼监'
        new_task = {
            'id': task_id,
            'title': title,
            'official': official,
            'org': initial_org,
            'state': 'Sili',
            'now': '等待司礼监接旨分办',
            'eta': '-',
            'block': '无',
            'output': '',
            'ac': '',
            'priority': priority,
            'templateId': template_id,
            'templateParams': params or {},
            'flow_log': [{
                'at': now_iso(),
                'from': '皇上',
                'to': initial_org,
                'remark': f'下旨：{title}'
            }],
            'updatedAt': now_iso(),
        }
        if target_dept:
            new_task['targetDept'] = target_dept
        ensure_task_shape(new_task)
        new_task['_stateVersion'] = max(1, int(new_task.get('_stateVersion') or 1))
        _ensure_scheduler(new_task)
        _scheduler_snapshot(new_task, 'create-task-initial')
        _scheduler_mark_progress(new_task, '任务创建')
        tasks.insert(0, new_task)
        result_box['task'] = json.loads(json.dumps(new_task, ensure_ascii=False))
        result_box['taskId'] = task_id
        return tasks

    _atomic_update_tasks(modifier)
    task_id = result_box.get('taskId', '')
    if not task_id:
        _record_task_audit('', 'task.create', actor, False, deny_reason='create failed', payload={'title': title})
        return {'ok': False, 'error': '创建任务失败'}

    _record_task_audit(task_id, 'task.create', actor, True, to_state='Sili', payload={'title': title, 'targetDept': target_dept})
    log.info(f'创建任务: {task_id} | {title[:40]}')
    dispatch_for_state(task_id, result_box['task'], 'Sili', trigger='imperial-edict', actor=actor)
    return {'ok': True, 'taskId': task_id, 'message': f'旨意 {task_id} 已下达，正在派发给司礼监'}


def handle_review_action(task_id, action, comment='', actor=None):
    """门下省御批：准奏/封驳。"""
    actor = actor or make_actor_context('emperor', source='dashboard')
    result = _with_task(task_id, lambda task, _tasks: _handle_review_action_update(task, action, comment, actor))
    if result is None:
        _record_task_audit(task_id, f'review.{action}', actor, False, deny_reason='task not found', payload={'comment': comment})
        return {'ok': False, 'error': f'任务 {task_id} 不存在'}
    if not result['allowed']:
        return {'ok': False, 'error': result['error']}
    if result.get('dispatch_state'):
        dispatch_for_state(task_id, result['task'], result['dispatch_state'], trigger=f'review-{action}', actor=actor)
    return {'ok': True, 'message': result['message']}


def _handle_review_action_update(task, action, comment, actor):
    ensure_task_shape(task)
    allowed, deny_reason = authorize_review(actor, task, action)
    if not allowed:
        _record_task_audit(task.get('id', ''), f'review.{action}', actor, False, from_state=task.get('state', ''), deny_reason=deny_reason, payload={'comment': comment})
        return {'allowed': False, 'error': deny_reason}

    _ensure_scheduler(task)
    _scheduler_snapshot(task, f'review-before-{action}')
    current_state = canonicalize_state(task.get('state', ''))
    ok, transition = review_transition(task, action, comment)
    if not ok:
        _record_task_audit(task.get('id', ''), f'review.{action}', actor, False, from_state=current_state, deny_reason=transition['reason'], payload={'comment': comment})
        return {'allowed': False, 'error': transition['reason']}

    new_state = transition['new_state']
    task['state'] = new_state
    task['org'] = transition['to_org']
    if transition.get('increment_review_round'):
        next_round = int(task.get('review_round') or 0) + 1
        task['review_round'] = next_round
        task['now'] = f'{transition["now"]}（第{next_round}轮）'
    else:
        task['now'] = transition['now']
    task.setdefault('flow_log', []).append({
        'at': now_iso(),
        'from': transition['from_org'],
        'to': transition['to_org'],
        'remark': transition['remark'],
    })
    _scheduler_mark_progress(task, f'审议动作 {action} -> {new_state}')
    _bump_task_version(task)
    task['updatedAt'] = now_iso()
    _record_task_audit(task.get('id', ''), f'review.{action}', actor, True, from_state=current_state, to_state=new_state, payload={'comment': comment})

    label = '已准奏' if action == 'approve' else '已驳回'
    dispatched = ' (已自动派发 Agent)' if new_state not in TERMINAL_STATES else ''
    return {
        'allowed': True,
        'message': f'{task.get("id", "")} {label}{dispatched}',
        'dispatch_state': new_state if new_state not in TERMINAL_STATES else '',
        'task': json.loads(json.dumps(task, ensure_ascii=False)),
    }


# ══ Agent 在线状态检测 ══

_AGENT_DEPTS = [
    {'id':'sili',   'label':'司礼监',  'emoji':'🧾', 'role':'掌印秉笔',   'rank':'内廷'},
    {'id':'zhongshu','label':'中书省','emoji':'📜', 'role':'中书令',   'rank':'正一品'},
    {'id':'menxia',  'label':'门下省','emoji':'🔍', 'role':'侍中',     'rank':'正一品'},
    {'id':'shangshu','label':'尚书省','emoji':'📮', 'role':'尚书令',   'rank':'正一品'},
    {'id':'hubu',    'label':'户部',  'emoji':'💰', 'role':'户部尚书', 'rank':'正二品'},
    {'id':'libu',    'label':'礼部',  'emoji':'📝', 'role':'礼部尚书', 'rank':'正二品'},
    {'id':'bingbu',  'label':'兵部',  'emoji':'⚔️', 'role':'兵部尚书', 'rank':'正二品'},
    {'id':'xingbu',  'label':'刑部',  'emoji':'⚖️', 'role':'刑部尚书', 'rank':'正二品'},
    {'id':'gongbu',  'label':'工部',  'emoji':'🔧', 'role':'工部尚书', 'rank':'正二品'},
    {'id':'libu_hr', 'label':'吏部',  'emoji':'👔', 'role':'吏部尚书', 'rank':'正二品'},
    {'id':'zaochao', 'label':'钦天监','emoji':'📰', 'role':'朝报官',   'rank':'正三品'},
]


def _check_gateway_alive():
    """检测 Gateway 进程是否在运行。"""
    try:
        result = subprocess.run(['pgrep', '-f', 'openclaw-gateway'],
                                capture_output=True, text=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False


def _check_gateway_probe():
    """通过 HTTP probe 检测 Gateway 是否响应。"""
    try:
        from urllib.request import urlopen
        resp = urlopen('http://127.0.0.1:18789/', timeout=3)
        return resp.status == 200
    except Exception:
        return False


def _get_agent_session_status(agent_id):
    """读取 Agent 的 sessions.json 获取活跃状态。
    返回: (last_active_ts_ms, session_count, is_busy)
    """
    sessions_file = OCLAW_HOME / 'agents' / agent_id / 'sessions' / 'sessions.json'
    if not sessions_file.exists():
        return 0, 0, False
    try:
        data = json.loads(sessions_file.read_text())
        if not isinstance(data, dict):
            return 0, 0, False
        session_count = len(data)
        last_ts = 0
        for v in data.values():
            ts = v.get('updatedAt', 0)
            if isinstance(ts, (int, float)) and ts > last_ts:
                last_ts = ts
        now_ms = int(datetime.datetime.now().timestamp() * 1000)
        age_ms = now_ms - last_ts if last_ts else 9999999999
        is_busy = age_ms <= 2 * 60 * 1000  # 2分钟内视为正在工作
        return last_ts, session_count, is_busy
    except Exception:
        return 0, 0, False


def _check_agent_process(agent_id):
    """检测是否有该 Agent 的 openclaw-agent 进程正在运行。"""
    try:
        result = subprocess.run(
            ['pgrep', '-f', f'openclaw.*--agent.*{agent_id}'],
            capture_output=True, text=True, timeout=5
        )
        return result.returncode == 0
    except Exception:
        return False


def _check_agent_workspace(agent_id):
    """检查 Agent 工作空间是否存在。"""
    ws = OCLAW_HOME / f'workspace-{agent_id}'
    return ws.is_dir()


def get_agents_status():
    """获取所有 Agent 的在线状态。
    返回各 Agent 的:
    - status: 'running' | 'idle' | 'offline' | 'unconfigured'
    - lastActive: 最后活跃时间
    - sessions: 会话数
    - hasWorkspace: 工作空间是否存在
    - processAlive: 是否有进程在运行
    """
    gateway_alive = _check_gateway_alive()
    gateway_probe = _check_gateway_probe() if gateway_alive else False

    agents = []
    seen_ids = set()
    for dept in _AGENT_DEPTS:
        aid = dept['id']
        if aid in seen_ids:
            continue
        seen_ids.add(aid)

        has_workspace = _check_agent_workspace(aid)
        last_ts, sess_count, is_busy = _get_agent_session_status(aid)
        process_alive = _check_agent_process(aid)

        # 状态判定
        if not has_workspace:
            status = 'unconfigured'
            status_label = '❌ 未配置'
        elif not gateway_alive:
            status = 'offline'
            status_label = '🔴 Gateway 离线'
        elif process_alive or is_busy:
            status = 'running'
            status_label = '🟢 运行中'
        elif last_ts > 0:
            now_ms = int(datetime.datetime.now().timestamp() * 1000)
            age_ms = now_ms - last_ts
            if age_ms <= 10 * 60 * 1000:  # 10分钟内
                status = 'idle'
                status_label = '🟡 待命'
            elif age_ms <= 3600 * 1000:  # 1小时内
                status = 'idle'
                status_label = '⚪ 空闲'
            else:
                status = 'idle'
                status_label = '⚪ 休眠'
        else:
            status = 'idle'
            status_label = '⚪ 无记录'

        # 格式化最后活跃时间
        last_active_str = None
        if last_ts > 0:
            try:
                last_active_str = datetime.datetime.fromtimestamp(
                    last_ts / 1000
                ).strftime('%m-%d %H:%M')
            except Exception:
                pass

        agents.append({
            'id': aid,
            'label': dept['label'],
            'emoji': dept['emoji'],
            'role': dept['role'],
            'status': status,
            'statusLabel': status_label,
            'lastActive': last_active_str,
            'lastActiveTs': last_ts,
            'sessions': sess_count,
            'hasWorkspace': has_workspace,
            'processAlive': process_alive,
        })

    return {
        'ok': True,
        'gateway': {
            'alive': gateway_alive,
            'probe': gateway_probe,
            'status': '🟢 运行中' if gateway_probe else ('🟡 进程在但无响应' if gateway_alive else '🔴 未启动'),
        },
        'agents': agents,
        'checkedAt': now_iso(),
    }


def wake_agent(agent_id, message='', actor=None, task_id=''):
    """唤醒指定 Agent，发送一条心跳/唤醒消息。"""
    actor = actor or make_actor_context('emperor', source='dashboard')
    if not _SAFE_NAME_RE.match(agent_id):
        _record_task_audit(task_id, 'agent.wake', actor, False, target_agent=agent_id, deny_reason=f'agent_id 非法: {agent_id}')
        return {'ok': False, 'error': f'agent_id 非法: {agent_id}'}
    allowed, deny_reason = authorize_agent_wake(actor, agent_id)
    if not allowed:
        _record_task_audit(task_id, 'agent.wake', actor, False, target_agent=agent_id, deny_reason=deny_reason)
        return {'ok': False, 'error': deny_reason}
    if not _check_agent_workspace(agent_id):
        _record_task_audit(task_id, 'agent.wake', actor, False, target_agent=agent_id, deny_reason=f'{agent_id} 工作空间不存在，请先配置')
        return {'ok': False, 'error': f'{agent_id} 工作空间不存在，请先配置'}
    if not _check_gateway_alive():
        _record_task_audit(task_id, 'agent.wake', actor, False, target_agent=agent_id, deny_reason='Gateway 未启动，请先运行 openclaw gateway start')
        return {'ok': False, 'error': 'Gateway 未启动，请先运行 openclaw gateway start'}

    # agent_id 直接作为 runtime_id（openclaw agents list 中的注册名）
    runtime_id = agent_id
    msg = message or f'🔔 系统心跳检测 — 请回复 OK 确认在线。当前时间: {now_iso()}'

    def do_wake():
        try:
            cmd = ['openclaw', 'agent', '--agent', runtime_id, '-m', msg, '--timeout', '120']
            log.info(f'🔔 唤醒 {agent_id}...')
            # 带重试（最多2次）
            for attempt in range(1, 3):
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=130)
                if result.returncode == 0:
                    log.info(f'✅ {agent_id} 已唤醒')
                    return
                err_msg = result.stderr[:200] if result.stderr else result.stdout[:200]
                log.warning(f'⚠️ {agent_id} 唤醒失败(第{attempt}次): {err_msg}')
                if attempt < 2:
                    import time
                    time.sleep(5)
            log.error(f'❌ {agent_id} 唤醒最终失败')
        except subprocess.TimeoutExpired:
            log.error(f'❌ {agent_id} 唤醒超时(130s)')
        except Exception as e:
            log.warning(f'⚠️ {agent_id} 唤醒异常: {e}')
    threading.Thread(target=do_wake, daemon=True).start()
    _record_task_audit(task_id, 'agent.wake', actor, True, target_agent=agent_id, payload={'message': msg[:200]})
    return {'ok': True, 'message': f'{agent_id} 唤醒指令已发出，约10-30秒后生效'}


# ══ Agent 实时活动读取 ══

# 状态 → agent_id 映射
_STATE_AGENT_MAP = {
    'Sili': 'sili',
    'Zhongshu': 'zhongshu',
    'Menxia': 'menxia',
    'Assigned': 'shangshu',
    'Doing': None,         # 六部，需从 org 推断
    'Review': 'shangshu',
    'Next': None,          # 待执行，从 org 推断
    'Pending': 'sili',
}
_ORG_AGENT_MAP = {
    '礼部': 'libu', '户部': 'hubu', '兵部': 'bingbu',
    '刑部': 'xingbu', '工部': 'gongbu', '吏部': 'libu_hr',
    '中书省': 'zhongshu', '门下省': 'menxia', '尚书省': 'shangshu',
}

_TERMINAL_STATES = set(TERMINAL_STATES)


def _parse_iso(ts):
    if not ts or not isinstance(ts, str):
        return None
    try:
        return datetime.datetime.fromisoformat(ts.replace('Z', '+00:00'))
    except Exception:
        return None


def _ensure_scheduler(task):
    sched = task.setdefault('_scheduler', {})
    if not isinstance(sched, dict):
        sched = {}
        task['_scheduler'] = sched
    sched.setdefault('enabled', True)
    sched.setdefault('stallThresholdSec', 600)
    sched.setdefault('maxRetry', 2)
    sched.setdefault('retryCount', 0)
    sched.setdefault('escalationLevel', 0)
    sched.setdefault('autoRollback', True)
    if not sched.get('lastProgressAt'):
        sched['lastProgressAt'] = task.get('updatedAt') or now_iso()
    if 'stallSince' not in sched:
        sched['stallSince'] = None
    if 'lastDispatchStatus' not in sched:
        sched['lastDispatchStatus'] = 'idle'
    if 'snapshot' not in sched:
        sched['snapshot'] = {
            'state': task.get('state', ''),
            'org': task.get('org', ''),
            'now': task.get('now', ''),
            'savedAt': now_iso(),
            'note': 'init',
        }
    return sched


def _scheduler_add_flow(task, remark, to=''):
    task.setdefault('flow_log', []).append({
        'at': now_iso(),
        'from': '司礼监调度',
        'to': to or task.get('org', ''),
        'remark': f'🧭 {remark}'
    })


def _scheduler_snapshot(task, note=''):
    sched = _ensure_scheduler(task)
    sched['snapshot'] = {
        'state': task.get('state', ''),
        'org': task.get('org', ''),
        'now': task.get('now', ''),
        'savedAt': now_iso(),
        'note': note or 'snapshot',
    }


def _scheduler_mark_progress(task, note=''):
    sched = _ensure_scheduler(task)
    sched['lastProgressAt'] = now_iso()
    sched['stallSince'] = None
    sched['retryCount'] = 0
    sched['escalationLevel'] = 0
    sched['lastEscalatedAt'] = None
    if note:
        _scheduler_add_flow(task, f'进展确认：{note}')


def _update_task_scheduler(task_id, updater):
    result = _with_task(
        task_id,
        lambda task, _tasks: _update_task_scheduler_inner(task, updater),
    )
    return result is not None


def _update_task_scheduler_inner(task, updater):
    ensure_task_shape(task)
    sched = _ensure_scheduler(task)
    updater(task, sched)
    task['updatedAt'] = now_iso()
    return True


def get_scheduler_state(task_id):
    tasks = load_tasks()
    task = next((t for t in tasks if t.get('id') == task_id), None)
    if not task:
        return {'ok': False, 'error': f'任务 {task_id} 不存在'}
    sched = _ensure_scheduler(task)
    last_progress = _parse_iso(sched.get('lastProgressAt') or task.get('updatedAt'))
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    stalled_sec = 0
    if last_progress:
        stalled_sec = max(0, int((now_dt - last_progress).total_seconds()))
    return {
        'ok': True,
        'taskId': task_id,
        'state': task.get('state', ''),
        'org': task.get('org', ''),
        'scheduler': sched,
        'stalledSec': stalled_sec,
        'checkedAt': now_iso(),
    }


def handle_scheduler_retry(task_id, reason='', actor=None):
    actor = actor or make_actor_context('sili', source='scheduler')
    allowed, deny_reason = authorize_scheduler(actor, 'retry')
    if not allowed:
        _record_task_audit(task_id, 'scheduler.retry', actor, False, deny_reason=deny_reason, payload={'reason': reason})
        return {'ok': False, 'error': deny_reason}
    result = _with_task(task_id, lambda task, _tasks: _handle_scheduler_retry_update(task, reason, actor))
    if result is None:
        _record_task_audit(task_id, 'scheduler.retry', actor, False, deny_reason='task not found', payload={'reason': reason})
        return {'ok': False, 'error': f'任务 {task_id} 不存在'}
    if not result['allowed']:
        return {'ok': False, 'error': result['error']}
    dispatch_for_state(task_id, result['task'], result['state'], trigger='sili-retry', actor=actor)
    return {'ok': True, 'message': result['message'], 'retryCount': result['retryCount']}


def _handle_scheduler_retry_update(task, reason, actor):
    ensure_task_shape(task)
    state = canonicalize_state(task.get('state', ''))
    if state in _TERMINAL_STATES or state == 'Blocked':
        deny_reason = f'任务 {task.get("id", "")} 当前状态 {state} 不支持重试'
        _record_task_audit(task.get('id', ''), 'scheduler.retry', actor, False, from_state=state, deny_reason=deny_reason, payload={'reason': reason})
        return {'allowed': False, 'error': deny_reason}
    sched = _ensure_scheduler(task)
    sched['retryCount'] = int(sched.get('retryCount') or 0) + 1
    sched['lastRetryAt'] = now_iso()
    sched['lastDispatchTrigger'] = 'sili-retry'
    _scheduler_add_flow(task, f'触发重试第{sched["retryCount"]}次：{reason or "超时未推进"}')
    task['updatedAt'] = now_iso()
    _record_task_audit(task.get('id', ''), 'scheduler.retry', actor, True, from_state=state, to_state=state, payload={'reason': reason, 'retryCount': sched['retryCount']})
    return {
        'allowed': True,
        'message': f'{task.get("id", "")} 已触发重试派发',
        'retryCount': sched['retryCount'],
        'state': state,
        'task': json.loads(json.dumps(task, ensure_ascii=False)),
    }


def handle_scheduler_escalate(task_id, reason='', actor=None):
    actor = actor or make_actor_context('sili', source='scheduler')
    allowed, deny_reason = authorize_scheduler(actor, 'escalate')
    if not allowed:
        _record_task_audit(task_id, 'scheduler.escalate', actor, False, deny_reason=deny_reason, payload={'reason': reason})
        return {'ok': False, 'error': deny_reason}
    result = _with_task(task_id, lambda task, _tasks: _handle_scheduler_escalate_update(task, reason, actor))
    if result is None:
        _record_task_audit(task_id, 'scheduler.escalate', actor, False, deny_reason='task not found', payload={'reason': reason})
        return {'ok': False, 'error': f'任务 {task_id} 不存在'}
    if not result['allowed']:
        return {'ok': False, 'error': result['error']}
    wake_agent(result['target'], result['messageText'], actor=actor, task_id=task_id)
    return {'ok': True, 'message': result['message'], 'escalationLevel': result['escalationLevel']}


def _handle_scheduler_escalate_update(task, reason, actor):
    ensure_task_shape(task)
    state = canonicalize_state(task.get('state', ''))
    if state in _TERMINAL_STATES:
        deny_reason = f'任务 {task.get("id", "")} 已结束，无需升级'
        _record_task_audit(task.get('id', ''), 'scheduler.escalate', actor, False, from_state=state, deny_reason=deny_reason, payload={'reason': reason})
        return {'allowed': False, 'error': deny_reason}
    sched = _ensure_scheduler(task)
    current_level = int(sched.get('escalationLevel') or 0)
    next_level = min(current_level + 1, 2)
    target = 'menxia' if next_level == 1 else 'shangshu'
    target_label = '门下省' if next_level == 1 else '尚书省'
    sched['escalationLevel'] = next_level
    sched['lastEscalatedAt'] = now_iso()
    _scheduler_add_flow(task, f'升级到{target_label}协调：{reason or "任务停滞"}', to=target_label)
    task['updatedAt'] = now_iso()
    _record_task_audit(task.get('id', ''), 'scheduler.escalate', actor, True, from_state=state, to_state=state, target_agent=target, payload={'reason': reason, 'level': next_level})
    msg = (
        f'🧭 司礼监调度升级通知\n'
        f'任务ID: {task.get("id", "")}\n'
        f'当前状态: {state}\n'
        f'停滞处理: 请你介入协调推进\n'
        f'原因: {reason or "任务超过阈值未推进"}\n'
        f'⚠️ 看板已有任务，请勿重复创建。'
    )
    return {
        'allowed': True,
        'message': f'{task.get("id", "")} 已升级至{target_label}',
        'escalationLevel': next_level,
        'target': target,
        'messageText': msg,
    }


def handle_scheduler_rollback(task_id, reason='', actor=None):
    actor = actor or make_actor_context('sili', source='scheduler')
    allowed, deny_reason = authorize_scheduler(actor, 'rollback')
    if not allowed:
        _record_task_audit(task_id, 'scheduler.rollback', actor, False, deny_reason=deny_reason, payload={'reason': reason})
        return {'ok': False, 'error': deny_reason}
    result = _with_task(task_id, lambda task, _tasks: _handle_scheduler_rollback_update(task, reason, actor))
    if result is None:
        _record_task_audit(task_id, 'scheduler.rollback', actor, False, deny_reason='task not found', payload={'reason': reason})
        return {'ok': False, 'error': f'任务 {task_id} 不存在'}
    if not result['allowed']:
        return {'ok': False, 'error': result['error']}
    if result.get('dispatch_state'):
        dispatch_for_state(task_id, result['task'], result['dispatch_state'], trigger='sili-rollback', actor=actor)
    return {'ok': True, 'message': result['message']}


def _handle_scheduler_rollback_update(task, reason, actor):
    ensure_task_shape(task)
    sched = _ensure_scheduler(task)
    snapshot = sched.get('snapshot') or {}
    snap_state = canonicalize_state(snapshot.get('state'))
    if not snap_state:
        deny_reason = f'任务 {task.get("id", "")} 无可用回滚快照'
        _record_task_audit(task.get('id', ''), 'scheduler.rollback', actor, False, from_state=task.get('state', ''), deny_reason=deny_reason, payload={'reason': reason})
        return {'allowed': False, 'error': deny_reason}
    old_state = canonicalize_state(task.get('state', ''))
    task['state'] = snap_state
    task['org'] = snapshot.get('org', task.get('org', ''))
    task['now'] = f'↩️ 司礼监调度自动回滚：{reason or "恢复到上个稳定节点"}'
    task['block'] = '无'
    sched['retryCount'] = 0
    sched['escalationLevel'] = 0
    sched['stallSince'] = None
    sched['lastProgressAt'] = now_iso()
    _scheduler_add_flow(task, f'执行回滚：{old_state} → {snap_state}，原因：{reason or "停滞恢复"}')
    _bump_task_version(task)
    task['updatedAt'] = now_iso()
    _record_task_audit(task.get('id', ''), 'scheduler.rollback', actor, True, from_state=old_state, to_state=snap_state, payload={'reason': reason})
    return {
        'allowed': True,
        'message': f'{task.get("id", "")} 已回滚到 {snap_state}',
        'dispatch_state': snap_state if snap_state not in _TERMINAL_STATES else '',
        'task': json.loads(json.dumps(task, ensure_ascii=False)),
    }


def handle_scheduler_scan(threshold_sec=600, actor=None):
    actor = actor or make_actor_context('sili', source='scheduler')
    allowed, deny_reason = authorize_scheduler(actor, 'scan')
    if not allowed:
        _record_task_audit('', 'scheduler.scan', actor, False, deny_reason=deny_reason, payload={'thresholdSec': threshold_sec})
        return {'ok': False, 'error': deny_reason}

    threshold_sec = max(60, int(threshold_sec or 600))
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    pending_retries = []
    pending_escalates = []
    pending_rollbacks = []
    actions = []

    def modifier(tasks):
        for task in tasks:
            if not isinstance(task, dict):
                continue
            ensure_task_shape(task)
            task_id = task.get('id', '')
            state = canonicalize_state(task.get('state', ''))
            if not task_id or state in _TERMINAL_STATES or task.get('archived') or state == 'Blocked':
                continue

            sched = _ensure_scheduler(task)
            task_threshold = int(sched.get('stallThresholdSec') or threshold_sec)
            last_progress = _parse_iso(sched.get('lastProgressAt') or task.get('updatedAt'))
            if not last_progress:
                continue
            stalled_sec = max(0, int((now_dt - last_progress).total_seconds()))
            if stalled_sec < task_threshold:
                continue

            if not sched.get('stallSince'):
                sched['stallSince'] = now_iso()

            retry_count = int(sched.get('retryCount') or 0)
            max_retry = max(0, int(sched.get('maxRetry') or 1))
            level = int(sched.get('escalationLevel') or 0)

            if retry_count < max_retry:
                sched['retryCount'] = retry_count + 1
                sched['lastRetryAt'] = now_iso()
                sched['lastDispatchTrigger'] = 'sili-scan-retry'
                _scheduler_add_flow(task, f'停滞{stalled_sec}秒，触发自动重试第{sched["retryCount"]}次')
                task['updatedAt'] = now_iso()
                pending_retries.append({'taskId': task_id, 'state': state, 'task': json.loads(json.dumps(task, ensure_ascii=False))})
                actions.append({'taskId': task_id, 'action': 'retry', 'stalledSec': stalled_sec})
                _record_task_audit(task_id, 'scheduler.scan.retry', actor, True, from_state=state, to_state=state, payload={'stalledSec': stalled_sec, 'retryCount': sched['retryCount']})
                continue

            if level < 2:
                next_level = level + 1
                target = 'menxia' if next_level == 1 else 'shangshu'
                target_label = '门下省' if next_level == 1 else '尚书省'
                sched['escalationLevel'] = next_level
                sched['lastEscalatedAt'] = now_iso()
                _scheduler_add_flow(task, f'停滞{stalled_sec}秒，升级至{target_label}协调', to=target_label)
                task['updatedAt'] = now_iso()
                pending_escalates.append({
                    'taskId': task_id,
                    'state': state,
                    'target': target,
                    'targetLabel': target_label,
                    'stalledSec': stalled_sec,
                })
                actions.append({'taskId': task_id, 'action': 'escalate', 'to': target_label, 'stalledSec': stalled_sec})
                _record_task_audit(task_id, 'scheduler.scan.escalate', actor, True, from_state=state, to_state=state, target_agent=target, payload={'stalledSec': stalled_sec, 'level': next_level})
                continue

            if sched.get('autoRollback', True):
                snapshot = sched.get('snapshot') or {}
                snap_state = canonicalize_state(snapshot.get('state'))
                if snap_state and snap_state != state:
                    task['state'] = snap_state
                    task['org'] = snapshot.get('org', task.get('org', ''))
                    task['now'] = '↩️ 司礼监调度自动回滚到稳定节点'
                    task['block'] = '无'
                    sched['retryCount'] = 0
                    sched['escalationLevel'] = 0
                    sched['stallSince'] = None
                    sched['lastProgressAt'] = now_iso()
                    _scheduler_add_flow(task, f'连续停滞，自动回滚：{state} → {snap_state}')
                    _bump_task_version(task)
                    task['updatedAt'] = now_iso()
                    pending_rollbacks.append({'taskId': task_id, 'state': snap_state, 'task': json.loads(json.dumps(task, ensure_ascii=False))})
                    actions.append({'taskId': task_id, 'action': 'rollback', 'toState': snap_state})
                    _record_task_audit(task_id, 'scheduler.scan.rollback', actor, True, from_state=state, to_state=snap_state, payload={'stalledSec': stalled_sec})
        return tasks

    _atomic_update_tasks(modifier)

    for item in pending_retries:
        dispatch_for_state(item['taskId'], item['task'], item['state'], trigger='sili-scan-retry', actor=actor)

    for item in pending_escalates:
        msg = (
            f'🧭 司礼监调度升级通知\n'
            f'任务ID: {item["taskId"]}\n'
            f'当前状态: {item["state"]}\n'
            f'已停滞: {item["stalledSec"]} 秒\n'
            f'请立即介入协调推进\n'
            f'⚠️ 看板已有任务，请勿重复创建。'
        )
        wake_agent(item['target'], msg, actor=actor, task_id=item['taskId'])

    for item in pending_rollbacks:
        if item['state'] not in _TERMINAL_STATES:
            dispatch_for_state(item['taskId'], item['task'], item['state'], trigger='sili-auto-rollback', actor=actor)

    return {
        'ok': True,
        'thresholdSec': threshold_sec,
        'actions': actions,
        'count': len(actions),
        'checkedAt': now_iso(),
    }


def _startup_recover_queued_dispatches():
    """服务启动后扫描 lastDispatchStatus=queued 的任务，重新派发。
    解决：kill -9 重启导致派发线程中断、任务永久卡住的问题。"""
    tasks = load_tasks()
    recovered = 0
    for task in tasks:
        task_id = task.get('id', '')
        state = task.get('state', '')
        if not task_id or state in _TERMINAL_STATES or task.get('archived'):
            continue
        sched = task.get('_scheduler') or {}
        if sched.get('lastDispatchStatus') == 'queued':
            log.info(f'🔄 启动恢复: {task_id} 状态={state} 上次派发未完成，重新派发')
            sched['lastDispatchTrigger'] = 'startup-recovery'
            dispatch_for_state(task_id, task, state, trigger='startup-recovery')
            recovered += 1
    if recovered:
        log.info(f'✅ 启动恢复完成: 重新派发 {recovered} 个任务')
    else:
        log.info(f'✅ 启动恢复: 无需恢复')


def handle_repair_flow_order():
    """修复历史任务中首条流转为“皇上->中书省”的错序问题。"""
    fixed = 0
    fixed_ids = []

    def modifier(tasks):
        nonlocal fixed, fixed_ids
        for task in tasks:
            if not isinstance(task, dict):
                continue
            ensure_task_shape(task)
            task_id = task.get('id', '')
            if not task_id.startswith('JJC-'):
                continue
            flow_log = task.get('flow_log') or []
            if not flow_log:
                continue

            first = flow_log[0]
            if first.get('from') != '皇上' or first.get('to') != '中书省':
                continue

            first['to'] = '司礼监'
            remark = first.get('remark', '')
            if isinstance(remark, str) and remark.startswith('下旨：'):
                first['remark'] = remark

            if task.get('state') == 'Zhongshu' and task.get('org') == '中书省' and len(flow_log) == 1:
                task['state'] = 'Sili'
                task['org'] = '司礼监'
                task['now'] = '等待司礼监接旨分办'

            task['updatedAt'] = now_iso()
            fixed += 1
            fixed_ids.append(task_id)
        return tasks

    _atomic_update_tasks(modifier)

    return {
        'ok': True,
        'count': fixed,
        'taskIds': fixed_ids[:80],
        'more': max(0, fixed - 80),
        'checkedAt': now_iso(),
    }


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
        return entry

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
        return entry

    if role == 'user':
        text = ''
        for c in msg.get('content', []) or []:
            if c.get('type') == 'text' and c.get('text'):
                text = str(c.get('text', '')).strip()
                break
        if not text:
            return None
        return {'at': ts, 'kind': 'user', 'text': text[:200]}

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
    return result


# 状态推进顺序（手动推进用）
_STATE_FLOW = {
    'Pending':  ('Sili', '皇上', '司礼监', '待处理旨意转交司礼监分办'),
    'Sili':    ('Zhongshu', '司礼监', '中书省', '司礼监分办完毕，转中书省起草'),
    'Zhongshu': ('Menxia', '中书省', '门下省', '中书省方案提交门下省审议'),
    'Menxia':   ('Assigned', '门下省', '尚书省', '门下省准奏，转尚书省派发'),
    'Assigned': ('Doing', '尚书省', '六部', '尚书省开始派发执行'),
    'Next':     ('Doing', '尚书省', '六部', '待执行任务开始执行'),
    'Doing':    ('Review', '六部', '尚书省', '各部完成，进入汇总'),
    'Review':   ('Done', '尚书省', '司礼监', '全流程完成，回奏司礼监转报皇上'),
}
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
    """手动推进任务到下一阶段（解卡用），推进后自动派发对应 Agent。"""
    actor = actor or make_actor_context('emperor', source='dashboard')
    result = _with_task(task_id, lambda task, _tasks: _handle_advance_state_update(task, comment, actor))
    if result is None:
        _record_task_audit(task_id, 'task.advance', actor, False, deny_reason='task not found', payload={'comment': comment})
        return {'ok': False, 'error': f'任务 {task_id} 不存在'}
    if not result['allowed']:
        return {'ok': False, 'error': result['error']}
    if result.get('dispatch_state'):
        dispatch_for_state(task_id, result['task'], result['dispatch_state'], trigger='manual-advance', actor=actor)
    return {'ok': True, 'message': result['message']}


def _handle_advance_state_update(task, comment, actor):
    ensure_task_shape(task)
    current_state = canonicalize_state(task.get('state', ''))
    ok, transition = next_manual_transition(task)
    if not ok:
        _record_task_audit(task.get('id', ''), 'task.advance', actor, False, from_state=current_state, deny_reason=transition['reason'], payload={'comment': comment})
        return {'allowed': False, 'error': transition['reason']}
    next_state = transition['next_state']
    allowed, deny_reason = authorize_transition(actor, task, next_state)
    if not allowed:
        _record_task_audit(task.get('id', ''), 'task.advance', actor, False, from_state=current_state, to_state=next_state, deny_reason=deny_reason, payload={'comment': comment})
        return {'allowed': False, 'error': deny_reason}

    _ensure_scheduler(task)
    _scheduler_snapshot(task, f'advance-before-{current_state}')
    remark = comment or transition['remark']
    task['state'] = next_state
    task['org'] = transition['to_org']
    task['now'] = f'⬇️ 手动推进：{remark}'
    task.setdefault('flow_log', []).append({
        'at': now_iso(),
        'from': transition['from_org'],
        'to': transition['to_org'],
        'remark': f'⬇️ 手动推进：{remark}',
    })
    _scheduler_mark_progress(task, f'手动推进 {current_state} -> {next_state}')
    _bump_task_version(task)
    task['updatedAt'] = now_iso()
    _record_task_audit(task.get('id', ''), 'task.advance', actor, True, from_state=current_state, to_state=next_state, payload={'comment': comment})

    from_label = _STATE_LABELS.get(current_state, current_state)
    to_label = _STATE_LABELS.get(next_state, next_state)
    dispatched = ' (已自动派发 Agent)' if next_state not in TERMINAL_STATES else ''
    return {
        'allowed': True,
        'message': f'{task.get("id", "")} {from_label} → {to_label}{dispatched}',
        'dispatch_state': next_state if next_state not in TERMINAL_STATES else '',
        'task': json.loads(json.dumps(task, ensure_ascii=False)),
    }


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
            template_id = body.get('templateId', '')
            params = body.get('params', {})
            if not title:
                self.send_json({'ok': False, 'error': 'title required'}, 400)
                return
            target_dept = body.get('targetDept', '').strip()
            actor = _actor_from_request(self.headers, body)
            result = handle_create_task(title, org, official, priority, template_id, params, target_dept, actor=actor)
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
