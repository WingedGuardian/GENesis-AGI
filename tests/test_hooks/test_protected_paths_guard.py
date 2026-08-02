"""Tests for scripts/hooks/protected_paths_guard.py — operand-aware rewrite.

The old guard was `if path in cmd` (raw substring): it blocked any command
MENTIONING a protected path near any rm, and blocked deleting files INSIDE a
protected dir — both live false positives (2026-07/08). The rewrite parses rm/
rmdir operands via shell_parse and blocks only real deletion targets.

Every test runs the guard as a subprocess with a SYNTHETIC $HOME (tmp_path),
so the suite is install-agnostic and can never touch real data.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parent.parent.parent
_SCRIPT = _WORKTREE / "scripts" / "hooks" / "protected_paths_guard.py"
_PYTHON = sys.executable


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    return tmp_path / "home"


def _run(cmd: str, home: Path, cwd: str | None = None) -> subprocess.CompletedProcess:
    payload: dict = {"tool_input": {"command": cmd}, "tool_name": "Bash"}
    if cwd is not None:
        payload["cwd"] = cwd
    env = dict(os.environ)
    env["HOME"] = str(home)
    return subprocess.run(
        [_PYTHON, str(_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
    )


H = "$HOME"  # shorthand used inside test commands (expanded by the guard)


class TestFalsePositiveRegressions:
    """These commands were BLOCKED by the old substring guard — live FPs."""

    def test_mention_only_is_allowed(self, fake_home):
        """rm of an unrelated file + a protected path merely MENTIONED."""
        r = _run(f"rm scratch.txt; cat {H}/backups/notes", fake_home)
        assert r.returncode == 0, r.stderr

    def test_file_inside_protected_dir_is_allowed(self, fake_home):
        """Deleting a specific file INSIDE a protected dir is legal (docstring)."""
        r = _run(f"rm {H}/genesis/data/old.log", fake_home)
        assert r.returncode == 0, r.stderr

    def test_echo_containing_path_is_allowed(self, fake_home):
        """The string appearing as DATA (echo/grep arg) next to an rm."""
        r = _run(f"rm /tmp/x.txt && echo 'see {H}/genesis/data for the DB'", fake_home)
        assert r.returncode == 0, r.stderr

    def test_non_rm_command_never_blocks(self, fake_home):
        r = _run(f"tar -cf /tmp/b.tar {H}/backups", fake_home)
        assert r.returncode == 0, r.stderr


class TestProtectedDirBlocks:
    def test_dir_itself(self, fake_home):
        r = _run(f"rm -rf {H}/genesis/data", fake_home)
        assert r.returncode == 2
        assert "BLOCKED" in r.stderr

    def test_tilde_form(self, fake_home):
        r = _run("rm -rf ~/genesis/data", fake_home)
        assert r.returncode == 2

    def test_ancestor(self, fake_home):
        """Deleting the parent removes the protected dir as a side effect."""
        r = _run(f"rm -rf {H}/genesis", fake_home)
        assert r.returncode == 2
        assert "ancestor" in r.stderr

    def test_rmdir_variant(self, fake_home):
        r = _run(f"rmdir {H}/snapshots", fake_home)
        assert r.returncode == 2

    def test_transcripts_dir(self, fake_home):
        r = _run(f"rm -rf {H}/.claude/projects", fake_home)
        assert r.returncode == 2

    def test_after_double_dash(self, fake_home):
        r = _run(f"rm -rf -- {H}/genesis/data", fake_home)
        assert r.returncode == 2

    def test_with_redirect(self, fake_home):
        """A glued redirect token must not shield the real operand."""
        r = _run(f"rm -rf {H}/genesis/data 2>/dev/null", fake_home)
        assert r.returncode == 2

    def test_nested_bash_c(self, fake_home):
        r = _run(f"bash -c 'rm -rf {H}/genesis/data'", fake_home)
        assert r.returncode == 2

    def test_chained_after_safe_command(self, fake_home):
        r = _run(f"ls /tmp && rm -rf {H}/backups", fake_home)
        assert r.returncode == 2


class TestGlobBlocks:
    """Globs that could wipe protected data (red-team findings 2 + 11)."""

    def test_full_contents_glob(self, fake_home):
        r = _run(f"rm -rf {H}/genesis/data/*", fake_home)
        assert r.returncode == 2

    def test_partial_glob_under_dir(self, fake_home):
        """*.db under the data dir wipes the databases while dodging 'the dir
        itself' — must block."""
        r = _run(f"rm -f {H}/genesis/data/*.db", fake_home)
        assert r.returncode == 2

    def test_sibling_prefix_glob(self, fake_home):
        """~/genesis/da* can expand to ~/genesis/data."""
        r = _run(f"rm -rf {H}/genesis/da*", fake_home)
        assert r.returncode == 2

    def test_unrelated_glob_allowed(self, fake_home):
        r = _run(f"rm -f {H}/tmp/build/*.o", fake_home)
        assert r.returncode == 0, r.stderr


class TestProtectedFiles:
    """The production DB + WAL/SHM sidecars are protected even though they
    live inside a dir whose OTHER files are deletable."""

    def test_genesis_db(self, fake_home):
        r = _run(f"rm {H}/genesis/data/genesis.db", fake_home)
        assert r.returncode == 2
        assert "genesis.db" in r.stderr

    def test_wal_sidecar(self, fake_home):
        r = _run(f"rm -f {H}/genesis/data/genesis.db-wal", fake_home)
        assert r.returncode == 2

    def test_shm_sidecar(self, fake_home):
        r = _run(f"rm -f {H}/genesis/data/genesis.db-shm", fake_home)
        assert r.returncode == 2

    def test_other_file_in_same_dir_allowed(self, fake_home):
        r = _run(f"rm {H}/genesis/data/export-2026.json", fake_home)
        assert r.returncode == 0, r.stderr


class TestRelativeOperands:
    def test_relative_dir_resolves_against_cwd(self, fake_home):
        """cd is in the payload: `rm -rf data` from ~/genesis targets the DB dir."""
        r = _run("rm -rf data", fake_home, cwd=str(fake_home / "genesis"))
        assert r.returncode == 2

    def test_relative_file_inside_allowed(self, fake_home):
        r = _run("rm old.log", fake_home, cwd=str(fake_home / "genesis" / "data"))
        assert r.returncode == 0, r.stderr

    def test_relative_without_cwd_falls_back_to_substring(self, fake_home):
        """Unresolvable relative operand + a protected mention → conservative
        substring fallback blocks (never weaker than the old guard)."""
        r = _run(f"rm -rf data  # cleanup of {H}/genesis/data", fake_home)
        assert r.returncode == 2

    def test_relative_without_cwd_and_no_mention_allowed(self, fake_home):
        r = _run("rm -rf build", fake_home)
        assert r.returncode == 0, r.stderr

    def test_dotdot_traversal_to_protected(self, fake_home):
        """normpath collapses interior '..' — data/../data is still data."""
        r = _run(f"rm -rf {H}/genesis/data/../data", fake_home)
        assert r.returncode == 2


class TestUnparseableFallback:
    def test_unclosed_quote_with_protected_mention_blocks(self, fake_home):
        r = _run(f'rm -rf "{H}/backups', fake_home)
        assert r.returncode == 2

    def test_unclosed_quote_without_mention_allows(self, fake_home):
        r = _run('rm -rf "/tmp/somewhere', fake_home)
        assert r.returncode == 0, r.stderr


class TestBraceExpansion:
    """REGRESSION (adversarial review, 2026-08-01): bash brace-expands an
    unquoted operand BEFORE rm runs, so `rm -rf ~/genesis/{data,logs}` deletes
    the protected DB dir — but the guard saw one opaque, non-glob, depth-4 token
    and allowed it. Each real expansion must now be checked."""

    def test_comma_brace_hits_protected_dir(self, fake_home):
        r = _run(f"rm -rf {H}/genesis/{{data,logs}}", fake_home)
        assert r.returncode == 2
        assert "genesis/data" in r.stderr

    def test_trailing_comma_expands_to_parent(self, fake_home):
        """`{data,}` → data AND '' → the parent dir (an ancestor) blocks."""
        r = _run(f"rm -rf {H}/genesis/{{data,}}", fake_home)
        assert r.returncode == 2

    def test_glob_under_expanded_protected(self, fake_home):
        r = _run(f"rm -rf {H}/genesis/{{data,logs}}/*", fake_home)
        assert r.returncode == 2

    def test_nested_brace_reaches_protected(self, fake_home):
        r = _run(f"rm -rf {H}/genesis/{{da{{ta,}},logs}}", fake_home)
        assert r.returncode == 2

    def test_unrelated_brace_allowed(self, fake_home):
        r = _run(f"rm -rf {H}/tmp/build/{{a,b,c}}", fake_home)
        assert r.returncode == 0, r.stderr

    def test_brace_bomb_fails_closed(self, fake_home):
        """A combinatorial blow-up raises → run_guard fails CLOSED (blocks)."""
        bomb = "rm -rf " + "".join("{a,b}" for _ in range(20)) + "/x"
        r = _run(bomb, fake_home)
        assert r.returncode == 2


class TestPayloadEdges:
    def test_empty_command(self, fake_home):
        r = _run("", fake_home)
        assert r.returncode == 0

    def test_no_rm_fast_path(self, fake_home):
        r = _run("git status", fake_home)
        assert r.returncode == 0
        assert r.stderr == ""

    def test_malformed_payload_fails_open(self, fake_home):
        env = dict(os.environ)
        env["HOME"] = str(fake_home)
        r = subprocess.run(
            [_PYTHON, str(_SCRIPT)],
            input="not json {{{",
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
        assert r.returncode == 0
