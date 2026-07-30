"""Tests for scripts/hooks/credential_surface_hook.py (PreToolUse Bash).

The hook's job is to POINT at stored credentials, never reveal them. These lock
in the privacy contract post-fix: reference matches surface by CONCEPT name only
(never the body's raw ``Value:`` secret), the model is directed to
``reference_lookup``, and output travels on the PreToolUse
``hookSpecificOutput.additionalContext`` channel (not plain stdout, which CC
drops on PreToolUse).

A throwaway SQLite DB (pointed at via ``GENESIS_DB_PATH``) stands in for the
reference store, so the live store is never touched.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent.parent
HOOK = REPO_DIR / "scripts" / "hooks" / "credential_surface_hook.py"

_SECRET_BODY = "Value: supersecretpassword123\nHost: 192.0.2.10"
_CONCEPT = "example-service SSH access"


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE knowledge_units (concept TEXT, body TEXT, project_type TEXT)")
    conn.execute(
        "INSERT INTO knowledge_units (concept, body, project_type) VALUES (?, ?, 'reference')",
        (_CONCEPT, _SECRET_BODY),
    )
    conn.commit()
    conn.close()


def _run(command: str, db: Path | None) -> subprocess.CompletedProcess:
    env = {**os.environ}
    env.pop("GENESIS_CC_SESSION", None)
    if db is not None:
        env["GENESIS_DB_PATH"] = str(db)
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "session_id": "t"}
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _context(proc: subprocess.CompletedProcess) -> str | None:
    if not proc.stdout.strip():
        return None
    return json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]


def test_points_at_concept_never_body(tmp_path: Path):
    db = tmp_path / "ref.db"
    _make_db(db)
    proc = _run("ssh deploy@192.0.2.10", db)
    ctx = _context(proc)
    assert ctx is not None
    assert _CONCEPT in ctx  # the label surfaces
    assert "reference_lookup" in ctx  # points at the masked retrieval tool
    assert "supersecretpassword123" not in ctx  # the body secret NEVER surfaces
    assert "Value:" not in ctx


def test_output_uses_additionalcontext_channel(tmp_path: Path):
    db = tmp_path / "ref.db"
    _make_db(db)
    proc = _run("scp file deploy@192.0.2.10:/tmp", db)
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "additionalContext" in out["hookSpecificOutput"]


def test_no_match_no_output(tmp_path: Path):
    db = tmp_path / "ref.db"
    _make_db(db)
    proc = _run("ssh nobody@10.10.10.10", db)  # no stored target
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_non_auth_command_ignored(tmp_path: Path):
    db = tmp_path / "ref.db"
    _make_db(db)
    proc = _run("ls -la /home", db)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_missing_db_fails_open(tmp_path: Path):
    proc = _run("ssh deploy@192.0.2.10", tmp_path / "does_not_exist.db")
    assert proc.returncode == 0
    # No reference store → no reference pointer (topology file may or may not
    # exist on the box; either way, never a crash and never a raw secret).
    assert "supersecretpassword123" not in (proc.stdout or "")
