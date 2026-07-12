"""Behavioral tests for the shared host-alignment function cc_align_host_sync
(scripts/lib/cc_version.sh) and the nightly timer entrypoint scripts/cc_align_host.sh.

cc_align_host_sync was factored OUT of update.sh's _sync_deploy_targets so the
nightly genesis-cc-align timer and update.sh run the IDENTICAL host CC/Node
alignment. The drift→update-cc/update-node logic previously had no direct
behavioral test (only the config-unreadable path and structural greps in
test_update_host_sync.py), so it is covered here: the function is sourced from
the REAL cc_version.sh and driven with a stubbed ``ssh`` on PATH that records
its calls, so the logic under test is the shipped code.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CC_ENV = REPO_ROOT / "scripts" / "lib" / "cc_version.sh"
UPDATE_SH = REPO_ROOT / "scripts" / "update.sh"
ALIGN_SH = REPO_ROOT / "scripts" / "cc_align_host.sh"


def _pins() -> tuple[str, str]:
    """The repo CC + Node pins (read from cc_version.sh so the tests track a
    pin bump instead of hardcoding a version that goes stale)."""
    out = subprocess.run(
        [
            "bash",
            "-c",
            f'unset CC_VERSION NODE_MAJOR; source "{CC_ENV}"; '
            'printf "%s\\n%s\\n" "$CC_VERSION" "$NODE_MAJOR"',
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert out.returncode == 0, out.stderr
    cc, node = out.stdout.split()
    return cc, node


def _run_align(host_ver_raw: str, tmp_path, ssh_rc: int = 0):
    """Source cc_version.sh and invoke cc_align_host_sync with a stub ``ssh`` on
    PATH (records its args to a file, exits ssh_rc). Returns (proc, ssh_calls)."""
    bind = tmp_path / "bin"
    bind.mkdir(exist_ok=True)
    record = tmp_path / "ssh_calls.txt"
    ssh_stub = bind / "ssh"
    ssh_stub.write_text(
        f"#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> '{record}'\nexit {ssh_rc}\n"
    )
    ssh_stub.chmod(0o755)
    harness = (
        f'export PATH="{bind}:$PATH"\n'
        "unset CC_VERSION NODE_MAJOR\n"
        f'source "{CC_ENV}"\n'
        'HOST_CC_DEGRADED=""\n'
        "cc_align_host_sync 'opuser' '192.0.2.7' '/nonexistent/key' "
        f"{shlex.quote(host_ver_raw)}\n"
        'echo "RC=$?"\n'
        'echo "DEGRADED=$HOST_CC_DEGRADED"\n'
    )
    proc = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, timeout=60)
    calls = record.read_text() if record.exists() else ""
    return proc, calls


def _vi(cc: str, node_major: str) -> str:
    return f'{{"cc_version": "{cc} (Claude Code)", "node_version": "v{node_major}.0.0"}}'


def test_aligned_host_issues_no_ssh(tmp_path):
    cc, node = _pins()
    proc, calls = _run_align(_vi(cc, node), tmp_path)
    assert "RC=0" in proc.stdout, proc.stdout
    assert "update-cc" not in calls and "update-node" not in calls, calls
    assert "DEGRADED=\n" in proc.stdout or proc.stdout.rstrip().endswith("DEGRADED=")


def test_cc_drift_issues_update_cc(tmp_path):
    cc, node = _pins()
    proc, calls = _run_align(_vi("0.0.1", node), tmp_path)
    assert f"update-cc {cc}" in calls, calls
    assert "update-node" not in calls, calls
    assert proc.stdout.rstrip().endswith("DEGRADED=")  # success → no degrade


def test_node_drift_issues_update_node(tmp_path):
    cc, node = _pins()
    proc, calls = _run_align(_vi(cc, "1"), tmp_path)
    assert f"update-node {node}" in calls, calls


def test_cc_absent_installs_pin(tmp_path):
    cc, node = _pins()
    proc, calls = _run_align(_vi("unavailable", node), tmp_path)
    assert f"update-cc {cc}" in calls, calls


def test_ssh_failure_marks_degraded(tmp_path):
    cc, node = _pins()
    proc, calls = _run_align(_vi("0.0.1", node), tmp_path, ssh_rc=1)
    assert "DEGRADED=" in proc.stdout
    degraded = proc.stdout.split("DEGRADED=", 1)[1].strip()
    assert "guardian_host_cc" in degraded, proc.stdout
    assert "RC=0" in proc.stdout  # non-fatal contract: always returns 0


def test_unreachable_marks_degraded_and_issues_no_ssh(tmp_path):
    proc, calls = _run_align("", tmp_path)
    degraded = proc.stdout.split("DEGRADED=", 1)[1].strip()
    assert degraded == "guardian_host_unreachable", proc.stdout
    assert calls == "", calls  # no probe when the host is unreachable
    assert "RC=0" in proc.stdout


def test_always_returns_zero_on_node_failure(tmp_path):
    """Non-fatal by contract even when a sync fails — a host hiccup must never
    abort the calling update run under set -e."""
    cc, node = _pins()
    proc, _ = _run_align(_vi(cc, "1"), tmp_path, ssh_rc=1)
    assert "RC=0" in proc.stdout, proc.stdout


def test_update_sh_delegates_to_shared_aligner():
    """update.sh must call the shared aligner, not re-inline host sync — the
    whole point of the factoring is that the timer and update.sh cannot diverge."""
    assert "cc_align_host_sync " in UPDATE_SH.read_text(), (
        "update.sh must delegate host CC/Node alignment to cc_align_host_sync"
    )


def test_timer_script_sources_shared_aligner_and_self_guards():
    """The timer must source the shared aligner (not duplicate the logic), guard
    against concurrent runs, and reset the degraded accumulator before use."""
    txt = ALIGN_SH.read_text()
    assert "cc_align_host_sync " in txt, "timer must call the shared aligner"
    assert "flock -n" in txt, "timer must single-flight to avoid concurrent update-cc"
    assert 'HOST_CC_DEGRADED=""' in txt, "timer must init the degraded accumulator (set -u)"
    assert "unset CC_VERSION NODE_MAJOR" in txt, "timer must let the repo pin win"


def test_timer_script_parses_clean():
    res = subprocess.run(["bash", "-n", str(ALIGN_SH)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
