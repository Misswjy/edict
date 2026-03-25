"""tests for scripts/run_loop.sh freeze behavior."""

from __future__ import annotations

import pathlib
import subprocess


ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_run_loop_exits_cleanly_when_legacy_is_frozen(tmp_path):
    freeze_marker = tmp_path / 'ops' / 'cutover' / '.legacy_write_frozen'
    freeze_marker.parent.mkdir(parents=True, exist_ok=True)
    freeze_marker.write_text('', encoding='utf-8')

    log_path = tmp_path / 'legacy-run-loop.log'
    pid_path = tmp_path / 'legacy-run-loop.pid'
    env = {
        'EDICT_LEGACY_WRITE_FREEZE_MARKER': str(freeze_marker),
        'EDICT_LEGACY_LOOP_LOG': str(log_path),
        'EDICT_LEGACY_LOOP_PIDFILE': str(pid_path),
    }

    completed = subprocess.run(
        ['bash', str(ROOT / 'scripts' / 'run_loop.sh'), '1', '1'],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        env=env,
        check=False,
    )

    assert completed.returncode == 0
    assert 'legacy write freeze active' in completed.stdout
    assert not pid_path.exists()
    assert not log_path.exists()
