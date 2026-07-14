"""Tests for scripts/hooks/prepare-commit-msg (install-identity trailer).

Both installs commit under one git identity, so the trailer is the only
durable per-machine provenance marker on a merged PR (squash bodies keep
commit messages; PR commits stay queryable via the API). Invariants:

1. Appends `Install: <id8>` exactly once (idempotent on --amend re-runs).
2. Resolution order: GENESIS_INSTALL_ID env > install.json (GENESIS_HOME
   honored) > silent no-op — identity must NEVER block a commit.
3. Merge commits are left unstamped.
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
