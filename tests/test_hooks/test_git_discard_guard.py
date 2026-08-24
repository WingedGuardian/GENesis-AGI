"""Tests for git_discard_guard — the RECOVERY net + the one clean block
(2026-08-24 redesign).

For the RECOVERABLE verbs (checkout/restore/switch/reset) the guard does not
BLOCK — deciding destructiveness from argv is an open-set parser problem, so it
instead `git stash create`-snapshots the worktree+index first and logs the sha,
making an overwrite undoable; it exits 0 for those.

``git clean`` is the EXCEPTION: `git stash create` cannot capture untracked
files (all clean deletes), so the snapshot net gives clean ZERO protection and
the guard keeps a real BLOCK — a CLOSED-SET whitelist (allow only exact dry-run
forms; block the open complement) that a false-ALLOW cannot penetrate. It exits
2 on a non-dry-run clean (unless `# discard-override`).

Hermetic: each test builds a real throwaway git repo under tmp_path.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parent.parent.parent
_HOOKS = _WORKTREE / "scripts" / "hooks"
_spec = importlib.util.spec_from_file_location("git_discard_guard", _HOOKS / "git_discard_guard.py")
_gd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_gd)

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    (r / "tracked.py").write_text("orig\n")
    (r / "keep.py").write_text("keep\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    _git(r, "branch", "feature")
    return r


@pytest.fixture
def snap_log(tmp_path: Path, monkeypatch) -> Path:
    log = tmp_path / "snapshots.jsonl"
    monkeypatch.setenv("GENESIS_DISCARD_SNAPSHOT_LOG", str(log))
    return log


def _rows(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


# ── the guard never blocks the RECOVERABLE verbs ─────────────────────────────
@pytest.mark.parametrize(
    "cmd",
    [
        "git reset --hard",  # recoverable via the snapshot net -> not blocked here
        "git checkout -f main",
        "git checkout tracked.py",
        "git switch --discard-changes main",
        "git status",
    ],
)
def test_main_never_blocks_recoverable_verbs(cmd, repo, monkeypatch, snap_log):
    monkeypatch.setattr(
        _gd, "read_payload", lambda: {"tool_input": {"command": cmd}, "cwd": str(repo)}
    )
    assert _gd.main() == 0


def test_main_fails_open_on_garbage(monkeypatch):
    monkeypatch.setattr(_gd, "read_payload", lambda: (_ for _ in ()).throw(KeyError("x")))
    assert _gd.main() == 0


# ── the ONE block: a non-dry-run `git clean` (unrecoverable verb) ─────────────
@pytest.mark.parametrize(
    "cmd",
    [
        "git clean -f",
        "git clean -fd",
        "git clean -fdx",
        "git clean --force",
        "git clean -xf",
        "git clean -x -f",
        "git clean",  # bare: requireForce may still act on a config; over-block
        "git clean -d",  # no dry-run token present
        "git clean -x",
        "git clean -f .",  # path argument
        "git clean -f src/",
        "git clean -f -e keepme",  # exclude flag
        "git clean -nf",  # dry-run CLUSTER not a literal member -> over-block (safe)
        "git clean -n src/",  # dry-run WITH a path -> superset kicks it out (safe over-block)
        "git clean -f -e -n",  # exotic: exclude VALUE `-n` — shell floor false-allows, guard BLOCKS
        "git clean -f -- -nine",  # exotic: `-n`-looking pathspec after `--` — guard BLOCKS
        "git -C /tmp clean -f",  # global -C before the verb
        "git clean -nd && git clean -f",  # second segment is a real clean
    ],
)
def test_clean_non_dry_run_blocks(cmd, monkeypatch):
    monkeypatch.setattr(
        _gd, "read_payload", lambda: {"tool_input": {"command": cmd}, "cwd": "/tmp"}
    )
    assert _gd.main() == 2


@pytest.mark.parametrize(
    "cmd",
    [
        "git clean -n",
        "git clean -nd",
        "git clean -dn",
        "git clean --dry-run",
        "git clean -f  # discard-override",  # sanctioned escape
        "git status",  # not a clean at all
        # A quoted multiword commit message that merely CONTAINS the word clean is
        # one shlex token (`clean up the repo`), never the bare `clean` verb -> safe.
        'git commit -m "clean up the repo"',
    ],
)
def test_clean_dry_run_and_override_allowed(cmd, monkeypatch):
    monkeypatch.setattr(
        _gd, "read_payload", lambda: {"tool_input": {"command": cmd}, "cwd": "/tmp"}
    )
    assert _gd.main() == 0


def test_bare_clean_token_over_blocks_by_design(monkeypatch):
    """KNOWN, ACCEPTED over-block: literal-membership verb detection (chosen over
    open-set positional resolution) means a BARE `clean` argv token — e.g. an
    unquoted `git commit -m clean` — trips the block. This is the safe direction
    (over-block, with `# discard-override` as the escape); the alternative
    (skipping git's open set of value-taking global flags to resolve the true
    subcommand) is the argv tar pit this design refuses. A quoted message is
    unaffected (see above)."""
    monkeypatch.setattr(
        _gd,
        "read_payload",
        lambda: {"tool_input": {"command": "git commit -m clean"}, "cwd": "/tmp"},
    )
    assert _gd.main() == 2


# ── snapshot-then-allow: the original incident is recoverable ────────────────
def test_incident_checkout_discard_is_recoverable(repo, snap_log):
    """Unstaged+staged edits, then `git checkout -- file` discards them — the
    snapshot recovers the bytes AND the staged/unstaged boundary via --index."""
    (repo / "tracked.py").write_text("precious unstaged edit\n")
    (repo / "keep.py").write_text("precious staged edit\n")
    _git(repo, "add", "keep.py")

    _gd._record_snapshots("git checkout -- tracked.py", {"cwd": str(repo)})
    rows = _rows(snap_log)
    assert len(rows) == 1
    sha = rows[0]["sha"]

    # simulate TOTAL loss so the --cached assertions discriminate --index
    _git(repo, "checkout", "--", "tracked.py")
    _git(repo, "reset", "--hard")
    assert (repo / "tracked.py").read_text() == "orig\n"
    assert (repo / "keep.py").read_text() == "keep\n"

    _git(repo, "stash", "apply", "--index", sha)
    assert (repo / "tracked.py").read_text() == "precious unstaged edit\n"
    assert (repo / "keep.py").read_text() == "precious staged edit\n"
    staged = _git(repo, "diff", "--cached", "--name-only")
    assert "keep.py" in staged and "tracked.py" not in staged


def test_reset_hard_is_snapshotted(repo, snap_log):
    # reset --hard reaches the snapshot net only when it BYPASSED the shell-layer
    # block (or is otherwise allowed); the net recovers what it destroys.
    (repo / "tracked.py").write_text("dirty\n")
    _gd._record_snapshots("git reset --hard", {"cwd": str(repo)})
    rows = _rows(snap_log)
    assert len(rows) == 1
    _git(repo, "reset", "--hard")
    _git(repo, "stash", "apply", rows[0]["sha"])
    assert (repo / "tracked.py").read_text() == "dirty\n"


def test_backslash_newline_reset_still_snapshots(repo, snap_log):
    # `git reset \<newline> --hard` — the tokenizer splits at the escaped
    # newline, so the SUBSTRING block misses it; the verb-triggered snapshot
    # still fires (the `reset` verb survives the split) -> loss is recoverable,
    # not silent. This is why the recovery net de-fangs the parser gaps.
    (repo / "tracked.py").write_text("dirty\n")
    _gd._record_snapshots("git reset \\\n --hard", {"cwd": str(repo)})
    assert len(_rows(snap_log)) == 1


# ── snapshot misses degrade to status quo (never block, never lie) ───────────
def test_clean_tree_no_snapshot(repo, snap_log):
    _gd._record_snapshots("git checkout feature", {"cwd": str(repo)})
    assert _rows(snap_log) == []


def test_non_repo_and_missing_cwd_silent(tmp_path, snap_log):
    _gd._record_snapshots("git checkout foo", {"cwd": str(tmp_path)})
    _gd._record_snapshots("git checkout foo", {"cwd": "/nonexistent/xyz"})
    assert _rows(snap_log) == []


def test_git_dash_C_snapshots_target_repo(repo, tmp_path, snap_log):
    (repo / "tracked.py").write_text("dirty\n")
    _gd._record_snapshots(f"git -C {repo} checkout -- tracked.py", {"cwd": str(tmp_path)})
    rows = _rows(snap_log)
    assert len(rows) == 1 and rows[0]["cwd"] == str(repo)


def test_one_snapshot_per_cwd(repo, snap_log):
    (repo / "tracked.py").write_text("dirty\n")
    _gd._record_snapshots("git checkout -- tracked.py && git restore keep.py", {"cwd": str(repo)})
    assert len(_rows(snap_log)) == 1


def test_clean_verb_never_snapshots(repo, snap_log):
    # clean is NOT in the snapshot set — stash create can't capture untracked.
    (repo / "junk.tmp").write_text("junk\n")
    _gd._record_snapshots("git clean -f", {"cwd": str(repo)})
    assert _rows(snap_log) == []


# ── the log is safe: metadata only, own-user only ────────────────────────────
def test_log_row_has_no_command(repo, snap_log):
    # the Bash payload can carry credentials — the row must NOT echo it.
    (repo / "tracked.py").write_text("dirty\n")
    secret = "curl -H 'Authorization: Bearer SECRET-TOKEN-XYZ' && git checkout -- tracked.py"
    _gd._record_snapshots(secret, {"cwd": str(repo)})
    row = _rows(snap_log)[0]
    assert set(row) == {"ts", "cwd", "sha"}
    assert "SECRET-TOKEN-XYZ" not in json.dumps(row)
    assert "SECRET-TOKEN-XYZ" not in snap_log.read_text()


def test_log_and_lock_are_own_user_only(repo, snap_log):
    (repo / "tracked.py").write_text("dirty\n")
    _gd._record_snapshots("git checkout -- tracked.py", {"cwd": str(repo)})
    assert stat.S_IMODE(snap_log.stat().st_mode) == 0o600
    lock = Path(str(snap_log) + ".lock")
    assert lock.exists() and stat.S_IMODE(lock.stat().st_mode) == 0o600


def test_relative_log_override(repo, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GENESIS_DISCARD_SNAPSHOT_LOG", "rel.jsonl")
    (repo / "tracked.py").write_text("dirty\n")
    _gd._record_snapshots("git checkout -- tracked.py", {"cwd": str(repo)})
    assert len(_rows(tmp_path / "rel.jsonl")) == 1


def test_log_trim_keeps_newest_half(repo, tmp_path, monkeypatch):
    log = tmp_path / "snap.jsonl"
    monkeypatch.setenv("GENESIS_DISCARD_SNAPSHOT_LOG", str(log))
    monkeypatch.setattr(_gd, "_SNAPSHOT_LOG_MAX_BYTES", 2000)
    log.write_text("\n".join(f'{{"ts":"old","sha":"{i:040d}"}}' for i in range(50)) + "\n")
    (repo / "tracked.py").write_text("dirty\n")
    _gd._record_snapshots("git checkout -- tracked.py", {"cwd": str(repo)})
    rows = _rows(log)
    assert rows and rows[-1]["cwd"] == str(repo)
    assert len(rows) < 50  # the trim actually fired


def test_stderr_recovery_note(repo, snap_log, capsys):
    (repo / "tracked.py").write_text("dirty\n")
    _gd._record_snapshots("git checkout -- tracked.py", {"cwd": str(repo)})
    err = capsys.readouterr().err
    assert "git stash apply --index" in err
    assert _rows(snap_log)[0]["sha"][:12] in err
