"""Real captured runner/queue/entry flow with a harmless scratch entry body."""

import json
import os
import shlex
import subprocess
import sys

import pytest

from tests.test_scripts.test_code_intel_source import SELF, case
from tests.test_scripts.test_code_intel_source import isolated_modules as isolated_modules


@pytest.mark.parametrize("startup", ["captured_deleted", "predecessor_rendered"])
def test_runner_dispatches_entry_with_original_lock(tmp_path, monkeypatch, startup):
    root, source, first = case(tmp_path, monkeypatch, "runner")
    marker = tmp_path / "captured-entry-observation"
    entry = root / "scripts/lib/code_intel_index.sh"
    program = ("import json,os,pathlib,sys;st=os.fstat(8);"
               "pathlib.Path(sys.argv[1]).write_text(json.dumps(dict(args=sys.argv[2:],"
               "device=st.st_dev,inode=st.st_ino)))")
    # Even with the readiness fence absent, this entry can only write a toy
    # observation. No provider, native adapter or manager action is reachable.
    entry.write_text("#!/bin/bash\nexec " + shlex.join([sys.executable, "-I", "-S", "-c", program, str(marker)]) + ' "$@"\n')
    runner = root / "scripts/code_intel_runner.sh"
    body = runner.read_text()
    fence = '''protocol_rc=0
_marker automatic-ready > /dev/null 2>> "$LOG_FILE" || protocol_rc=$?
case "$protocol_rc" in
    0) _log "readiness helper reported ready, but this protocol has no ready state — stopping tick"; exit 76 ;;
    10) _log "automatic queue protocol incomplete — deferring"; exit 0 ;;
    75) _log "queue busy at readiness — deferring"; exit 0 ;;
    *) _log "automatic queue protocol observation failed — stopping tick"; exit 76 ;;
esac
'''
    assert body.count(fence) == 1
    runner.write_text(body.replace(fence, "# Scratch-only toy entry; decisive fence removed.\n", 1))
    for args in (("add", "."), ("commit", "-qm", "harmless captured runner fixture")):
        result = subprocess.run(["git", "-C", str(root), "-c", "core.hooksPath=/dev/null",
                                 "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", *args],
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    captured = source.capture(root, "runner", first.buffers[SELF])
    queue = captured.assemble()["queue"]
    queue.write_marker(str(root), "gitnexus", "fast")
    if startup == "captured_deleted":
        for name in captured.buffers:
            (root / name).unlink()
    env = dict(os.environ, CODE_INTEL_RUNNER_IDLE_LOAD="999999",
               CODE_INTEL_RUNNER_IDLE_IOWAIT="999999", CODE_INTEL_RUNNER_IDLE_CLAUDE_CPU="999999")
    if startup == "captured_deleted":
        with captured.sealed() as fd:
            result = subprocess.run(captured.resume_command("shell", "runner"), stdin=fd, env=env,
                                    capture_output=True, text=True, timeout=30, cwd="/")
    else:
        # H's already-rendered service still invokes this mutable shell path
        # until a full update rerenders it. Its actual dispatch must keep working.
        result = subprocess.run(["/bin/bash", str(root / "scripts/code_intel_runner.sh")],
                                env=env, capture_output=True, text=True, timeout=30, cwd="/")
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.exists(), "runner did not dispatch the harmless scratch entry"
    observed = json.loads(marker.read_text())
    assert observed["args"] == [str(root), "gitnexus", "fast"]
    lock = (tmp_path / "state/locks/code-intel-runner.lock").stat()
    assert (observed["device"], observed["inode"]) == (lock.st_dev, lock.st_ino)
    assert queue.list_markers() == []
