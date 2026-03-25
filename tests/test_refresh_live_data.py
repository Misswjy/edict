"""tests for scripts/refresh_live_data.py"""

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))

import refresh_live_data as refresh_live_data


def test_refresh_live_data_skips_live_status_write_when_legacy_frozen(tmp_path, monkeypatch):
    data_dir = tmp_path / 'data'
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / 'tasks_source.json').write_text(
        json.dumps([{'id': 'JJC-FROZEN-LIVE-001', 'title': '冻结期间不刷新', 'state': 'Doing'}], ensure_ascii=False),
        encoding='utf-8',
    )
    live_status_path = data_dir / 'live_status.json'
    sentinel = {'generatedAt': 'before-freeze', 'tasks': [{'id': 'OLD'}]}
    live_status_path.write_text(json.dumps(sentinel, ensure_ascii=False), encoding='utf-8')

    freeze_marker = tmp_path / 'ops' / 'cutover' / '.legacy_write_frozen'
    freeze_marker.parent.mkdir(parents=True, exist_ok=True)
    freeze_marker.write_text('', encoding='utf-8')
    monkeypatch.setenv('EDICT_LEGACY_WRITE_FREEZE_MARKER', str(freeze_marker))
    monkeypatch.setattr(refresh_live_data, 'DATA', data_dir, raising=False)

    result = refresh_live_data.main()

    assert json.loads(live_status_path.read_text(encoding='utf-8')) == sentinel
    assert result == {
        'ok': True,
        'skipped': True,
        'readOnly': True,
        'freezeMarker': str(freeze_marker),
    }

