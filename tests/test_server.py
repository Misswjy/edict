"""tests for dashboard/server.py route handling and task mutations"""

import json
import pathlib
import sys
import threading
import time
from http.client import HTTPConnection

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'dashboard'))
sys.path.insert(0, str(ROOT / 'scripts'))


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
    return srv, data_dir


def _read_tasks(data_dir):
    return json.loads((data_dir / 'tasks_source.json').read_text())


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
