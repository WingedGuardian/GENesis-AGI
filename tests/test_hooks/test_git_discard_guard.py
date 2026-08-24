"""Tests for git_discard_guard — recoverability-not-classification redesign.

Two claims, tested separately because they are separate mechanisms:
  * COARSE BLOCKS: closed-set literal-token verdicts (pure argv, no repo state).
  * SNAPSHOT-THEN-ALLOW: allowed checkout/restore/switch segments leave a
    ``git stash create`` recovery sha behind — including an ACTUAL byte-recovery
    test of the original incident (``git checkout <file>`` over unstaged edits).

Hermetic: each test builds a real throwaway git repo under tmp_path (git in a
pytest subprocess is fine — the CC Bash guards police scratch git in the Bash
TOOL, not pytest subprocesses).
"""

from __future__ import annotations

import importlib.util
import json
import os
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
    "GIT_CONFIG_GLOBAL": "/dev/null",  # hermetic — ignore the user's ~/.gitconfig
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    )
    return proc.stdout


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


def _blocks(cmd: str) -> bool:
    """True iff the coarse blocks would block *cmd* (pure argv — no repo)."""
    return bool(_gd._violations(cmd))


def _rows(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


# ── coarse blocks: reset ─────────────────────────────────────────────────────
def test_reset_hard_blocks():
    assert _blocks("git reset --hard")
    assert _blocks("git reset --hard HEAD~1")


def test_reset_merge_blocks():
    assert _blocks("git reset --merge")


def test_reset_keep_allows():
    # --keep ABORTS on local changes instead of overwriting them — safe.
    assert not _blocks("git reset --keep HEAD~1")


def test_reset_soft_mixed_allow():
    assert not _blocks("git reset --soft HEAD~1")
    assert not _blocks("git reset HEAD~1")
    assert not _blocks("git reset")


# ── coarse blocks: clean (exact-form dry-run whitelist) ──────────────────────
def test_clean_dry_run_forms_allow():
    assert not _blocks("git clean -n")
    assert not _blocks("git clean -n -d")
    assert not _blocks("git clean --dry-run -x")
    assert not _blocks("git clean -n -d -X")


def test_clean_force_forms_block():
    assert _blocks("git clean -f")
    assert _blocks("git clean -fd")
    assert _blocks("git clean -xf")
    assert _blocks("git clean -fdx")
    assert _blocks("git clean --force")


def test_clean_bare_blocks():
    # Not the exact dry-run shape → block (closed-set: only the whitelist allows).
    assert _blocks("git clean")


def test_clean_unknown_flag_blocks():
    # THE decision-test case: a flag we never modeled cannot flip a verdict to
    # allow — anything outside the whitelist blocks, by construction.
    assert _blocks("git clean --some-future-flag -n")
    assert _blocks("git clean -e pattern -n")  # -e's value ambiguity → block


def test_clean_interactive_blocks():
    assert _blocks("git clean -i")


def test_clean_with_path_operand_blocks():
    # Documented over-block: paths are outside the whitelist. Preview with
    # `git clean -nd` (no path) or use `# discard-override`.
    assert _blocks("git clean -n src/")


def test_clean_canonical_preview_clusters_allow():
    # -nd/-dn are explicit LITERAL whitelist members (the canonical preview
    # form) — allowed WITHOUT cluster decomposition.
    assert not _blocks("git clean -nd")
    assert not _blocks("git clean -dn")


def test_clean_other_clusters_block():
    # Any other cluster is outside the literal whitelist → over-block +
    # override. Deliberate: cluster decomposition is flag semantics, the tar
    # pit this redesign removed.
    assert _blocks("git clean -ndx")
    assert _blocks("git clean -nX")


# ── coarse blocks: checkout / switch force ───────────────────────────────────
def test_checkout_force_blocks():
    assert _blocks("git checkout --force main")
    assert _blocks("git checkout -f main")
    assert _blocks("git checkout -fb newbranch")


def test_switch_force_blocks():
    assert _blocks("git switch --force main")
    assert _blocks("git switch -f main")
    assert _blocks("git switch --discard-changes main")


def test_checkout_plain_forms_allow():
    assert not _blocks("git checkout feature")
    assert not _blocks("git checkout -b newbranch")
    assert not _blocks("git checkout tracked.py")
    assert not _blocks("git checkout -- tracked.py")
    assert not _blocks("git switch feature")
    assert not _blocks("git restore --staged tracked.py")
    assert not _blocks("git restore tracked.py")


# ── override + non-git ───────────────────────────────────────────────────────
def test_discard_override_allows_every_block_class():
    assert not _blocks("git reset --hard  # discard-override")
    assert not _blocks("git clean -fdx  # discard-override")
    assert not _blocks("git checkout -f main  # discard-override")


def test_unrelated_git_commands_ignored():
    assert not _blocks("git status")
    assert not _blocks("git log --oneline")
    assert not _blocks("git stash list")


# ── M1 regression: separated global value-flags must not mask the subcommand ──
def test_git_dir_global_flag_reset_hard_blocks():
    assert _blocks("git --git-dir /x reset --hard")


def test_namespace_global_flag_reset_hard_blocks():
    assert _blocks("git --namespace ns reset --hard")


def test_dash_c_value_named_clean_still_blocks_real_clean():
    # `git -C clean clean -f`: .index() anchors on the -C VALUE, making the
    # checked tail a SUPERSET — still blocks (over-block direction proof).
    assert _blocks("git -C clean clean -f")


# ── snapshot-then-allow ──────────────────────────────────────────────────────
def test_incident_checkout_discard_is_recoverable(repo, snap_log):
    """THE original incident, end-to-end: unstaged+staged edits, then
    `git checkout -- file` discards them — the snapshot recovers the BYTES."""
    (repo / "tracked.py").write_text("precious unstaged edit\n")
    (repo / "keep.py").write_text("precious staged edit\n")
    _git(repo, "add", "keep.py")

    payload = {"tool_input": {"command": "git checkout -- tracked.py"}, "cwd": str(repo)}
    _gd._record_snapshots("git checkout -- tracked.py", payload)

    rows = _rows(snap_log)
    assert len(rows) == 1
    sha = rows[0]["sha"]
    assert rows[0]["cwd"] == str(repo)

    # The discard actually happens (the guard ALLOWED it) — then simulate
    # TOTAL loss (hard reset wipes both files AND the staged split), so the
    # --cached assertions below can ONLY pass if --index actually restored
    # keep.py INTO the index (plain apply restores it unstaged — F4
    # discrimination) and the byte assertions prove full content recovery.
    _git(repo, "checkout", "--", "tracked.py")
    assert (repo / "tracked.py").read_text() == "orig\n"
    _git(repo, "reset", "--hard")
    assert (repo / "keep.py").read_text() == "keep\n"  # staged edit gone too

    _git(repo, "stash", "apply", "--index", sha)
    assert (repo / "tracked.py").read_text() == "precious unstaged edit\n"
    assert (repo / "keep.py").read_text() == "precious staged edit\n"
    staged = _git(repo, "diff", "--cached", "--name-only")
    assert "keep.py" in staged  # staged hunk restored AS staged
    assert "tracked.py" not in staged


def test_clean_tree_writes_no_snapshot(repo, snap_log):
    payload = {"tool_input": {"command": "git checkout feature"}, "cwd": str(repo)}
    _gd._record_snapshots("git checkout feature", payload)
    assert _rows(snap_log) == []


def test_non_repo_cwd_silent_allow_no_snapshot(tmp_path, snap_log):
    payload = {"cwd": str(tmp_path)}
    _gd._record_snapshots("git checkout foo", payload)
    assert _rows(snap_log) == []


def test_missing_cwd_dir_silent_allow(snap_log):
    _gd._record_snapshots("git checkout foo", {"cwd": "/nonexistent/dir/xyz"})
    assert _rows(snap_log) == []


def test_git_dash_C_snapshots_target_repo(repo, tmp_path, snap_log):
    (repo / "tracked.py").write_text("dirty\n")
    cmd = f"git -C {repo} checkout -- tracked.py"
    _gd._record_snapshots(cmd, {"cwd": str(tmp_path)})
    rows = _rows(snap_log)
    assert len(rows) == 1
    assert rows[0]["cwd"] == str(repo)


def test_one_snapshot_per_cwd_for_compound(repo, snap_log):
    (repo / "tracked.py").write_text("dirty\n")
    cmd = "git checkout -- tracked.py && git restore keep.py"
    _gd._record_snapshots(cmd, {"cwd": str(repo)})
    assert len(_rows(snap_log)) == 1  # same resolved cwd → one snapshot


def test_reset_snapshots_but_clean_never_does(repo, snap_log):
    # reset IS in the snapshot set (F3: an overridden reset --hard is the
    # sanctioned discard path and deserves a recovery sha); clean is NOT
    # (stash create never captures untracked files, which is all clean deletes).
    (repo / "tracked.py").write_text("dirty\n")
    _gd._record_snapshots("git clean -nd", {"cwd": str(repo)})
    assert _rows(snap_log) == []
    _gd._record_snapshots("git reset --soft HEAD", {"cwd": str(repo)})
    assert len(_rows(snap_log)) == 1


def test_overridden_reset_hard_is_recoverable(repo, snap_log):
    """F3 end-to-end: the SANCTIONED discard path leaves a recovery sha."""
    (repo / "tracked.py").write_text("precious\n")
    cmd = "git reset --hard  # discard-override"
    assert not _gd._violations(cmd)  # override waives the block
    _gd._record_snapshots(cmd, {"cwd": str(repo)})
    rows = _rows(snap_log)
    assert len(rows) == 1
    _git(repo, "reset", "--hard")
    assert (repo / "tracked.py").read_text() == "orig\n"
    _git(repo, "stash", "apply", rows[0]["sha"])
    assert (repo / "tracked.py").read_text() == "precious\n"


# ── F1/F2 regressions: resolution-shift and abbreviation under-blocks ────────
def test_unknown_value_flag_cannot_shift_verdict_to_allow():
    # F1 (executed by the adversarial review): --attr-source is a value-taking
    # global flag NOT in the tokenizer's skip-set — positional resolution would
    # return "HEAD" and skip the segment. Literal verb membership doesn't care.
    assert _blocks("git --attr-source HEAD reset --hard")
    assert _blocks("git --attr-source HEAD clean -f")
    assert _blocks("git --attr-source HEAD checkout -f main")


def test_long_flag_abbreviations_block():
    # F2: git accepts unambiguous long-flag prefixes; `git reset --har` DOES a
    # full hard reset. Closed prefix-set (len >= 4) blocks them.
    assert _blocks("git reset --har")
    assert _blocks("git reset --merg HEAD~1")
    assert _blocks("git checkout --forc main")
    assert _blocks("git switch --discard main")


def test_single_letter_abbreviations_block():
    # MEASURED on git 2.43 (Codex round-2 P1): `git reset --h` performs a full
    # hard reset and `git checkout --f` force-switches — single-letter long
    # prefixes are accepted by git, so the floor is len >= 3 (`--X`).
    assert _blocks("git reset --h")
    assert _blocks("git checkout --f main")
    assert _blocks("git switch --f main")


def test_recurse_submodules_blocks():
    # MEASURED: a superproject `git stash create` stores NOTHING from a dirty
    # submodule, so recursive restoration is unrecoverable — flag-form blocks
    # on all three verbs (+ abbreviations); config-form is a documented residual.
    assert _blocks("git checkout --recurse-submodules -- sm")
    assert _blocks("git restore --recurse-submodules sm")
    assert _blocks("git switch --recurse-submodules b2")
    assert _blocks("git checkout --recurse -- sm")  # abbreviation prefix
    # measured bypass (round-3 BLOCKER): optional-arg = spellings parse as
    # boolean-true recursion and were longer than the flag -> prefix miss
    assert _blocks("git checkout --recurse-submodules=yes main")
    assert _blocks("git checkout --recurse=yes main")
    assert _blocks("git -c submodule.recurse=true checkout main")
    assert not _blocks("git restore sm")  # plain restore stays snapshot-then-allow


def test_checkout_detach_abbreviation_allows():
    # --d on checkout uniquely abbreviates harmless --detach (F5: the
    # switch-only --discard-changes must not over-block it); on switch --d is
    # ambiguous so blocking is free.
    assert not _blocks("git checkout --d HEAD~1")
    assert _blocks("git switch --d b2")


def test_verdict_crash_propagates_for_fail_closed_conversion(monkeypatch):
    # F4: a crash in the verdict path must NOT be swallowed to exit 0 —
    # it propagates so run_guard converts it to a VISIBLE exit-2 block.
    monkeypatch.setattr(
        _gd, "read_payload", lambda: {"tool_input": {"command": "git reset --hard"}}
    )
    monkeypatch.setattr(
        _gd, "_coarse_violations", lambda cmd: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    with pytest.raises(RuntimeError):
        _gd.main()


# ── main() exit codes (payload-driven) ───────────────────────────────────────
def _main_with(monkeypatch, payload):
    monkeypatch.setattr(_gd, "read_payload", lambda: payload)
    return _gd.main()


def test_main_blocks_coarse_with_exit_2(repo, monkeypatch, snap_log):
    rc = _main_with(monkeypatch, {"tool_input": {"command": "git reset --hard"}, "cwd": str(repo)})
    assert rc == 2
    assert _rows(snap_log) == []  # blocked path never snapshots


def test_main_allows_checkout_with_snapshot(repo, monkeypatch, snap_log, capsys):
    (repo / "tracked.py").write_text("dirty\n")
    rc = _main_with(
        monkeypatch,
        {"tool_input": {"command": "git checkout -- tracked.py"}, "cwd": str(repo)},
    )
    assert rc == 0
    rows = _rows(snap_log)
    assert len(rows) == 1
    err = capsys.readouterr().err
    assert "git stash apply" in err
    assert rows[0]["sha"][:12] in err


def test_main_override_allows_and_still_snapshots(repo, monkeypatch, snap_log):
    # Override waives the BLOCK; the snapshot net still runs (costs nothing).
    (repo / "tracked.py").write_text("dirty\n")
    rc = _main_with(
        monkeypatch,
        {
            "tool_input": {"command": "git checkout -f feature  # discard-override"},
            "cwd": str(repo),
        },
    )
    assert rc == 0
    assert len(_rows(snap_log)) == 1


def test_main_ignores_non_git(monkeypatch):
    assert _main_with(monkeypatch, {"tool_input": {"command": "ls -la"}, "cwd": "/tmp"}) == 0


def test_main_fails_open_on_garbage_payload(monkeypatch):
    monkeypatch.setattr(_gd, "read_payload", lambda: (_ for _ in ()).throw(KeyError("x")))
    assert _gd.main() == 0


def test_relative_snapshot_log_override(repo, tmp_path, monkeypatch):
    # A relative GENESIS_DISCARD_SNAPSHOT_LOG has an empty dirname —
    # makedirs("") used to raise and silently drop the row (Codex round-2 P2).
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GENESIS_DISCARD_SNAPSHOT_LOG", "rel_snapshots.jsonl")
    (repo / "tracked.py").write_text("dirty\n")
    _gd._record_snapshots("git checkout -- tracked.py", {"cwd": str(repo)})
    assert (tmp_path / "rel_snapshots.jsonl").exists()
    assert len(_rows(tmp_path / "rel_snapshots.jsonl")) == 1


def test_log_trim_keeps_newest_half(tmp_path, monkeypatch, repo):
    log = tmp_path / "snap.jsonl"
    monkeypatch.setenv("GENESIS_DISCARD_SNAPSHOT_LOG", str(log))
    # Pre-fill past the cap with old rows, then snapshot once: the trim keeps
    # the newest half and the new row survives (atomic replace under flock).
    monkeypatch.setattr(_gd, "_SNAPSHOT_LOG_MAX_BYTES", 2000)
    log.write_text("\n".join(f'{{"ts":"old","sha":"{i:040d}"}}' for i in range(50)) + "\n")
    (repo / "tracked.py").write_text("dirty\n")
    _gd._record_snapshots("git checkout -- tracked.py", {"cwd": str(repo)})
    rows = _rows(log)
    assert rows, "log must survive the trim"
    assert rows[-1]["cwd"] == str(repo)  # the just-appended row is retained
    # the trim must have actually FIRED (a crashed trim swallowed by the
    # OSError handler would leave all 51 rows and still pass the above — F3)
    assert len(rows) < 50
