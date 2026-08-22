"""Tests for scripts/hooks/prepare-commit-msg (provenance trailers).

Both installs commit under one git identity, so the trailers are the only
durable provenance markers on a merged PR (squash bodies keep commit
messages; PR commits stay queryable via the API). Two independent trailers:

`Install: <id8>` (per-install pseudonym):
1. Appended exactly once (idempotent on --amend re-runs).
2. Resolution order: GENESIS_INSTALL_ID env > install.json (GENESIS_HOME
   honored) > silent no-op — identity must NEVER block a commit.

`Genesis-Session: <id8>` (CC session id → cc_sessions.topic, privately):
4. Appended exactly once from a session-id env var.
5. Resolution order: GENESIS_SESSION_ID > CLAUDE_CODE_SESSION_ID >
   CLAUDE_SESSION_ID > silent no-op.
6. Value is a bare 8-hex prefix (opaque; no path/IP/email/name shape, so it
   clears the leak gates); a non-hex id is skipped, never mis-stamped.

Both:
3. Merge commits are left unstamped.
7. The two trailers are independent — one id missing skips only its own.
"""

import json
import subprocess
from pathlib import Path

HOOK = Path(__file__).resolve().parents[2] / "scripts" / "hooks" / "prepare-commit-msg"


def _run(
    msg_file: Path,
    source: str = "",
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Invoke the hook the way git does: <msg-file> [<source>]."""
    args = ["bash", str(HOOK), str(msg_file)]
    if source:
        args.append(source)
    # Minimal env: the hook must not depend on the caller's HOME state.
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(msg_file.parent)}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(args, capture_output=True, text=True, env=env)


def test_appends_trailer_from_env(tmp_path):
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("feat(x): subject\n\nBody.\n")
    proc = _run(msg, env_extra={"GENESIS_INSTALL_ID": "abc12345"})
    assert proc.returncode == 0
    assert "Install: abc12345" in msg.read_text()


def test_idempotent_on_rerun(tmp_path):
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("feat(x): subject\n")
    _run(msg, env_extra={"GENESIS_INSTALL_ID": "abc12345"})
    first = msg.read_text()
    _run(msg, env_extra={"GENESIS_INSTALL_ID": "abc12345"})
    assert msg.read_text() == first
    assert msg.read_text().count("Install:") == 1


def test_merge_commits_left_unstamped(tmp_path):
    msg = tmp_path / "MERGE_MSG"
    original = "Merge branch 'main' of example into main\n"
    msg.write_text(original)
    proc = _run(msg, source="merge", env_extra={"GENESIS_INSTALL_ID": "abc12345"})
    assert proc.returncode == 0
    assert msg.read_text() == original


def test_no_identity_is_silent_noop(tmp_path):
    msg = tmp_path / "COMMIT_EDITMSG"
    original = "fix(y): no identity available\n"
    msg.write_text(original)
    proc = _run(msg)  # no env id, no install.json under HOME
    assert proc.returncode == 0
    assert msg.read_text() == original


def test_reads_install_json_via_genesis_home(tmp_path):
    ghome = tmp_path / "ghome"
    ghome.mkdir()
    (ghome / "install.json").write_text(
        json.dumps(
            {
                "install_id": "deadbeef-1234-5678-9abc-def012345678",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        )
    )
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("chore: from install.json\n")
    proc = _run(msg, env_extra={"GENESIS_HOME": str(ghome)})
    assert proc.returncode == 0
    assert "Install: deadbeef" in msg.read_text()


def test_env_wins_over_install_json(tmp_path):
    ghome = tmp_path / "ghome"
    ghome.mkdir()
    (ghome / "install.json").write_text(
        json.dumps(
            {
                "install_id": "deadbeef-1234-5678-9abc-def012345678",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        )
    )
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("chore: env precedence\n")
    _run(
        msg,
        env_extra={"GENESIS_HOME": str(ghome), "GENESIS_INSTALL_ID": "envwins1"},
    )
    body = msg.read_text()
    assert "Install: envwins1" in body
    assert "deadbeef" not in body


# --- Genesis-Session: trailer -------------------------------------------------

_SID = "5e551011-0000-4000-8000-000000000000"  # synthetic UUID-shaped session id


def test_session_trailer_from_foreground_env(tmp_path):
    """CLAUDE_CODE_SESSION_ID (the foreground CC var) stamps the 8-hex prefix."""
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("feat(x): subject\n")
    proc = _run(msg, env_extra={"CLAUDE_CODE_SESSION_ID": _SID})
    assert proc.returncode == 0
    assert "Genesis-Session: 5e551011" in msg.read_text()


def test_session_trailer_env_precedence(tmp_path):
    """GENESIS_SESSION_ID (dispatched sessions) wins over CLAUDE_CODE_SESSION_ID."""
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("fix(y): subject\n")
    _run(
        msg,
        env_extra={
            "GENESIS_SESSION_ID": "aaaa1111-2222-3333-4444-555566667777",
            "CLAUDE_CODE_SESSION_ID": _SID,
        },
    )
    body = msg.read_text()
    assert "Genesis-Session: aaaa1111" in body
    assert "5e551011" not in body


def test_session_trailer_idempotent(tmp_path):
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("feat(x): subject\n")
    _run(msg, env_extra={"CLAUDE_CODE_SESSION_ID": _SID})
    first = msg.read_text()
    _run(msg, env_extra={"CLAUDE_CODE_SESSION_ID": _SID})
    assert msg.read_text() == first
    assert msg.read_text().count("Genesis-Session:") == 1


def test_no_session_env_omits_trailer(tmp_path):
    """No session env: Install still stamps, but no Genesis-Session trailer."""
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("chore: no session\n")
    proc = _run(msg, env_extra={"GENESIS_INSTALL_ID": "abc12345"})
    body = msg.read_text()
    assert proc.returncode == 0
    assert "Install: abc12345" in body
    assert "Genesis-Session:" not in body


def test_non_hex_session_id_skipped(tmp_path):
    """A session id not starting with 8 hex is skipped, never mis-stamped."""
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("feat(x): subject\n")
    proc = _run(msg, env_extra={"CLAUDE_CODE_SESSION_ID": "not-a-hex-value"})
    assert proc.returncode == 0
    assert "Genesis-Session:" not in msg.read_text()


def test_both_trailers_coexist(tmp_path):
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("feat(x): subject\n")
    _run(
        msg,
        env_extra={"GENESIS_INSTALL_ID": "abc12345", "CLAUDE_CODE_SESSION_ID": _SID},
    )
    body = msg.read_text()
    assert "Install: abc12345" in body
    assert "Genesis-Session: 5e551011" in body


def test_merge_leaves_session_trailer_off(tmp_path):
    msg = tmp_path / "MERGE_MSG"
    original = "Merge branch 'main'\n"
    msg.write_text(original)
    proc = _run(msg, source="merge", env_extra={"CLAUDE_CODE_SESSION_ID": _SID})
    assert proc.returncode == 0
    assert msg.read_text() == original


def test_session_trailer_value_is_bare_hex(tmp_path):
    """The stamped value must be a bare 8-hex token (sanitizer-safe shape)."""
    import re

    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("feat(x): subject\n")
    _run(msg, env_extra={"CLAUDE_CODE_SESSION_ID": _SID})
    line = next(ln for ln in msg.read_text().splitlines() if ln.startswith("Genesis-Session:"))
    value = line.split(":", 1)[1].strip()
    assert re.fullmatch(r"[0-9a-f]{8}", value), value
