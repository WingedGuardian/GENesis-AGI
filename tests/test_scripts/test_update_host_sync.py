"""Host-sync config parsing in scripts/update.sh (`_sync_deploy_targets`).

PR #898 extracted the deploy-target sync into a function and re-indented the
inline ``python -c`` snippets that parse ``guardian_remote.yaml``. Indented
top-level Python is an IndentationError; the ``2>/dev/null || true`` swallowed
it, ``HOST_IP`` resolved empty, and the ENTIRE host block — guardian redeploy
plus host Node/CC pin healing — silently skipped on every update run, with
``HOST_CC_DEGRADED`` never set (update_history reported the host healthy).

These tests extract the snippets and the function from the REAL update.sh and
execute them, so the logic under test is the shipped script, not a copy. A
class-level guardrail scans every shell script for the indented-multiline
``python -c`` shape so the regression cannot be reintroduced elsewhere.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
UPDATE_SH = SCRIPTS_DIR / "update.sh"

SAMPLE_YAML = 'host_ip: "192.0.2.7"\nhost_user: "opuser"\n'


def _extract_snippet(var: str) -> str:
    """Pull the python -c payload off the HOST_IP=/HOST_USER= line."""
    for line in UPDATE_SH.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith(f'{var}=$(') and '-c "' in stripped:
            m = re.search(r'-c "(.+)" 2>/dev/null', stripped)
            assert m, f"could not extract -c payload from {var} line: {stripped}"
            return m.group(1)
    pytest.fail(f"no single-line {var}= python -c assignment found in update.sh")


def _run_snippet(var: str, config_path: Path) -> subprocess.CompletedProcess:
    code = _extract_snippet(var).replace("$GUARDIAN_CONFIG", str(config_path))
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=30
    )


def test_host_ip_snippet_parses_and_reads_config(tmp_path):
    """The exact shipped snippet must be valid Python and yield host_ip.

    With the pre-fix indented body this fails with IndentationError.
    """
    cfg = tmp_path / "guardian_remote.yaml"
    cfg.write_text(SAMPLE_YAML)
    result = _run_snippet("HOST_IP", cfg)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "192.0.2.7"


def test_host_user_snippet_parses_and_defaults(tmp_path):
    cfg = tmp_path / "guardian_remote.yaml"
    cfg.write_text(SAMPLE_YAML)
    result = _run_snippet("HOST_USER", cfg)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "opuser"

    cfg.write_text('host_ip: "192.0.2.7"\n')
    result = _run_snippet("HOST_USER", cfg)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ubuntu"


def test_no_indented_multiline_python_bodies_in_shell_scripts():
    """Class guardrail: a multi-line ``python -c "`` whose body's first line is
    indented is a top-level IndentationError waiting behind 2>/dev/null.

    Unindented multi-line bodies (first line at column 0) are fine and exist
    legitimately (e.g. the network-identity block in update.sh).
    """
    violations = []
    for script in sorted(SCRIPTS_DIR.rglob("*.sh")):
        text = script.read_text()
        for m in re.finditer(r'-c "\n[ \t]+', text):
            line_no = text[: m.start()].count("\n") + 1
            violations.append(f"{script.relative_to(REPO_ROOT)}:{line_no}")
    assert not violations, (
        "indented multi-line python -c body (silent IndentationError under "
        f"2>/dev/null): {violations} — inline the snippet on one line or "
        "start its body at column 0"
    )


def _extract_sync_function() -> str:
    text = UPDATE_SH.read_text()
    start = text.index("_sync_deploy_targets() {")
    end = text.index("\n}", start) + 2
    return text[start:end]


def test_unusable_guardian_config_warns_and_marks_degraded(tmp_path):
    """Config present but unusable (here: SSH key absent) must be LOUD:
    warning on stdout + guardian_config_unreadable in HOST_CC_DEGRADED,
    never a silent skip."""
    home = tmp_path / "home"
    (home / ".genesis").mkdir(parents=True)
    (home / ".genesis" / "guardian_remote.yaml").write_text(SAMPLE_YAML)

    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").symlink_to(sys.executable)

    script_dir = tmp_path / "scripts"  # no lib/cc_version.sh → CC sync skips
    script_dir.mkdir()

    harness = (
        f"HOME={home}\nVENV_DIR={tmp_path / 'venv'}\nSCRIPT_DIR={script_dir}\n"
        f"GENESIS_ROOT={tmp_path}\n"
        + _extract_sync_function()
        + '\n_sync_deploy_targets\necho "DEGRADED=$HOST_CC_DEGRADED"\n'
    )
    result = subprocess.run(
        ["bash", "-c", harness], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert "host sync SKIPPED" in result.stdout
    assert "DEGRADED=guardian_config_unreadable" in result.stdout


# ── Guardian redeploy reconciliation (PR-D) ─────────────────────────────────
# The redeploy trigger keys on the host's ACTUAL deployed_commit (observable
# state), not on whether THIS run's git pull touched guardian paths. The old
# pull-delta gate (OLD_COMMIT→HEAD) silently skipped the redeploy whenever the
# host was last deployed from a since-rebased local HEAD, stranding it on an
# orphan commit. These tests run the shipped pure decision helper directly.


def _extract_reason_function() -> str:
    text = UPDATE_SH.read_text()
    start = text.index("_guardian_redeploy_reason() {")
    end = text.index("\n}", start) + 2
    return text[start:end]


def _run_reason(*args: str) -> subprocess.CompletedProcess:
    """Source the REAL _guardian_redeploy_reason from update.sh and invoke it.

    Runs under ``set -euo pipefail`` so a set -e / unbound-var regression in the
    helper surfaces as a non-zero exit, not a silently-empty reason.

    Arg order: reachable recognized host_commit head_commit host_differ pull_differ
    """
    harness = (
        "set -euo pipefail\n"
        + _extract_reason_function()
        + "\n_guardian_redeploy_reason "
        + " ".join(shlex.quote(a) for a in args)
        + "\n"
    )
    return subprocess.run(
        ["bash", "-c", harness], capture_output=True, text=True, timeout=30
    )


def test_reason_in_sync_is_empty():
    """Reachable host, recognized commit, no guardian-path drift → no redeploy
    (and specifically NOT triggered by an unrelated pull-delta)."""
    r = _run_reason("1", "1", "abc1234", "def5678", "0", "1")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "", r.stdout


def test_reason_host_drift_redeploys():
    r = _run_reason("1", "1", "abc1234", "def5678", "1", "0")
    assert r.returncode == 0, r.stderr
    assert "drift" in r.stdout
    assert "abc1234" in r.stdout and "def5678" in r.stdout


def test_reason_unrecognized_commit_redeploys():
    """Orphaned / GC'd deployed_commit we cannot diff → reconcile unconditionally."""
    r = _run_reason("1", "0", "0ffaced", "def5678", "0", "0")
    assert r.returncode == 0, r.stderr
    assert "unrecognized" in r.stdout
    assert "def5678" in r.stdout


def test_reason_unknown_deployed_commit_redeploys():
    """Host never deployed / gateway returned "unknown" → reconcile."""
    r = _run_reason("1", "0", "unknown", "def5678", "0", "0")
    assert r.returncode == 0, r.stderr
    assert "unrecognized" in r.stdout


def test_reason_unreachable_falls_back_to_pull_delta():
    """Cannot read host state; legacy pull-delta says guardian paths changed."""
    r = _run_reason("0", "0", "", "def5678", "0", "1")
    assert r.returncode == 0, r.stderr
    assert "pull-delta" in r.stdout


def test_reason_unreachable_no_pull_delta_skips():
    """Cannot read host state and nothing changed this run → skip (legacy)."""
    r = _run_reason("0", "0", "", "def5678", "0", "0")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == ""


def test_redeploy_gate_reconciles_against_host_deployed_commit():
    """Structural regression guard: the redeploy trigger must content-diff
    guardian paths against the host's reported deployed_commit and drive the
    decision through _guardian_redeploy_reason — never revert to the old
    pull-delta-only gate (OLD_COMMIT→HEAD) that stranded hosts on orphans."""
    fn = _extract_sync_function()
    assert 'diff --quiet "$HOST_DEPLOYED_COMMIT" HEAD -- $GUARDIAN_PATHS' in fn, (
        "redeploy decision must content-diff against the host's deployed_commit"
    )
    assert "_guardian_redeploy_reason" in fn, (
        "redeploy decision must route through the pure reason helper"
    )
    assert 'if [ -n "$_gv_reason" ]; then' in fn, (
        "redeploy must be gated on the helper's reason, not a raw pull-delta diff"
    )


def test_redeploy_sends_verified_form_with_legacy_fallback():
    """Structural regression guard (F.0): the sender must materialize the
    archive to a file, hash it, send the sha-checked ``redeploy <hash> <sha>``
    form, and KEEP a bare ``redeploy <hash>`` fallback so a mid-rollout old
    gateway (redeploy verb but no sha arg) still deploys."""
    fn = _extract_sync_function()
    # Archive materialized to a file (not piped) so it can be hashed + re-sent.
    assert 'git -C "$GENESIS_ROOT" archive HEAD' in fn
    assert '> "$GUARDIAN_ARCHIVE"' in fn, "archive must be written to a file, not piped"
    # Big-temp discipline: never the inherited TMPDIR (cc-tmp/tmpfs).
    assert '$HOME/tmp/guardian-deploy' in fn, "archive temp must route to ~/tmp"
    # Stream sha256 computed and sent as the 2nd redeploy arg.
    assert 'sha256sum "$GUARDIAN_ARCHIVE"' in fn
    assert 'redeploy $DEPLOY_HASH $GUARDIAN_ARCHIVE_SHA' in fn, (
        "must send the sha-checked redeploy form"
    )
    # Legacy 1-arg fallback preserved for old gateways.
    assert '"redeploy $DEPLOY_HASH"' in fn, (
        "must retain the bare redeploy <hash> fallback for old gateways"
    )
    # Temp archive cleaned up regardless of path.
    assert 'rm -f "$GUARDIAN_ARCHIVE"' in fn


def test_update_sh_parses_clean():
    """`bash -n` on the whole update.sh — cheap guard that the F.0 edits (and
    anything else) didn't introduce a syntax error the structural greps miss."""
    res = subprocess.run(["bash", "-n", str(UPDATE_SH)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
