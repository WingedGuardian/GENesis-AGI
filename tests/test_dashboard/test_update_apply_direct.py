"""_apply_direct cgroup isolation (deploy-audit P5-B, part 1 / architect NOTE-1).

The direct update path must spawn update.sh in its OWN systemd scope, so
update.sh's `systemctl stop genesis-server` cannot SIGTERM it via the shared
cgroup (which aborts the update, or self-SIGTERMs into a spurious rollback at
the final restart). Falls back to start_new_session if systemd-run is absent.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from genesis.dashboard.routes import updates


@pytest.fixture
def _passthrough_jsonify():
    with patch.object(updates, "jsonify", lambda x: x):
        yield


def test_apply_direct_uses_systemd_run_scope(tmp_path, _passthrough_jsonify):
    fake_proc = MagicMock(pid=12345)
    pid_file = tmp_path / ".genesis" / "update_in_progress.pid"
    with (
        patch.object(updates.subprocess, "Popen", return_value=fake_proc) as popen,
        patch.object(updates, "_HOME", tmp_path),
    ):
        updates._apply_direct(pid_file)
    argv = popen.call_args_list[0][0][0]
    assert argv[0] == "systemd-run"
    assert "--user" in argv and "--scope" in argv
    assert "bash" in argv and any(str(updates._UPDATE_SCRIPT) == a for a in argv)
    assert pid_file.read_text() == "12345"


def test_apply_direct_falls_back_when_systemd_run_missing(tmp_path, _passthrough_jsonify):
    fake_proc = MagicMock(pid=999)
    pid_file = tmp_path / ".genesis" / "update_in_progress.pid"
    with (
        patch.object(
            updates.subprocess, "Popen", side_effect=[FileNotFoundError(), fake_proc]
        ) as popen,
        patch.object(updates, "_HOME", tmp_path),
    ):
        updates._apply_direct(pid_file)
    # Second call is the fallback — a plain bash spawn with start_new_session.
    fallback_argv = popen.call_args_list[1][0][0]
    fallback_kwargs = popen.call_args_list[1][1]
    assert fallback_argv[0] == "bash"
    assert fallback_kwargs.get("start_new_session") is True
    assert pid_file.read_text() == "999"


def test_apply_guard_and_status_use_update_in_progress():
    """Part 3 wiring: both the concurrency guard and the status route now consult
    the canonical env.update_in_progress (marker AND state file), not a PID-file-
    only check — so a CLI run is seen and its crash-recovery state is not GC'd."""
    from pathlib import Path

    text = Path(updates.__file__).read_text()
    # The old PID-file-only liveness gate must be gone from the guard/status.
    assert 'result = subprocess.run(["kill", "-0", str(old_pid)]' not in text
    assert "in_progress = update_in_progress()" in text
    assert text.count("if update_in_progress():") >= 1  # the apply guard
