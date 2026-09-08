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


# ── fail-CLOSED on a parser CRASH (Codex round-5 P1 + the security CRITICAL).
#    ROBUST-BY-CONSTRUCTION: no bespoke coarse re-parse on the crash path (that
#    hand-rolled floor drew a cross-segment-decoy bypass). We are already inside
#    `"clean" in cmd`, so a crash on a clean-MENTIONING command fails CLOSED
#    unconditionally and asks the user to simplify. The direct settings.json wiring
#    has no shell floor behind it, so the guard must self-block here.
def test_clean_parse_crash_blocks(monkeypatch):
    monkeypatch.setattr(
        _gd, "read_payload", lambda: {"tool_input": {"command": "git clean -f"}, "cwd": "/tmp"}
    )
    monkeypatch.setattr(
        _gd, "_clean_violation", lambda cmd: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    assert _gd.main() == 2  # NOT a silent allow


def test_clean_parse_crash_overblocks_clean_mentioning_command(monkeypatch):
    # Accepted safe-direction over-block: on the RARE crash path we cannot tell a
    # real `git clean` from a `clean`-mentioning checkout, so a crashed
    # `git checkout clean-branch` blocks too (message tells the user to simplify).
    monkeypatch.setattr(
        _gd,
        "read_payload",
        lambda: {"tool_input": {"command": "git checkout clean-branch"}, "cwd": "/tmp"},
    )
    monkeypatch.setattr(
        _gd, "_clean_violation", lambda cmd: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    assert _gd.main() == 2


def test_clean_parse_crash_no_clean_substring_fails_open(monkeypatch):
    # A crash on a command with NO `clean` substring never reaches Phase 1's block
    # — only the snapshot path (which swallows its own errors) → allow.
    monkeypatch.setattr(
        _gd,
        "read_payload",
        lambda: {"tool_input": {"command": "git checkout main"}, "cwd": "/tmp"},
    )
    # analyze() would be called by the snapshot path; force it to raise there too.
    monkeypatch.setattr(_gd, "analyze", lambda cmd: (_ for _ in ()).throw(RuntimeError("boom")))
    assert _gd.main() == 0


def test_clean_override_escapes_on_normal_path(monkeypatch):
    # On the NORMAL (parsed) path — the common case — `# discard-override` escapes
    # the clean block via the precise _clean_violation (no crash involved). On the
    # crash path the override does NOT escape by design; the user simplifies.
    monkeypatch.setattr(
        _gd,
        "read_payload",
        lambda: {
            "tool_input": {"command": "git clean -f  # discard-override"},
            "cwd": "/tmp",
        },
    )
    assert _gd.main() == 0


def test_clean_bypass_via_deep_nest_and_cross_segment_decoy_blocks(monkeypatch):
    # End-to-end regression for the security CRITICAL: a deeply-nested $(...)
    # crashes analyze() for REAL (no monkeypatch), and an unrelated cross-segment
    # decoy carries `#...discard-override`. main() must STILL block — the crash
    # fails closed unconditionally, so no decoy can disarm it.
    nest = "$(" * 3000 + "true" + ")" * 3000
    cmd = f"git clean -f {nest} && echo notes#42-discard-override-guide.md"
    monkeypatch.setattr(
        _gd, "read_payload", lambda: {"tool_input": {"command": cmd}, "cwd": "/tmp"}
    )
    assert _gd.main() == 2  # NOT a silent allow


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


# ── submodule-RECURSIVE overwrite: unrecoverable by the superproject snapshot, so
#    it BLOCKS like clean (Codex P1 3839052534 — no false recovery promise) ─────────
@pytest.mark.parametrize(
    "cmd",
    [
        "git restore --recurse-submodules sm",
        "git checkout --recurse-submodules -- sm",
        "git switch --recurse-submodules main",
        "git checkout --recurse-submodules main",
        "git -c submodule.recurse=true restore sm",
        "git -c submodule.recurse=1 checkout -- sm",
        "git -c submodule.recurse restore sm",  # bare config → git treats as true
        "git -c submodule.recurse=true reset --hard",
        "git -c Submodule.Recurse=true checkout -- sm",  # config keys are case-INSENSITIVE
        "git -c submodule.recurse=yes restore sm",  # non-false value → ON
        "git -c submodule.recurse=true read-tree -u -m HEAD",  # read-tree recurses too
        "git checkout --recurse-submodules=true main",  # flag =truthy
    ],
)
def test_submodule_recursive_verbs_block(cmd, monkeypatch):
    monkeypatch.setattr(
        _gd, "read_payload", lambda: {"tool_input": {"command": cmd}, "cwd": "/tmp"}
    )
    assert _gd.main() == 2


@pytest.mark.parametrize(
    "cmd",
    [
        "git restore --no-recurse-submodules sm",  # explicit OFF → not blocked
        "git -c submodule.recurse=false restore sm",  # explicit false → not blocked
        "git -c submodule.recurse=off checkout -- sm",
        "git restore --recurse-submodules=no sm",  # flag =OFF value → SAFE, not blocked
        "git checkout --recurse-submodules=false main",  # flag =false → not blocked
        "git restore --recurse-submodules=0 sm",
        "git restore sm",  # no recursion at all
        "git checkout main",
        "git pull --recurse-submodules",  # not a snapshot verb → out of scope
        "git submodule update --recurse-submodules",  # not a snapshot verb
    ],
)
def test_submodule_non_recursive_or_out_of_scope_not_blocked(cmd, repo, monkeypatch, snap_log):
    monkeypatch.setattr(
        _gd, "read_payload", lambda: {"tool_input": {"command": cmd}, "cwd": str(repo)}
    )
    assert _gd.main() == 0


def test_submodule_recursive_override_escapes(monkeypatch):
    monkeypatch.setattr(
        _gd,
        "read_payload",
        lambda: {
            "tool_input": {"command": "git restore --recurse-submodules sm  # discard-override"},
            "cwd": "/tmp",
        },
    )
    assert _gd.main() == 0


def test_submodule_block_fails_open_on_parser_crash(monkeypatch):
    # UNLIKE clean, the submodule block fails OPEN on a parser crash — a snapshot
    # verb is normally recoverable, so we must not over-block every crashed checkout.
    monkeypatch.setattr(
        _gd,
        "read_payload",
        lambda: {
            "tool_input": {"command": "git checkout --recurse-submodules sm"},
            "cwd": "/tmp",
        },
    )
    monkeypatch.setattr(
        _gd, "_submodule_recurse_violation", lambda cmd: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    assert _gd.main() == 0  # documented residual: crash + submodule not caught


def test_argv_recurses_submodules_helper():
    assert _gd._argv_recurses_submodules(["git", "restore", "--recurse-submodules", "sm"])
    assert _gd._argv_recurses_submodules(["git", "-c", "submodule.recurse=true", "restore"])
    assert _gd._argv_recurses_submodules(["git", "-c", "submodule.recurse", "restore"])
    assert _gd._argv_recurses_submodules(["git", "-c", "Submodule.Recurse=true", "restore"])  # case
    assert _gd._argv_recurses_submodules(["git", "restore", "--recurse-submodules=true"])
    assert not _gd._argv_recurses_submodules(["git", "restore", "--no-recurse-submodules"])
    assert not _gd._argv_recurses_submodules(["git", "-c", "submodule.recurse=false", "restore"])
    assert not _gd._argv_recurses_submodules(["git", "restore", "--recurse-submodules=no"])  # =OFF
    assert not _gd._argv_recurses_submodules(["git", "restore", "--recurse-submodules=0"])
    assert not _gd._argv_recurses_submodules(["git", "restore", "sm"])


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


# ── widened snapshot-verb CLASS: rm/mv/checkout-index/read-tree also overwrite
#    or delete TRACKED work and are recoverable via the stash-create snapshot
#    (Codex round-5 P1 — these were silent-loss vectors with no snapshot).
@pytest.mark.parametrize(
    "cmd",
    [
        "git rm -f tracked.py",
        "git rm -rf tracked.py",
        "git mv -f tracked.py other.py",
        "git checkout-index -f -a",
        "git read-tree --reset -u HEAD",
    ],
)
def test_widened_verbs_are_snapshotted(cmd, repo, snap_log):
    (repo / "tracked.py").write_text("dirty\n")
    _gd._record_snapshots(cmd, {"cwd": str(repo)})
    assert len(_rows(snap_log)) == 1


def test_widened_verbs_reach_snapshot_through_main(repo, snap_log, monkeypatch):
    # The trigger-substring gate in main() must let `git rm`/`git mv` through to
    # the snapshot path (regression guard: `rm`/`mv`/`read-tree` added to
    # _TRIGGER_SUBSTRINGS, else main() returns before snapshotting).
    (repo / "tracked.py").write_text("dirty\n")
    monkeypatch.setattr(
        _gd,
        "read_payload",
        lambda: {"tool_input": {"command": "git rm -f tracked.py"}, "cwd": str(repo)},
    )
    assert _gd.main() == 0
    assert len(_rows(snap_log)) == 1


def test_git_rm_f_is_recoverable(repo, snap_log):
    # End-to-end recovery for the highest-value widened verb: `git rm -f` deletes
    # a modified tracked file. The snapshot commit PRESERVES the bytes — the
    # security property. (For a staged deletion, `stash apply --index` hits a
    # modify/delete conflict, so the reliable restore is `git checkout <sha> --
    # <path>`, which pulls the file straight from the snapshot tree; the recovery
    # note's `--index` hedge — "drop it if the apply conflicts" — points here.)
    (repo / "tracked.py").write_text("precious\n")
    _gd._record_snapshots("git rm -f tracked.py", {"cwd": str(repo)})
    sha = _rows(snap_log)[0]["sha"]
    _git(repo, "rm", "-f", "tracked.py")
    assert not (repo / "tracked.py").exists()
    _git(repo, "checkout", sha, "--", "tracked.py")
    assert (repo / "tracked.py").read_text() == "precious\n"


def test_snapshot_budget_skips_later_repos_and_surfaces(repo, tmp_path, snap_log, monkeypatch):
    # A whole-payload deadline bounds the total time; when it's spent, later repos
    # are skipped AND the skip is surfaced in a note (never a silent cap).
    r2 = tmp_path / "r2"
    r2.mkdir()
    _git(r2, "init", "-q", "-b", "main")
    (r2 / "f.py").write_text("x\n")
    _git(r2, "add", "-A")
    _git(r2, "commit", "-qm", "init")
    (repo / "tracked.py").write_text("dirty\n")
    (r2 / "f.py").write_text("dirty2\n")
    monkeypatch.setattr(_gd, "_TOTAL_SNAPSHOT_BUDGET_S", 0.0)  # budget already spent
    notes = _gd._record_snapshots(
        f"git -C {repo} checkout -- tracked.py && git -C {r2} checkout -- f.py",
        {"cwd": str(tmp_path)},
    )
    assert any("snapshot budget" in n for n in notes)
    assert _rows(snap_log) == []  # nothing snapshotted under a zero budget


def test_snapshot_budget_partial_first_snapshots_second_skipped(
    repo, tmp_path, snap_log, monkeypatch
):
    # The real shape: repo 1 fits the budget and IS snapshotted; the clock then
    # advances past the deadline so repo 2 is skipped and surfaced. Deterministic
    # via a controlled time.monotonic sequence (deadline calc, repo1 remaining,
    # repo2 remaining) — budget default 8, third tick 7.6 → repo2 remaining 0.4.
    r2 = tmp_path / "r2"
    r2.mkdir()
    _git(r2, "init", "-q", "-b", "main")
    (r2 / "f.py").write_text("x\n")
    _git(r2, "add", "-A")
    _git(r2, "commit", "-qm", "init")
    (repo / "tracked.py").write_text("dirty\n")
    (r2 / "f.py").write_text("dirty2\n")
    ticks = iter([0.0, 0.0, 7.6])
    monkeypatch.setattr(_gd.time, "monotonic", lambda: next(ticks, 7.6))
    notes = _gd._record_snapshots(
        f"git -C {repo} checkout -- tracked.py && git -C {r2} checkout -- f.py",
        {"cwd": str(tmp_path)},
    )
    rows = _rows(snap_log)
    assert len(rows) == 1 and rows[0]["cwd"] == str(repo)  # repo 1 snapshotted
    assert any("snapshot budget" in n for n in notes)  # repo 2 skip surfaced


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


def test_recovery_note_returned(repo, snap_log):
    (repo / "tracked.py").write_text("dirty\n")
    notes = _gd._record_snapshots("git checkout -- tracked.py", {"cwd": str(repo)})
    assert notes and "git stash apply --index" in notes[0]


def test_recovery_note_delivered_via_additional_context(repo, snap_log, monkeypatch, capsys):
    # Codex round-5 P2: a snapshot exits 0, and Claude Code discards an exit-0
    # hook's stderr — so the recovery note MUST ride
    # hookSpecificOutput.additionalContext on STDOUT, else the model never sees
    # the sha. Assert main() emits valid additionalContext JSON on stdout.
    (repo / "tracked.py").write_text("dirty\n")
    monkeypatch.setattr(
        _gd,
        "read_payload",
        lambda: {"tool_input": {"command": "git checkout -- tracked.py"}, "cwd": str(repo)},
    )
    assert _gd.main() == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert payload["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "git stash apply --index" in ctx


def test_no_additional_context_when_nothing_snapshotted(repo, snap_log, monkeypatch, capsys):
    # A clean tree yields no snapshot → no note → NO stdout JSON (an empty
    # additionalContext would be noise the model must parse for nothing).
    monkeypatch.setattr(
        _gd,
        "read_payload",
        lambda: {"tool_input": {"command": "git checkout feature"}, "cwd": str(repo)},
    )
    assert _gd.main() == 0
    assert capsys.readouterr().out == ""


# ── whole-tree rewind: a LOUD NOTE, never a block ────────────────────────────
# Origin 2026-08-31: `git checkout <commit> -- .` reverted two already-merged
# PRs. The broad pathspec matches every tracked path, so the command rewrites the
# WHOLE worktree to that commit; the reversion then sits inside the author's own
# diff looking deliberate. The first was found by luck, the second only by a
# second, different check.
#
# It is a NOTE and not a block on purpose, and these tests pin that: `checkout`
# is a snapshot verb, so the pre-command tree IS recoverable, and this module's
# admission test for a block is UNRECOVERABILITY (clean, submodule-recursion).
# What failed was noticing, not recovering — the generic snapshot note fires on
# every checkout and never said "you just rewound the tree".


def _rewind_ctx(cmd: str, repo: Path, monkeypatch, capsys) -> str:
    """Run the guard and return its additionalContext (empty string if silent)."""
    (repo / "tracked.py").write_text("dirty\n")
    monkeypatch.setattr(
        _gd, "read_payload", lambda: {"tool_input": {"command": cmd}, "cwd": str(repo)}
    )
    assert _gd.main() == 0, "the rewind path must NEVER block — it is advisory"
    out = capsys.readouterr().out
    if not out.strip():
        return ""
    return json.loads(out)["hookSpecificOutput"]["additionalContext"]


def test_acceptance_the_2026_08_31_incident_shape_is_named(repo, snap_log, monkeypatch, capsys):
    """The replay: if it does not fire on the command that actually caused the
    incident, it does not ship."""
    old = _git(repo, "rev-parse", "HEAD").strip()
    ctx = _rewind_ctx(f"git checkout {old} -- .", repo, monkeypatch, capsys)
    assert "WHOLE-TREE REWIND" in ctx
    assert old in ctx, "the note must name the commit being rewound to"
    # It must say what the command DOES, not merely that a snapshot exists — the
    # generic note already said that on 2026-08-31 and the reversion still shipped.
    assert "every tracked path" in ctx
    assert "looking deliberate" in ctx
    # …and scoped honestly. The predicate is deliberately generous (any operand
    # ending in `/`), so it also fires on `git checkout main -- tests/`, which
    # does NOT touch the whole tree. An unconditional "rewrites EVERY tracked
    # path" would be false for those firings — in a module whose entire ethos is
    # not over-claiming, and immediately before a recovery instruction premised
    # on a whole-tree rewind.
    assert "UNDER THE PATHSPEC YOU GAVE" in ctx
    # And point at the conflict-aware alternatives that fail loudly instead.
    for safe in ("merge --squash", "cherry-pick", "apply --3way"):
        assert safe in ctx


@pytest.mark.parametrize(
    "cmd_t,label",
    [
        ("git checkout {old} -- .", "the incident shape"),
        ("git checkout {old} .", "no -- separator"),
        ("git checkout {old} :/", "magic repo-root pathspec"),
        ("git checkout {old} -- src/", "a directory operand"),
        ("git restore --source={old} .", "restore, attached source"),
        ("git restore -s {old} .", "restore, separated source"),
        ("git read-tree -u {old}", "read-tree -u: whole tree, no pathspec at all"),
    ],
)
def test_rewind_shapes_fire(cmd_t, label, repo, snap_log, monkeypatch, capsys):
    old = _git(repo, "rev-parse", "HEAD").strip()
    ctx = _rewind_ctx(cmd_t.format(old=old), repo, monkeypatch, capsys)
    assert "WHOLE-TREE REWIND" in ctx, f"should fire: {label}"


@pytest.mark.parametrize(
    "cmd_t,label",
    [
        ("git checkout -- tracked.py", "plain local discard of one file"),
        ("git checkout .", "THE common local discard — first operand is a pathspec"),
        ("git checkout src/", "directory local discard"),
        ("git checkout HEAD -- .", "discard back to where I already am"),
        ("git checkout @ -- .", "@ is a HEAD alias"),
        ("git checkout feature", "branch switch, no pathspec"),
        ("git restore .", "restore reads the INDEX with no --source"),
        ("git read-tree {old}", "read-tree without -u touches only the index"),
        ("git status", "not a rewrite at all"),
    ],
)
def test_ordinary_work_stays_quiet(cmd_t, label, repo, snap_log, monkeypatch, capsys):
    """A note that fires on the everyday discard gets ignored, and then it is
    worth nothing on the day it matters."""
    old = _git(repo, "rev-parse", "HEAD").strip()
    ctx = _rewind_ctx(cmd_t.format(old=old), repo, monkeypatch, capsys)
    assert "WHOLE-TREE REWIND" not in ctx, f"must stay quiet: {label}"


def test_the_rewind_path_never_blocks(repo, snap_log, monkeypatch, capsys):
    """The design contract this change must not break: for the RECOVERABLE verbs
    the hook is advisory and never exits non-zero. A rewind is recoverable — the
    snapshot runs first — so it warns and allows."""
    old = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "tracked.py").write_text("dirty\n")
    monkeypatch.setattr(
        _gd,
        "read_payload",
        lambda: {"tool_input": {"command": f"git checkout {old} -- ."}, "cwd": str(repo)},
    )
    assert _gd.main() == 0


def test_the_recovery_command_in_the_note_ACTUALLY_WORKS(repo, snap_log, monkeypatch, capsys):
    """EXECUTE the recovery command against the post-rewind state.

    This is the test that matters, and its absence is what let a bad one ship.
    The first version of this note recommended `git stash apply --index <sha>`,
    and a test that merely string-matched that command was green — while the
    command itself, run for real, exits 1 and writes CONFLICT MARKERS into the
    file the user is trying to rescue. A whole-tree rewind leaves every tracked
    path staged-modified, which is exactly what `stash apply` refuses to merge
    into.

    So: perform a real rewind, pull the command out of the note, run it, and
    assert the pre-command content came back. Nothing weaker can distinguish a
    usable recovery instruction from a harmful one.
    """
    old = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "tracked.py").write_text("MY-UNCOMMITTED-WORK\n")
    monkeypatch.setattr(
        _gd,
        "read_payload",
        lambda: {"tool_input": {"command": f"git checkout {old} -- ."}, "cwd": str(repo)},
    )
    assert _gd.main() == 0
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    rewind_line = next(line for line in ctx.splitlines() if "WHOLE-TREE REWIND" in line)

    # Perform the destruction the note describes.
    _git(repo, "checkout", old, "--", ".")
    assert (repo / "tracked.py").read_text() == "orig\n", "the rewind must have happened"

    # Extract the recommended command verbatim and run it.
    snap = _rows(snap_log)[-1]["sha"]
    assert f"git checkout {snap[:12]} -- ." in rewind_line, (
        "the note must recommend the restore-from-snapshot form"
    )
    assert "git stash apply" not in rewind_line.split("NOT `git stash apply`")[0], (
        "it must not RECOMMEND stash apply — that form conflicts here"
    )
    subprocess.run(
        ["git", "-C", str(repo), "checkout", snap[:12], "--", "."],
        check=True,
        capture_output=True,
        env=_GIT_ENV,
    )
    assert (repo / "tracked.py").read_text() == "MY-UNCOMMITTED-WORK\n", (
        "the recovery command the note gives must actually restore the work"
    )


def test_the_note_warns_against_the_command_that_does_not_work(repo, snap_log, monkeypatch, capsys):
    """`git stash apply` is the obvious thing to reach for and it makes this
    situation worse, so the note says so rather than staying silent."""
    old = _git(repo, "rev-parse", "HEAD").strip()
    ctx = _rewind_ctx(f"git checkout {old} -- .", repo, monkeypatch, capsys)
    line = next(x for x in ctx.splitlines() if "WHOLE-TREE REWIND" in x)
    assert "NOT `git stash apply`" in line
    assert "conflict markers" in line


# ── the tripwire: recurrence must be COUNTABLE, not argued ───────────────────
# The owner's decision (2026-09-06) was "loud note now, block if it recurs". That
# only means something if recurrence can be measured, so every match writes a
# flag to the snapshot log.


def test_a_rewind_is_flagged_in_the_snapshot_log(repo, snap_log, monkeypatch, capsys):
    old = _git(repo, "rev-parse", "HEAD").strip()
    _rewind_ctx(f"git checkout {old} -- .", repo, monkeypatch, capsys)
    assert _rows(snap_log)[-1].get("tree_rewind") is True


def test_an_ordinary_discard_is_not_flagged(repo, snap_log, monkeypatch, capsys):
    _rewind_ctx("git checkout -- tracked.py", repo, monkeypatch, capsys)
    row = _rows(snap_log)[-1]
    assert "tree_rewind" not in row, "the flag must mean something when present"


def test_the_log_row_stays_metadata_only(repo, snap_log, monkeypatch, capsys):
    """The row deliberately carries no command text and no commit-ish: this log is
    durable and a Bash payload can carry credentials."""
    old = _git(repo, "rev-parse", "HEAD").strip()
    _rewind_ctx(f"git checkout {old} -- .", repo, monkeypatch, capsys)
    row = _rows(snap_log)[-1]
    assert set(row) == {"ts", "cwd", "sha", "tree_rewind"}
    assert old not in json.dumps(row)


def test_an_overridden_rewind_is_silent_but_still_counted(repo, snap_log, monkeypatch, capsys):
    """`# discard-override` says "I know" — so drop the note, but still record it.
    Whether to escalate to a block is a question about how often this HAPPENS, and
    an acknowledged rewind is still a rewind."""
    old = _git(repo, "rev-parse", "HEAD").strip()
    ctx = _rewind_ctx(f"git checkout {old} -- .  # discard-override", repo, monkeypatch, capsys)
    assert "WHOLE-TREE REWIND" not in ctx
    assert _rows(snap_log)[-1].get("tree_rewind") is True


def test_a_broken_rewind_predicate_never_costs_the_snapshot(repo, snap_log, monkeypatch, capsys):
    """The snapshot is the actual recovery mechanism; this note is a courtesy on
    top. A bug in the courtesy must not take the mechanism down with it."""
    old = _git(repo, "rev-parse", "HEAD").strip()
    monkeypatch.setattr(
        _gd, "_tree_rewind_segments", lambda cmd: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    ctx = _rewind_ctx(f"git checkout {old} -- .", repo, monkeypatch, capsys)
    assert _rows(snap_log), "the snapshot must still have been taken"
    assert "snapshotted the worktree" in ctx
    assert "WHOLE-TREE REWIND" not in ctx


# ── multi-repo compounds: the sha and the flag must follow the RIGHT repo ────
# `git -C A … && git -C B checkout <c> -- .` is ordinary here (this box runs many
# worktrees). Before these existed, the note advertised the FIRST snapshotted
# repo's sha regardless of which repo was rewound — measured: a sha that does not
# even resolve in the repo the reader is standing in.


@pytest.fixture
def two_repos(tmp_path: Path) -> tuple[Path, Path, str]:
    """Two repos with DIFFERENT content — identical trees hash identically and
    would mask a wrong-sha bug entirely."""
    made = []
    for name, body in (("A", "alpha"), ("B", "bravo")):
        r = tmp_path / name
        r.mkdir()
        _git(r, "init", "-q", "-b", "main")
        (r / "tracked.py").write_text(f"{body}-v1\n")
        _git(r, "add", "-A")
        _git(r, "commit", "-qm", "v1")
        made.append(r)
    a, b = made
    b_old = _git(b, "rev-parse", "HEAD").strip()
    (b / "tracked.py").write_text("bravo-v2\n")
    _git(b, "commit", "-qam", "v2")
    (a / "tracked.py").write_text("alpha-dirty\n")
    (b / "tracked.py").write_text("bravo-dirty\n")
    return a, b, b_old


def test_the_note_carries_the_REWOUND_repos_sha_not_another(
    two_repos, snap_log, monkeypatch, capsys
):
    a, b, b_old = two_repos
    cmd = f"git -C {a} rm -r --cached tracked.py && git -C {b} checkout {b_old} -- ."
    monkeypatch.setattr(
        _gd, "read_payload", lambda: {"tool_input": {"command": cmd}, "cwd": str(a.parent)}
    )
    assert _gd.main() == 0
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    line = next(x for x in ctx.splitlines() if "WHOLE-TREE REWIND" in x)

    by_cwd = {row["cwd"]: row["sha"] for row in _rows(snap_log)}
    assert str(a) in by_cwd and str(b) in by_cwd, "both repos should be snapshotted"
    assert by_cwd[str(b)][:12] in line, "must offer the REWOUND repo's snapshot"
    assert by_cwd[str(a)][:12] not in line, "must not offer an unrelated repo's sha"
    assert str(b) in line, "and must name which repo it is talking about"


def test_only_the_rewound_repo_is_flagged_in_the_log(two_repos, snap_log, monkeypatch, capsys):
    """The tripwire decides whether this becomes a block, so an inflated count
    argues for one on false evidence."""
    a, b, b_old = two_repos
    cmd = f"git -C {a} rm -r --cached tracked.py && git -C {b} checkout {b_old} -- ."
    monkeypatch.setattr(
        _gd, "read_payload", lambda: {"tool_input": {"command": cmd}, "cwd": str(a.parent)}
    )
    assert _gd.main() == 0
    flagged = {row["cwd"] for row in _rows(snap_log) if row.get("tree_rewind")}
    assert flagged == {str(b)}


def test_attached_short_source_form_fires(repo, snap_log, monkeypatch, capsys):
    """`git restore -s<sha> .` — git accepts it and performs the rewind. Its long
    twin `--source=<sha>` was handled from the start; this spelling was not, so
    two spellings of one command behaved differently."""
    old = _git(repo, "rev-parse", "HEAD").strip()
    ctx = _rewind_ctx(f"git restore -s{old} .", repo, monkeypatch, capsys)
    assert "WHOLE-TREE REWIND" in ctx


@pytest.mark.parametrize("pathspec", [".", "./", ":/", ":/.", "*"])
def test_every_broad_pathspec_in_the_set_is_covered(pathspec, repo, snap_log, monkeypatch, capsys):
    """Pins the whole constant. Trimming it to {'.', './', ':/'} previously left
    the suite green, so `:/.` and `*` were carried without any test."""
    old = _git(repo, "rev-parse", "HEAD").strip()
    ctx = _rewind_ctx(f"git checkout {old} -- {pathspec}", repo, monkeypatch, capsys)
    assert "WHOLE-TREE REWIND" in ctx, f"{pathspec} is in the constant but unpinned"
