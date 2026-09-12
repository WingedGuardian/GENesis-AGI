"""Tests for the guardian-gateway.sh ``cc-tmp-apply`` verb.

Runs the REAL gateway against a throwaway ``$HOME`` install dir. The verb sources
``$INSTALL_DIR/scripts/lib/cc_tmp_volume.sh`` and calls ``cc_tmp_volume_apply``,
then emits ONE JSON line on stdout carrying the lib's result token (the lib's
own incus logic is covered by test_cc_tmp_volume_sh.py). Here we pin the verb's
CONTRACT: lib-missing → clean JSON error; stdout is exactly the JSON line while
the lib's human progress goes to stderr; result/claude_procs/applied reflect the
lib's outcome; a non-numeric count serializes as JSON ``null``.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

_GATEWAY = Path(__file__).resolve().parents[2] / "scripts" / "guardian-gateway.sh"


def _run(home: Path):
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["SSH_ORIGINAL_COMMAND"] = "cc-tmp-apply"
    return subprocess.run(
        ["bash", str(_GATEWAY)],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )


def _install_stub_lib(home: Path, *, result: str, procs: str, human: str = "progress") -> None:
    """Drop a stub cc_tmp_volume.sh in the install dir that just sets the result
    seam vars and prints a human line (which the verb must route to stderr)."""
    lib = home / ".local" / "share" / "genesis-guardian" / "scripts" / "lib" / "cc_tmp_volume.sh"
    lib.parent.mkdir(parents=True, exist_ok=True)
    lib.write_text(
        "_cctmpvol_result=''\n"
        "_cctmpvol_claude_procs=''\n"
        "cc_tmp_volume_apply() {\n"
        f"    echo '{human}'\n"
        f"    _cctmpvol_result='{result}'\n"
        f"    _cctmpvol_claude_procs='{procs}'\n"
        "}\n"
    )


def test_lib_missing_returns_clean_json_error(tmp_path):
    (tmp_path / ".local" / "share" / "genesis-guardian").mkdir(parents=True)
    proc = _run(tmp_path)
    assert proc.returncode == 1
    payload = json.loads(proc.stderr.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["action"] == "cc-tmp-apply"
    assert "not found" in payload["error"]


def test_emits_single_json_line_human_to_stderr(tmp_path):
    _install_stub_lib(tmp_path, result="live-cc", procs="4", human="SHOULD_BE_STDERR")
    proc = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    # stdout is EXACTLY the JSON line — nothing else.
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1, proc.stdout
    payload = json.loads(lines[0])
    assert payload == {
        "ok": True,
        "action": "cc-tmp-apply",
        "result": "live-cc",
        "claude_procs": 4,
        "applied": False,
    }
    # the lib's human progress must NOT pollute stdout
    assert "SHOULD_BE_STDERR" not in proc.stdout
    assert "SHOULD_BE_STDERR" in proc.stderr


def test_applied_flag_true_only_on_applied(tmp_path):
    _install_stub_lib(tmp_path, result="applied", procs="")
    payload = json.loads(_run(tmp_path).stdout.strip())
    assert payload["result"] == "applied"
    assert payload["applied"] is True
    assert payload["claude_procs"] is None  # empty count → JSON null


def test_non_applied_result_is_not_applied(tmp_path):
    _install_stub_lib(tmp_path, result="unsupported-pool", procs="")
    payload = json.loads(_run(tmp_path).stdout.strip())
    assert payload["result"] == "unsupported-pool"
    assert payload["applied"] is False


def test_non_numeric_count_serializes_null(tmp_path):
    _install_stub_lib(tmp_path, result="no-incus", procs="not-a-number")
    payload = json.loads(_run(tmp_path).stdout.strip())
    assert payload["claude_procs"] is None
