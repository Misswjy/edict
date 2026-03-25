"""tests for dashboard/server.py route handling and task mutations"""

import importlib
import json
import pathlib
import sys
import threading
import time
from http.client import HTTPConnection

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'dashboard'))
sys.path.insert(0, str(ROOT / 'scripts'))

from file_lock import LegacyWriteFrozenError


def _configure_server(tmp_path, monkeypatch):
    import server as srv

    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    (data_dir / 'tasks_source.json').write_text('[]')
    (data_dir / 'live_status.json').write_text(json.dumps({'tasks': [], 'syncStatus': {'ok': True}}))
    (data_dir / 'agent_config.json').write_text(json.dumps({'agents': []}))

    monkeypatch.setattr(srv, 'DATA', data_dir, raising=False)
    monkeypatch.setattr(srv, 'TASKS_PATH', data_dir / 'tasks_source.json', raising=False)
    monkeypatch.setattr(srv, 'TASK_AUDIT_PATH', data_dir / 'task_audit_log.json', raising=False)
    monkeypatch.setattr(srv, '_trigger_refresh_async', lambda: None, raising=False)
    monkeypatch.setattr(srv, 'dispatch_for_state', lambda *args, **kwargs: None, raising=False)
    monkeypatch.setattr(srv, 'wake_agent', lambda *args, **kwargs: {'ok': True, 'message': 'noop'}, raising=False)
    if hasattr(srv, 'cd_set_store_path'):
        srv.cd_set_store_path(data_dir / 'court_discuss_sessions.json')
    return srv, data_dir


def _read_tasks(data_dir):
    return json.loads((data_dir / 'tasks_source.json').read_text())


def _read_audit(data_dir):
    audit_path = data_dir / 'task_audit_log.json'
    if not audit_path.exists():
        return []
    return json.loads(audit_path.read_text())


def test_healthz(tmp_path, monkeypatch):
    """GET /healthz returns 200 with status ok."""
    srv, data_dir = _configure_server(tmp_path, monkeypatch)

    from http.server import HTTPServer
    port = 18971
    httpd = HTTPServer(('127.0.0.1', port), srv.Handler)
    t = threading.Thread(target=httpd.handle_request, daemon=True)
    t.start()

    time.sleep(0.1)
    conn = HTTPConnection('127.0.0.1', port, timeout=5)
    conn.request('GET', '/healthz')
    resp = conn.getresponse()
    body = json.loads(resp.read())
    conn.close()

    assert resp.status == 200
    assert body['status'] in ('ok', 'degraded')

    httpd.server_close()


def test_legacy_freeze_marker_blocks_post_writes(tmp_path, monkeypatch):
    srv, data_dir = _configure_server(tmp_path, monkeypatch)
    freeze_marker = tmp_path / 'ops' / 'cutover' / '.legacy_write_frozen'
    freeze_marker.parent.mkdir(parents=True)
    freeze_marker.write_text('', encoding='utf-8')
    monkeypatch.setenv('EDICT_LEGACY_WRITE_FREEZE_MARKER', str(freeze_marker))

    from http.server import HTTPServer
    port = 18972
    httpd = HTTPServer(('127.0.0.1', port), srv.Handler)
    t = threading.Thread(target=httpd.handle_request, daemon=True)
    t.start()

    time.sleep(0.1)
    conn = HTTPConnection('127.0.0.1', port, timeout=5)
    payload = json.dumps({'title': '只读期间不应创建', 'targetDept': '工部'})
    conn.request('POST', '/api/create-task', body=payload, headers={'Content-Type': 'application/json'})
    resp = conn.getresponse()
    body = json.loads(resp.read())
    conn.close()

    assert resp.status == 423
    assert body['ok'] is False
    assert 'read-only' in body['error']
    assert _read_tasks(data_dir) == []

    httpd.server_close()


def test_legacy_freeze_marker_still_allows_get_reads(tmp_path, monkeypatch):
    srv, data_dir = _configure_server(tmp_path, monkeypatch)
    freeze_marker = tmp_path / 'ops' / 'cutover' / '.legacy_write_frozen'
    freeze_marker.parent.mkdir(parents=True)
    freeze_marker.write_text('', encoding='utf-8')
    monkeypatch.setenv('EDICT_LEGACY_WRITE_FREEZE_MARKER', str(freeze_marker))
    (data_dir / 'live_status.json').write_text(json.dumps({'tasks': [{'id': 'JJC-READ-001'}], 'syncStatus': {'ok': True}}))

    from http.server import HTTPServer
    port = 18973
    httpd = HTTPServer(('127.0.0.1', port), srv.Handler)
    t = threading.Thread(target=httpd.handle_request, daemon=True)
    t.start()

    time.sleep(0.1)
    conn = HTTPConnection('127.0.0.1', port, timeout=5)
    conn.request('GET', '/api/live-status')
    resp = conn.getresponse()
    body = json.loads(resp.read())
    conn.close()

    assert resp.status == 200
    assert body['tasks'][0]['id'] == 'JJC-READ-001'

    httpd.server_close()


def test_legacy_freeze_marker_blocks_internal_atomic_updates(tmp_path, monkeypatch):
    srv, data_dir = _configure_server(tmp_path, monkeypatch)
    freeze_marker = tmp_path / 'ops' / 'cutover' / '.legacy_write_frozen'
    freeze_marker.parent.mkdir(parents=True)
    freeze_marker.write_text('', encoding='utf-8')
    monkeypatch.setenv('EDICT_LEGACY_WRITE_FREEZE_MARKER', str(freeze_marker))

    (data_dir / 'tasks_source.json').write_text(json.dumps([{'id': 'JJC-LOCK-001', 'title': '保留原样'}]), encoding='utf-8')

    with pytest.raises(LegacyWriteFrozenError):
        srv._atomic_update_tasks(
            lambda tasks: tasks + [{'id': 'JJC-LOCK-002', 'title': '不应写入'}],
            trigger_refresh=False,
        )

    assert _read_tasks(data_dir) == [{'id': 'JJC-LOCK-001', 'title': '保留原样'}]


def test_legacy_freeze_marker_blocks_internal_handle_create_task(tmp_path, monkeypatch):
    srv, data_dir = _configure_server(tmp_path, monkeypatch)
    freeze_marker = tmp_path / 'ops' / 'cutover' / '.legacy_write_frozen'
    freeze_marker.parent.mkdir(parents=True)
    freeze_marker.write_text('', encoding='utf-8')
    monkeypatch.setenv('EDICT_LEGACY_WRITE_FREEZE_MARKER', str(freeze_marker))

    with pytest.raises(LegacyWriteFrozenError):
        srv.handle_create_task('只读期间内部创建也应失败', actor=srv.make_actor_context('emperor', source='test'))

    assert _read_tasks(data_dir) == []
    assert _read_audit(data_dir) == []


def test_court_discuss_session_persists_across_reload(tmp_path, monkeypatch):
    srv, data_dir = _configure_server(tmp_path, monkeypatch)

    created = srv.cd_create('讨论架构改造收尾方案', ['sili', 'zhongshu', 'menxia'], 'JJC-COURT-001')
    assert created['ok'] is True
    session_id = created['session_id']

    store_path = data_dir / 'court_discuss_sessions.json'
    assert store_path.exists()

    advanced = srv.cd_advance(session_id, '请各位速议 P3 收尾重点', None)
    assert advanced['ok'] is True
    assert advanced['round'] == 1

    concluded = srv.cd_conclude(session_id)
    assert concluded['ok'] is True
    assert concluded['summary']

    import court_discuss as cd

    reloaded = importlib.reload(cd)
    reloaded.set_session_store_path(store_path)

    recovered = reloaded.get_session(session_id)
    assert recovered is not None
    assert recovered['phase'] == 'concluded'
    assert recovered['summary'] == concluded['summary']
    assert any('朝堂议政结束' in msg.get('content', '') for msg in recovered['messages'])

    sessions = reloaded.list_sessions()
    assert sessions
    assert sessions[0]['session_id'] == session_id
    assert sessions[0]['summary'] == concluded['summary']


def test_server_concurrent_create_no_duplicate_ids(tmp_path, monkeypatch):
    srv, data_dir = _configure_server(tmp_path, monkeypatch)
    barrier = threading.Barrier(2)

    def worker(title):
        barrier.wait()
        srv.handle_create_task(title, actor=srv.make_actor_context('emperor', source='test'))

    t1 = threading.Thread(target=worker, args=('并发创建任务一号',))
    t2 = threading.Thread(target=worker, args=('并发创建任务二号',))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    tasks = _read_tasks(data_dir)
    ids = [task['id'] for task in tasks]
    assert len(tasks) == 2
    assert len(set(ids)) == 2


def test_server_concurrent_stop_and_todo_no_lost_update(tmp_path, monkeypatch):
    srv, data_dir = _configure_server(tmp_path, monkeypatch)
    task = {
        'id': 'JJC-TEST-001',
        'title': '并发更新',
        'state': 'Doing',
        'org': '工部',
        'targetDept': '工部',
        'flow_log': [],
        'todos': [],
        'updatedAt': '2026-03-24T00:00:00+00:00',
    }
    (data_dir / 'tasks_source.json').write_text(json.dumps([task], ensure_ascii=False))
    barrier = threading.Barrier(2)

    def stop_worker():
        barrier.wait()
        srv.handle_task_action('JJC-TEST-001', 'stop', '人工叫停', actor=srv.make_actor_context('emperor', source='test'))

    def todo_worker():
        barrier.wait()
        srv.update_task_todos('JJC-TEST-001', [{'id': '1', 'title': '修复并发', 'status': 'in-progress'}], actor=srv.make_actor_context('emperor', source='test'))

    t1 = threading.Thread(target=stop_worker)
    t2 = threading.Thread(target=todo_worker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    [task] = _read_tasks(data_dir)
    assert task['state'] == 'Blocked'
    assert task['block'] == '人工叫停'
    assert task['todos'][0]['title'] == '修复并发'


def test_review_reject_from_review_returns_to_doing(tmp_path, monkeypatch):
    srv, data_dir = _configure_server(tmp_path, monkeypatch)
    task = {
        'id': 'JJC-TEST-002',
        'title': '审查退回',
        'state': 'Review',
        'org': '尚书省',
        'targetDept': '工部',
        'flow_log': [],
        'updatedAt': '2026-03-24T00:00:00+00:00',
    }
    (data_dir / 'tasks_source.json').write_text(json.dumps([task], ensure_ascii=False))

    result = srv.handle_review_action('JJC-TEST-002', 'reject', '请返工', actor=srv.make_actor_context('emperor', source='test'))
    assert result['ok'] is True

    [task] = _read_tasks(data_dir)
    assert task['state'] == 'Doing'
    assert task['org'] == '工部'


def test_fast_lane_review_approve_auto_moves_into_next(tmp_path, monkeypatch):
    srv, data_dir = _configure_server(tmp_path, monkeypatch)
    task = {
        'id': 'JJC-TEST-FAST-001',
        'title': '快车道任务',
        'state': 'Menxia',
        'org': '门下省',
        'lane': 'fast',
        'targetDept': '工部',
        'flow_log': [],
        'updatedAt': '2026-03-24T00:00:00+00:00',
    }
    (data_dir / 'tasks_source.json').write_text(json.dumps([task], ensure_ascii=False))

    result = srv.handle_review_action('JJC-TEST-FAST-001', 'approve', '快车道准奏', actor=srv.make_actor_context('menxia', source='test'))
    assert result['ok'] is True

    [task] = _read_tasks(data_dir)
    assert task['state'] == 'Next'
    assert task['org'] == '工部'
    assert any('快车道' in entry.get('remark', '') for entry in task['flow_log'])
    audit_entries = _read_audit(data_dir)
    assert any(entry['action'] == 'review.approve' and entry['to_state'] == 'Next' for entry in audit_entries)


def test_cancelled_cannot_resume(tmp_path, monkeypatch):
    srv, data_dir = _configure_server(tmp_path, monkeypatch)
    task = {
        'id': 'JJC-TEST-003',
        'title': '取消终态',
        'state': 'Cancelled',
        'org': '皇上',
        '_prev_state': 'Doing',
        'targetDept': '工部',
        'flow_log': [],
        'updatedAt': '2026-03-24T00:00:00+00:00',
    }
    (data_dir / 'tasks_source.json').write_text(json.dumps([task], ensure_ascii=False))

    result = srv.handle_task_action('JJC-TEST-003', 'resume', '误恢复', actor=srv.make_actor_context('emperor', source='test'))
    assert result['ok'] is False
    assert '不支持 resume' in result['error'] or '终态' in result['error']


def test_scheduler_update_does_not_clobber_manual_state_change(tmp_path, monkeypatch):
    srv, data_dir = _configure_server(tmp_path, monkeypatch)
    task = {
        'id': 'JJC-TEST-004',
        'title': '调度并发',
        'state': 'Assigned',
        'org': '尚书省',
        'targetDept': '工部',
        'flow_log': [],
        'updatedAt': '2026-03-24T00:00:00+00:00',
    }
    (data_dir / 'tasks_source.json').write_text(json.dumps([task], ensure_ascii=False))
    barrier = threading.Barrier(2)

    def scheduler_worker():
        barrier.wait()
        srv._update_task_scheduler('JJC-TEST-004', lambda _task, sched: sched.update({'lastDispatchStatus': 'queued', 'marker': 'test'}))

    def advance_worker():
        barrier.wait()
        srv.handle_advance_state('JJC-TEST-004', actor=srv.make_actor_context('emperor', source='test'))

    t1 = threading.Thread(target=scheduler_worker)
    t2 = threading.Thread(target=advance_worker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    [task] = _read_tasks(data_dir)
    assert task['state'] == 'Doing'
    assert task['org'] == '工部'
    assert task['_scheduler']['marker'] == 'test'


def test_queue_metrics_reports_central_backlog_and_fast_lane(tmp_path, monkeypatch):
    srv, data_dir = _configure_server(tmp_path, monkeypatch)
    tasks = [
        {
            'id': 'JJC-QUEUE-001',
            'title': '门下审议',
            'state': 'Menxia',
            'org': '门下省',
            'lane': 'fast',
            'updatedAt': '2026-03-24T00:00:00+00:00',
        },
        {
            'id': 'JJC-QUEUE-002',
            'title': '尚书派发',
            'state': 'Assigned',
            'org': '尚书省',
            'lane': 'standard',
            'updatedAt': '2026-03-24T00:00:00+00:00',
        },
    ]
    (data_dir / 'tasks_source.json').write_text(json.dumps(tasks, ensure_ascii=False))

    metrics = srv.get_queue_metrics()
    assert metrics['ok'] is True
    assert metrics['queues']['menxia']['waiting'] == 1
    assert metrics['queues']['menxia']['fastLane'] == 1
    assert metrics['queues']['shangshu']['waiting'] == 1


def test_task_consult_preserves_state_and_logs_consultation(tmp_path, monkeypatch):
    srv, data_dir = _configure_server(tmp_path, monkeypatch)
    task = {
        'id': 'JJC-CONSULT-001',
        'title': '横向咨询',
        'state': 'Assigned',
        'org': '尚书省',
        'lane': 'standard',
        'targetDept': '工部',
        'flow_log': [],
        'updatedAt': '2026-03-24T00:00:00+00:00',
    }
    (data_dir / 'tasks_source.json').write_text(json.dumps([task], ensure_ascii=False))

    result = srv.handle_task_consult('JJC-CONSULT-001', 'gongbu', '请评估执行复杂度', actor=srv.make_actor_context('shangshu', source='test'))
    assert result['ok'] is True

    [task] = _read_tasks(data_dir)
    assert task['state'] == 'Assigned'
    assert task['org'] == '尚书省'
    assert task['consultLog'][0]['to'] == 'gongbu'


def test_archive_all_done_rejects_unauthorized_actor(tmp_path, monkeypatch):
    srv, data_dir = _configure_server(tmp_path, monkeypatch)
    tasks = [
        {
            'id': 'JJC-ARCHIVE-001',
            'title': '已完成任务',
            'state': 'Done',
            'org': '皇上',
            'archived': False,
            'updatedAt': '2026-03-24T00:00:00+00:00',
        }
    ]
    (data_dir / 'tasks_source.json').write_text(json.dumps(tasks, ensure_ascii=False))

    result = srv.handle_archive_task('', True, archive_all_done=True, actor=srv.make_actor_context('gongbu', source='test'))

    assert result['ok'] is False
    assert '无权执行归档' in result['error']
    [task] = _read_tasks(data_dir)
    assert task['archived'] is False


def test_happy_path_with_rework_reaches_done(tmp_path, monkeypatch):
    srv, data_dir = _configure_server(tmp_path, monkeypatch)
    actor = srv.make_actor_context('emperor', source='test')

    created = srv.handle_create_task(
        '请制定工部春季水利整修方案并督办落实',
        target_dept='工部',
        actor=actor,
    )
    assert created['ok'] is True
    task_id = created['taskId']

    assert srv.handle_advance_state(task_id, '司礼监分办', actor=actor)['ok'] is True
    assert srv.handle_advance_state(task_id, '中书起草', actor=actor)['ok'] is True
    assert srv.handle_review_action(task_id, 'reject', '门下请返工补充预算', actor=srv.make_actor_context('menxia', source='test'))['ok'] is True
    assert srv.handle_advance_state(task_id, '中书二次呈报', actor=actor)['ok'] is True
    assert srv.handle_review_action(task_id, 'approve', '门下准奏', actor=srv.make_actor_context('menxia', source='test'))['ok'] is True
    assert srv.handle_advance_state(task_id, '尚书派发', actor=actor)['ok'] is True
    assert srv.handle_advance_state(task_id, '进入执行队列', actor=actor)['ok'] is True
    assert srv.handle_advance_state(task_id, '六部执行完成', actor=actor)['ok'] is True

    [task] = _read_tasks(data_dir)
    assert task['id'] == task_id
    assert task['state'] == 'Done'
    assert task['org'] == '皇上'
    assert int(task.get('review_round') or 0) == 1
    assert task['targetDept'] == '工部'

    audit_entries = _read_audit(data_dir)
    actions = [entry['action'] for entry in audit_entries if entry.get('task_id') == task_id]
    assert 'task.create' in actions
    assert actions.count('review.reject') == 1
    assert actions.count('review.approve') == 1


def test_task_action_stop_resume_cancel_success_path(tmp_path, monkeypatch):
    srv, data_dir = _configure_server(tmp_path, monkeypatch)
    task = {
        'id': 'JJC-ACTION-001',
        'title': '动作回归',
        'state': 'Doing',
        'org': '工部',
        'targetDept': '工部',
        'flow_log': [],
        'updatedAt': '2026-03-24T00:00:00+00:00',
    }
    (data_dir / 'tasks_source.json').write_text(json.dumps([task], ensure_ascii=False))
    actor = srv.make_actor_context('emperor', source='test')

    assert srv.handle_task_action('JJC-ACTION-001', 'stop', '人工暂停排查', actor=actor)['ok'] is True
    [task] = _read_tasks(data_dir)
    assert task['state'] == 'Blocked'
    assert task['_prev_state'] == 'Doing'

    assert srv.handle_task_action('JJC-ACTION-001', 'resume', '问题已解除', actor=actor)['ok'] is True
    [task] = _read_tasks(data_dir)
    assert task['state'] == 'Doing'
    assert task['block'] == '无'

    assert srv.handle_task_action('JJC-ACTION-001', 'cancel', '皇上取消本旨意', actor=actor)['ok'] is True
    [task] = _read_tasks(data_dir)
    assert task['state'] == 'Cancelled'
    assert task['org'] == '皇上'


def test_scheduler_retry_escalate_and_rollback_success_path(tmp_path, monkeypatch):
    srv, data_dir = _configure_server(tmp_path, monkeypatch)
    tasks = [
        {
            'id': 'JJC-SCHED-RETRY-001',
            'title': '自动重试',
            'state': 'Doing',
            'org': '工部',
            'targetDept': '工部',
            'flow_log': [],
            'updatedAt': '2026-03-24T00:00:00+00:00',
        },
        {
            'id': 'JJC-SCHED-ESC-001',
            'title': '自动升级',
            'state': 'Doing',
            'org': '工部',
            'targetDept': '工部',
            'flow_log': [],
            'updatedAt': '2026-03-24T00:00:00+00:00',
        },
        {
            'id': 'JJC-SCHED-ROLL-001',
            'title': '自动回滚',
            'state': 'Review',
            'org': '尚书省',
            'targetDept': '工部',
            'flow_log': [],
            'updatedAt': '2026-03-24T00:00:00+00:00',
            '_scheduler': {
                'enabled': True,
                'retryCount': 2,
                'escalationLevel': 2,
                'autoRollback': True,
                'snapshot': {
                    'state': 'Doing',
                    'org': '工部',
                    'now': '执行中',
                    'savedAt': '2026-03-24T00:00:00+00:00',
                    'note': 'before-review',
                },
            },
        },
    ]
    (data_dir / 'tasks_source.json').write_text(json.dumps(tasks, ensure_ascii=False))
    actor = srv.make_actor_context('sili', source='test')

    retry = srv.handle_scheduler_retry('JJC-SCHED-RETRY-001', '超时未推进', actor=actor)
    assert retry['ok'] is True
    escalate = srv.handle_scheduler_escalate('JJC-SCHED-ESC-001', '持续卡住', actor=actor)
    assert escalate['ok'] is True
    rollback = srv.handle_scheduler_rollback('JJC-SCHED-ROLL-001', '恢复到稳定节点', actor=actor)
    assert rollback['ok'] is True

    tasks_by_id = {item['id']: item for item in _read_tasks(data_dir)}
    assert tasks_by_id['JJC-SCHED-RETRY-001']['_scheduler']['retryCount'] == 1
    assert tasks_by_id['JJC-SCHED-ESC-001']['_scheduler']['escalationLevel'] == 1
    assert tasks_by_id['JJC-SCHED-ROLL-001']['state'] == 'Doing'
    assert tasks_by_id['JJC-SCHED-ROLL-001']['org'] == '工部'


def test_archive_and_archive_all_done_success_path(tmp_path, monkeypatch):
    srv, data_dir = _configure_server(tmp_path, monkeypatch)
    tasks = [
        {
            'id': 'JJC-ARCHIVE-OK-001',
            'title': '单任务归档',
            'state': 'Doing',
            'org': '工部',
            'archived': False,
            'updatedAt': '2026-03-24T00:00:00+00:00',
        },
        {
            'id': 'JJC-ARCHIVE-OK-002',
            'title': '已完成任务',
            'state': 'Done',
            'org': '皇上',
            'archived': False,
            'updatedAt': '2026-03-24T00:00:00+00:00',
        },
        {
            'id': 'JJC-ARCHIVE-OK-003',
            'title': '已取消任务',
            'state': 'Cancelled',
            'org': '皇上',
            'archived': False,
            'updatedAt': '2026-03-24T00:00:00+00:00',
        },
    ]
    (data_dir / 'tasks_source.json').write_text(json.dumps(tasks, ensure_ascii=False))
    actor = srv.make_actor_context('emperor', source='test')

    assert srv.handle_archive_task('JJC-ARCHIVE-OK-001', True, actor=actor)['ok'] is True
    archive_all = srv.handle_archive_task('', True, archive_all_done=True, actor=actor)
    assert archive_all['ok'] is True
    assert archive_all['count'] == 2

    tasks_by_id = {item['id']: item for item in _read_tasks(data_dir)}
    assert tasks_by_id['JJC-ARCHIVE-OK-001']['archived'] is True
    assert tasks_by_id['JJC-ARCHIVE-OK-002']['archived'] is True
    assert tasks_by_id['JJC-ARCHIVE-OK-003']['archived'] is True
