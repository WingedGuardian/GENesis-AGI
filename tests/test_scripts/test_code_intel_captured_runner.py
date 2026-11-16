"""Captured runner dispatch retains its source and canonical lock after checkout loss."""
import fcntl
import json
import os
import subprocess
import time

import pytest

from tests.test_scripts.test_code_intel_source import isolated_modules as isolated_modules
from tests.test_scripts.test_code_intel_typed_runner import prepared


@pytest.mark.parametrize("startup", ["captured_deleted", "predecessor_rendered"])
def test_runner_dispatches_entry_with_original_lock(tmp_path, monkeypatch, startup):
    snapshot, queue, marker = prepared(tmp_path, monkeypatch, "valid")
    if startup == "captured_deleted":
        for name in snapshot.buffers:
            (snapshot.root/name).unlink()
    env = dict(os.environ, CODE_INTEL_RUNNER_IDLE_LOAD="999999",
               CODE_INTEL_RUNNER_IDLE_IOWAIT="999999", CODE_INTEL_RUNNER_IDLE_CLAUDE_CPU="999999")
    if startup == "captured_deleted":
        with snapshot.sealed() as fd:
            result = subprocess.run(snapshot.resume_command("shell", "runner"), stdin=fd, env=env,
                                    capture_output=True, text=True, timeout=30, cwd="/")
    else:
        # Existing rendered units enter this shell path until a full update.
        result = subprocess.run(["/bin/bash", str(snapshot.root/"scripts/code_intel_runner.sh")],
                                env=env, capture_output=True, text=True, timeout=30, cwd="/")
    assert result.returncode == 0, result.stdout+result.stderr
    observed = json.loads(marker.read_text())
    assert observed["context"]["repo_path"] == str(snapshot.root)
    lock = (tmp_path/"state/locks/code-intel-runner.lock").stat()
    assert (observed["device"], observed["inode"]) == (lock.st_dev, lock.st_ino)
    assert {row["provider"] for row in queue.list_markers()} == {"cbm", "gitnexus"}


@pytest.mark.parametrize('failure', [1, 2, 75, 76, 'signal'])
def test_legacy_ready_runner_maps_capture_failure_before_queue_actions(tmp_path, monkeypatch, failure):
    snapshot, queue, marker = prepared(tmp_path, monkeypatch, 'valid')
    # Only this owned scratch entry is replaced; no provider is reachable.
    entry = snapshot.root/'scripts/lib/code_intel_source.py'
    entry.write_text('import os,signal\nos.kill(os.getpid(),signal.SIGTERM)\n'
                     if failure == 'signal' else 'raise SystemExit('+str(failure)+')\n')
    result = subprocess.run(['/bin/bash', str(snapshot.root/'scripts/code_intel_runner.sh')],
                            capture_output=True, text=True, timeout=30, cwd='/')
    assert result.returncode == (failure if failure in (75, 76) else 76), result.stderr
    assert not marker.exists() and len(queue.list_markers()) == 2
    assert all(row['attempts'] == 0 for row in queue.list_markers())
    assert all(queue.get_inflight(row['hash']) is None for row in queue.list_markers())


def test_competitor_can_win_raw_recapture_gap_without_stale_queue_action(tmp_path, monkeypatch):
    snapshot, queue, marker = prepared(tmp_path, monkeypatch, 'valid')
    ready, allow = tmp_path/'recapture-ready', tmp_path/'recapture-allow'
    # The harmless entry pauses exactly at Source invocation, then resumes the
    # actual sealed runner. Parent owns the competing descriptor and release.
    entry = snapshot.root/'scripts/lib/code_intel_source.py'
    entry.write_text('import os,time\nfrom pathlib import Path\n'
        + 'Path('+repr(str(ready))+').touch()\n'
        + 'deadline=time.monotonic()+10\n'
        + 'while not Path('+repr(str(allow))+').exists():\n'
        + ' if time.monotonic()>deadline: raise RuntimeError("owned recapture barrier expired")\n'
        + ' time.sleep(.01)\n'
        + 'command='+repr(snapshot.resume_command('shell', 'runner'))+'\n'
        + 'os.execv(command[0],command)\n')
    lock = tmp_path/'state/locks/code-intel-runner.lock'
    lock.parent.mkdir(parents=True, exist_ok=True)
    with snapshot.sealed() as fd, lock.open('a') as contender:
        process = subprocess.Popen(['/bin/bash', str(snapshot.root/'scripts/code_intel_runner.sh')],
            stdin=fd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd='/')
        try:
            deadline = time.monotonic()+10
            while not ready.exists():
                assert process.poll() is None and time.monotonic()<deadline, 'raw recapture not reached'
                time.sleep(.01)
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            allow.touch()
            stdout, stderr = process.communicate(timeout=30)
            assert process.returncode == 0, stdout+stderr
        finally:
            allow.touch()
            fcntl.flock(contender, fcntl.LOCK_UN)
            if process.poll() is None:
                process.communicate(timeout=30)
    assert 'another runner tick' in (tmp_path/'state/code-intelligence-runner.log').read_text()
    assert not marker.exists() and len(queue.list_markers()) == 2
    assert all(row['attempts'] == 0 and queue.get_inflight(row['hash']) is None
               for row in queue.list_markers())
