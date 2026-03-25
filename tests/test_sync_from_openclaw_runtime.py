"""tests for scripts/sync_from_openclaw_runtime.py"""

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))

import sync_from_openclaw_runtime as sync_runtime


def test_sync_runtime_skips_tasks_source_write_when_legacy_frozen(tmp_path, monkeypatch):
    data_dir = tmp_path / 'data'
    data_dir.mkdir(parents=True, exist_ok=True)
    tasks_path = data_dir / 'tasks_source.json'
    original_tasks = [{'id': 'JJC-FROZEN-001', 'title': '保留原样', 'state': 'Doing'}]
    tasks_path.write_text(json.dumps(original_tasks, ensure_ascii=False), encoding='utf-8')

    freeze_marker = tmp_path / 'ops' / 'cutover' / '.legacy_write_frozen'
    freeze_marker.parent.mkdir(parents=True, exist_ok=True)
    freeze_marker.write_text('', encoding='utf-8')
    monkeypatch.setenv('EDICT_LEGACY_WRITE_FREEZE_MARKER', str(freeze_marker))

    monkeypatch.setattr(sync_runtime, 'DATA', data_dir, raising=False)
    monkeypatch.setattr(sync_runtime, 'SYNC_STATUS', data_dir / 'sync_status.json', raising=False)
    monkeypatch.setattr(sync_runtime, 'SESSIONS_ROOT', tmp_path / '.openclaw' / 'agents', raising=False)

    sync_runtime.main()

    assert json.loads(tasks_path.read_text(encoding='utf-8')) == original_tasks

    status = json.loads((data_dir / 'sync_status.json').read_text(encoding='utf-8'))
    assert status['ok'] is True
    assert status['skipped'] is True
    assert status['readOnly'] is True
    assert 'read-only' in status['note']
    assert status['freezeMarker'] == str(freeze_marker)
