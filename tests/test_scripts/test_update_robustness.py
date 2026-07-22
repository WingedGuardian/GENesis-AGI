"""Robustness-sweep locks for scripts/update.sh (deploy-audit P4a).

update.sh cannot be executed in CI (it stops services, does git operations, and
runs migrations), so these are extraction-style assertions against the REAL
shipped script text — anchored on actual command lines, not comment prose — plus
one functional test that runs the shipped conflict-context JSON builder against
hostile input to prove it emits valid JSON.

Findings locked here:
  #2  update_conflicts.json is built as VALID JSON (python json.dumps), not a
      shell heredoc that only escaped `"` (multi-line merge output → invalid).
  #3  network calls are bounded: git fetch (timeout), curl health (--max-time
      above the route's own budget), ssh legs (ServerAlive, not a blanket
      timeout that would kill a slow-but-alive redeploy).
  #8  the pre-update DB snapshot uses sqlite3 `.backup` (consistent), not `cp`
      of a live WAL database (torn copy).
  #9  a broken migrations module rolls back; only a genuinely-absent one skips.
  #10 the container-specs refresh surfaces the real error, not a blanket
      "no profile yet".
  #12 the fallback `kill -TERM` is guarded so a kill/exit race can't abort.
  #13 a container CC sync failure is recorded as a degraded subsystem.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
UPDATE_SH = REPO_ROOT / "scripts" / "update.sh"


@pytest.fixture(scope="module")
def text() -> str:
    return UPDATE_SH.read_text()


# ── #2 — conflict context is valid JSON ────────────────────────────────────
def test_conflict_json_built_with_python_not_sed(text: str) -> None:
    assert "print(json.dumps(data, indent=2))" in text, (
        "update_conflicts.json must be serialized with json.dumps, not a heredoc"
    )
    # The old buggy escaping (only `"` in merge_output) must be gone.
    assert "head -20 | sed 's/\"/\\\\\"/g'" not in text
    assert 'CONFLICT_JSON=$(echo "$CONFLICTED_FILES" | awk' not in text


def _extract_conflict_json_py(text: str) -> str:
    """Pull the shipped conflict-context builder's python body (the heredoc that
    emits conflicted_files) so we can run it in isolation."""
    m = re.search(r"<<'PYEOF'\n(.*?conflicted_files.*?)\nPYEOF", text, re.DOTALL)
    assert m, "conflict-context python heredoc not found"
    return m.group(1)


def test_conflict_json_is_valid_under_hostile_input(text: str) -> None:
    """A multi-line merge message, embedded quotes/backslashes, and a filename
    containing a quote must all round-trip as VALID JSON (the exact #2 bug)."""
    body = _extract_conflict_json_py(text)
    env = {
        "UC_OLD_TAG": "v1.0",
        "UC_OLD_COMMIT": "abc123",
        "UC_TARGET_TAG": "v1.1",
        "UC_TARGET_COMMIT": "def456",
        "UC_FILES": 'src/a.py\nsrc/b"quote.py',
        "UC_MERGE_OUTPUT": 'CONFLICT (content): line one "quoted"\nline two\\backslash',
    }
    result = subprocess.run(
        [sys.executable, "-c", body],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)  # must not raise
    assert data["conflicted_files"] == ["src/a.py", 'src/b"quote.py']
    assert "\n" in data["merge_output"]  # newline preserved, not corrupted
    assert data["old_tag"] == "v1.0"


# ── #3 — bounded network calls ─────────────────────────────────────────────
def test_git_fetch_is_timeout_bounded(text: str) -> None:
    assert 'timeout 120 git -C "$GENESIS_ROOT" fetch "$UPDATE_REMOTE" main' in text


def test_health_curls_have_max_time(text: str) -> None:
    health = "http://localhost:5000/api/genesis/health"
    bounded = text.count(f"curl -sf --max-time 20 {health}")
    assert bounded >= 2, "both health curls must be --max-time bounded"
    # No unbounded health curl may remain.
    assert re.search(rf"curl -sf {re.escape(health)}", text) is None


def test_ssh_legs_use_serveralive_not_blanket_timeout(text: str) -> None:
    # All four guardian ssh invocations bound a DEAD connection via ServerAlive.
    assert text.count("-o ServerAliveInterval=15 -o ServerAliveCountMax=4") == 4
    # A blanket `timeout N ssh` would wrongly kill a slow-but-alive redeploy.
    assert "timeout 60 ssh" not in text
    assert re.search(r"timeout \d+ ssh ", text) is None


# ── #8 — consistent DB snapshot ────────────────────────────────────────────
def test_db_snapshot_uses_backup_not_cp(text: str) -> None:
    assert 'sqlite3 "$DB_FILE" ".backup \'$DB_FILE.pre-update\'"' in text
    assert 'cp "$DB_FILE" "$DB_FILE.pre-update"' not in text


# ── #9 — broken-vs-absent migrations ───────────────────────────────────────
def test_migration_gate_distinguishes_absent_from_broken(text: str) -> None:
    assert 'importlib.util.find_spec("genesis.db.migrations")' in text
    assert "sys.exit(2)" in text  # genuinely absent → skip
    assert "broken migration" in text  # import failure → rollback reason
    # The old collapse-both-into-silent-skip guard is gone.
    assert (
        'if "$VENV_DIR/bin/python" -c "import genesis.db.migrations" 2>/dev/null; then' not in text
    )


# ── #10 — honest specs-refresh message ─────────────────────────────────────
def test_specs_refresh_surfaces_real_error(text: str) -> None:
    assert (
        '_specs_err=$("$VENV_DIR/bin/python" -m genesis.infra_profile --claude-md-block 2>&1)'
        in text
    )
    assert "${_specs_err:-no profile yet}" in text
    # The unconditional misleading skip message is gone.
    assert '|| echo "  Container specs refresh skipped (no profile yet)"' not in text


# ── #12 — kill race guarded ────────────────────────────────────────────────
def test_fallback_kill_term_is_guarded(text: str) -> None:
    assert 'kill -TERM "$pid" 2>/dev/null || true' in text


# ── #13 — container CC failure recorded as degraded ────────────────────────
def test_container_cc_failure_marks_degraded(text: str) -> None:
    assert "if ! cc_ensure_local; then" in text
    assert 'HOST_CC_DEGRADED="${HOST_CC_DEGRADED:+$HOST_CC_DEGRADED,}container_cc_sync"' in text
    assert "cc_ensure_local || true" not in text  # old swallow-and-forget gone
