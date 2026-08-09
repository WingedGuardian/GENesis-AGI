"""Behavioral tests for the container-side entrypoint scripts/cc_tmp_align_host.sh.

It SSHes the guardian `cc-tmp-apply` verb and records the outcome in
~/.genesis/state/cc_tmp_apply.json (read by the infra_profile storage collector,
which feeds the awareness posture nag). The script resolves its venv python from
its own location, so the harness copies the REAL script into a tmp GENESIS_ROOT
with a symlink to the running venv python (needs yaml) and stubs `ssh` on PATH —
so the logic under test is the shipped code, end to end.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ALIGN = REPO_ROOT / "scripts" / "cc_tmp_align_host.sh"


def _harness(tmp_path, *, ssh_response="", ssh_rc=0, with_guardian=True, with_key=True):
    root = tmp_path  # doubles as GENESIS_ROOT (has .venv) and HOME (has .genesis/.ssh)
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    script = root / "scripts" / "cc_tmp_align_host.sh"
    script.write_text(ALIGN.read_text())
    script.chmod(0o755)
    # Real venv python (has yaml + json) so the script's absolute VENV_PY resolves.
    venvbin = root / ".venv" / "bin"
    venvbin.mkdir(parents=True)
    os.symlink(sys.executable, venvbin / "python")

    gdir = root / ".genesis"
    gdir.mkdir(exist_ok=True)
    if with_guardian:
        (gdir / "guardian_remote.yaml").write_text("host_ip: 192.0.2.9\nhost_user: opuser\n")
    sshdir = root / ".ssh"
    sshdir.mkdir(exist_ok=True)
    if with_key:
        (sshdir / "genesis_guardian_ed25519").write_text("KEY")

    bind = root / "bin"
    bind.mkdir(exist_ok=True)
    respfile = root / "ssh_response.txt"
    respfile.write_text(ssh_response)
    ssh = bind / "ssh"
    ssh.write_text(f"#!/usr/bin/env bash\ncat '{respfile}'\nexit {ssh_rc}\n")
    ssh.chmod(0o755)

    env = dict(os.environ)
    env["HOME"] = str(root)
    env["PATH"] = f"{bind}:/usr/bin:/bin"
    proc = subprocess.run(
        ["bash", str(script)], env=env, capture_output=True, text=True, timeout=60
    )
    marker = root / ".genesis" / "state" / "cc_tmp_apply.json"
    data = json.loads(marker.read_text()) if marker.exists() else None
    return proc, data


def _applied_json(result="applied", applied=True, procs=None):
    return json.dumps(
        {
            "ok": True,
            "action": "cc-tmp-apply",
            "result": result,
            "claude_procs": procs,
            "applied": applied,
        }
    )


def test_parses_clean():
    res = subprocess.run(["bash", "-n", str(ALIGN)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def test_guardian_less_is_clean_noop(tmp_path):
    proc, marker = _harness(tmp_path, with_guardian=False)
    assert proc.returncode == 0
    assert marker is None  # no host plane → nothing attempted, no marker


def test_missing_key_skips(tmp_path):
    proc, marker = _harness(tmp_path, with_key=False)
    assert proc.returncode == 0
    assert marker is None


def test_writes_applied_marker(tmp_path):
    proc, marker = _harness(tmp_path, ssh_response=_applied_json())
    assert proc.returncode == 0, proc.stderr
    assert marker["last_reason"] == "applied"
    assert marker["applied"] is True
    assert "last_attempt_at" in marker and marker["last_attempt_at"]


def test_writes_live_cc_marker_with_count(tmp_path):
    proc, marker = _harness(
        tmp_path, ssh_response=_applied_json(result="live-cc", applied=False, procs=6)
    )
    assert proc.returncode == 0
    assert marker["last_reason"] == "live-cc"
    assert marker["applied"] is False
    assert marker["claude_procs"] == 6


def test_gateway_error_json_marks_gateway_error(tmp_path):
    resp = json.dumps({"ok": False, "action": "cc-tmp-apply", "error": "lib missing"})
    proc, marker = _harness(tmp_path, ssh_response=resp)
    assert proc.returncode == 0
    assert marker["last_reason"] == "gateway-error"
    assert marker["applied"] is False


def test_old_gateway_denied_is_honest_marker(tmp_path):
    # A gateway predating the verb answers a non-JSON "denied" line.
    proc, marker = _harness(tmp_path, ssh_response='{"ok": false, "error": "denied"}\n')
    assert proc.returncode == 0
    # 'denied' arrives as a valid ok=false JSON here → gateway-error; a truly
    # non-JSON line is covered below.
    assert marker["last_reason"] in ("gateway-error", "unreachable-or-old-gateway")


def test_non_json_response_is_unreachable_marker(tmp_path):
    proc, marker = _harness(tmp_path, ssh_response="not json at all\n")
    assert proc.returncode == 0
    assert marker["last_reason"] == "unreachable-or-old-gateway"
    assert marker["applied"] is False


def test_unreachable_empty_response_is_marker(tmp_path):
    proc, marker = _harness(tmp_path, ssh_response="", ssh_rc=255)
    assert proc.returncode == 0
    assert marker["last_reason"] == "unreachable-or-old-gateway"


def test_self_guards_present():
    txt = ALIGN.read_text()
    assert "flock -n" in txt, "must single-flight"
    assert "guardian_remote.yaml" in txt, "must no-op without a guardian"
    assert "cc-tmp-apply" in txt, "must call the gateway verb"
