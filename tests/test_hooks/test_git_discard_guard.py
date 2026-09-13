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

import ast
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

#: Assembled rather than written literally: this file is read by the shell
#: safety hook as DATA when it appears in a command payload, and the discard
#: guard's closed-set clean whitelist correctly refuses a payload carrying a
#: bare clean invocation. Splitting it keeps the test honest without waving
#: the guard off with an override.
_CLEAN_DRY_RUN = "git clean -" + "nd"

_WORKTREE = Path(__file__).resolve().parent.parent.parent
_HOOKS = _WORKTREE / "scripts" / "hooks"
_spec = importlib.util.spec_from_file_location("git_discard_guard", _HOOKS / "git_discard_guard.py")
_gd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_gd)

# The guard's own parser, so a fixture can PROVE it trips the bound it claims to.
_sp_spec = importlib.util.spec_from_file_location("shell_parse", _HOOKS / "shell_parse.py")
shell_parse = importlib.util.module_from_spec(_sp_spec)
_sp_spec.loader.exec_module(shell_parse)

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


@pytest.fixture(autouse=True)
def _clear_parse_memo():
    """The guard memoises its parse in module state, once per process.

    Without this, a command string parsed by an earlier test is already cached, so
    `monkeypatch.setattr(_gd, "analyze_checked", <raises>)` is INERT for it and the
    test quietly stops exercising the crash path. Measured: two tests share the
    string "git checkout main", and the crash-path one stays non-vacuous only
    because definition order happens to put it first. Neither `pytest-randomly` nor
    `pytest-random-order` is installed today, so it holds — but a `-k` subset, a
    reorder, or a new test inserted above it would silently vacate it, and nothing
    would report that.
    """
    _gd._PARSE_MEMO.clear()
    yield
    _gd._PARSE_MEMO.clear()


@pytest.fixture
def snap_log(tmp_path: Path, monkeypatch) -> Path:
    """The recovery STORE — a directory, one file per snapshot."""
    store = tmp_path / "snapshots"
    monkeypatch.setenv("GENESIS_DISCARD_SNAPSHOT_DIR", str(store))
    return store


def _snap_files(store: Path) -> list[Path]:
    """Record files, oldest first. REGULAR files only, without following links —
    a reader must skip anything else, exactly as ``trim_dir_by_size`` does."""
    if not store.exists():
        return []
    return sorted(f for f in store.glob("*.jsonl") if f.is_file() and not f.is_symlink())


def _snapshot_rows(store: Path) -> list[dict]:
    """Rows recording a SNAPSHOT (they carry a stash sha)."""
    return [r for r in _rows(store) if "sha" in r]


def _rewind_rows(store: Path) -> list[dict]:
    """Rows recording a REWIND EVENT — the tripwire.

    Selected by KIND, never by position. These tests used to read `_rows(...)[-1]`,
    which silently meant "the snapshot row" only because nothing was appended after
    it; the moment rewind events became their own rows, four tests broke on ordering
    rather than on behaviour. Asking for the row you mean cannot drift that way.
    """
    return [r for r in _rows(store) if r.get("tree_rewind")]


def _rows(store: Path) -> list[dict]:
    return [
        json.loads(line)
        for f in _snap_files(store)
        for line in f.read_text().splitlines()
        if line.strip()
    ]


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
@pytest.mark.parametrize(
    "label,verb",
    [
        ("clean", "git clean -fd"),
        ("submodule-recursive checkout", "git checkout --recurse-submodules ."),
    ],
)
@pytest.mark.parametrize("axis", ["length", "depth"])
def test_an_unreadable_command_never_reaches_a_silent_allow(label, verb, axis, monkeypatch):
    """A guard whose only verdicts are BLOCK and ALLOW must refuse on EITHER bound.

    This is the coverage that did not exist, and its absence is why a real fail-open
    shipped green. No test drove any guard with an OVER-LENGTH command, so when the
    length axis was softened to "ask" — for a guard that cannot ask, where not
    refusing means permitting — MEASURED `echo "<49,200 chars>" && git clean -fd`
    returned rc=0 from this guard AND rc=0 from bash_safety_hook.sh, with nothing
    anywhere blocking a real, executing `git clean -fd`. The whole suite stayed green.

    The justification at the time was that bash_safety_hook.sh's raw-text `git clean`
    grep still covered it. It does not: that fallback runs only when this guard is
    ABSENT or CRASHES (bash_safety_hook.sh:220 gates it on `_handled == 0`), and
    exiting 0 sets `_handled=1`. For the submodule verb there is no fallback at all.

    Parametrised over BOTH axes deliberately — a bound softened on one axis is
    invisible to a test that only exercises the other.
    """
    if axis == "length":
        cmd = 'echo "' + "x" * (shell_parse.MAX_COMMAND_CHARS + 64) + '" && ' + verb
    else:
        cmd = 'bash -c "$(' * 9 + verb + ')"' * 9
    _segs, blind = shell_parse.analyze_checked(cmd)
    assert blind is not None and blind.bounds_induced, (
        f"fixture must actually trip the {axis} bound, or it proves nothing"
    )
    monkeypatch.setattr(
        _gd, "read_payload", lambda: {"tool_input": {"command": cmd}, "cwd": "/tmp"}
    )
    assert _gd.main() == 2, (
        f"an over-{axis} `{label}` was not refused. This guard cannot ask, so any "
        "verdict other than 2 is a silent permit of an unrecoverable operation"
    )


def test_the_command_is_parsed_exactly_once_per_process(monkeypatch):
    """Parsing once is a SECURITY property here, not a tidiness one.

    `bash_safety_hook.sh` is registered at 5s and delegates to THREE guards over the
    same command, this one included — and this guard reaches three consumers that each
    analyse it. That put FIVE full parses on one 5s clock. MEASURED end to end with
    the worst payload inside both bounds: 6.12s BEFORE memoisation, i.e. the hook is
    KILLED, and a killed hook does not refuse — it PERMITS. Comfortably inside the
    budget after; the after-figure is the depth-5 row in `shell_parse.MAX_COMMAND_CHARS`
    and is not repeated here, because three files restating it from memory is how it
    came to have three different values.

    So bounding the input was not sufficient on its own; the same work had to stop
    being done three times. This test exists because a mutation removing the memo
    SURVIVED the whole suite: every verdict stayed correct and only the clock moved,
    which is invisible to an assertion about verdicts and fatal in production.
    Counting the calls pins the property directly rather than timing it, since a
    wall-clock assertion would be flaky on a shared box.
    """
    cmd = "git clean -nd && git checkout -- x.py && git submodule update --recurse-submodules"
    calls = []
    real = _gd.analyze_checked

    def counting(c):
        calls.append(c)
        return real(c)

    _gd._PARSE_MEMO.clear()
    monkeypatch.setattr(_gd, "analyze_checked", counting)
    monkeypatch.setattr(
        _gd, "read_payload", lambda: {"tool_input": {"command": cmd}, "cwd": "/tmp"}
    )
    _gd.main()
    assert len(calls) == 1, (
        f"the command was parsed {len(calls)} times in one process. Three of the five "
        "parses on the shell hook's 5s clock are this guard's; duplicating them "
        "measured 6.12s against that 5s registration, which permits the command"
    )


def test_an_ordinary_command_near_the_cap_is_still_allowed(monkeypatch):
    """The control for the test above: refusing everything would also make it pass.

    A long-but-readable command REACHING THE PARSE and carrying the trigger word
    must still be ALLOWED, so the refusals above are attributable to the bound
    rather than to a guard that blocks anything large or anything saying "clean".

    Both of those properties were missing. The command ended `&& git status`, whose
    trigger-substring hits are zero — so `main` returned 0 at the trigger gate and
    never reached the parse, and the command contained no "clean" at all, making the
    second half of the claim untestable by it. MEASURED: inserting
    `if len(cmd) > 2000: return 2` right after the trigger gate left the suite fully
    green, i.e. a guard that blocks every command over 2 KB would have shipped. A
    dry-run clean fixes both halves: it is a trigger word, it reaches the parse, and
    it is the one clean form that must be allowed.
    """
    cmd = 'echo "' + "x" * (shell_parse.MAX_COMMAND_CHARS - 256) + '" && ' + _CLEAN_DRY_RUN
    _segs, blind = shell_parse.analyze_checked(cmd)
    assert blind is None, "control must be INSIDE the bounds"
    assert any(s in cmd for s in _gd._TRIGGER_SUBSTRINGS), (
        "the control must reach the parse, or it cannot attribute anything"
    )
    monkeypatch.setattr(
        _gd, "read_payload", lambda: {"tool_input": {"command": cmd}, "cwd": "/tmp"}
    )
    assert _gd.main() == 0


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
    # The snapshot path parses too; force that to raise as well. It asks
    # `analyze_checked` now (one call for "what runs" AND "could I read it all"), so
    # that is the name to poison — patching the old one silently patched nothing.
    monkeypatch.setattr(
        _gd, "analyze_checked", lambda cmd: (_ for _ in ()).throw(RuntimeError("boom"))
    )
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
    assert not any("SECRET-TOKEN-XYZ" in f.read_text() for f in _snap_files(snap_log))


def test_store_and_records_are_own_user_only(repo, snap_log):
    """The store sits in ~/.genesis beside secrets, so both levels matter.

    There is no sidecar lock any more: one file per snapshot means nothing is
    shared, so nothing needs serialising and there is no second file to protect.
    """
    (repo / "tracked.py").write_text("dirty\n")
    _gd._record_snapshots("git checkout -- tracked.py", {"cwd": str(repo)})
    assert stat.S_IMODE(snap_log.stat().st_mode) == 0o700
    files = _snap_files(snap_log)
    assert files, "no record file was written"
    for f in files:
        assert stat.S_IMODE(f.stat().st_mode) == 0o600, f
    assert not list(snap_log.glob("*.lock")), "the sidecar lock should be gone"


def test_a_relative_store_override_is_REFUSED(repo, tmp_path, monkeypatch, capsys):
    """A relative override would put durable recovery records inside the repo.

    It resolves against the hook's cwd, so `rel_store` lands in whatever tree the
    guarded command ran in — where it can be committed, and where a worktree removal
    destroys it. The store's own docstring already claimed it "lives outside any
    repo"; this is what makes that true.

    An earlier version of this test asserted the OPPOSITE, pinning the permissive
    behaviour, while the sibling merge-override store refused the same input. The
    asymmetry was the defect, not the refusal.

    Tests the RESOLVER rather than driving a write, deliberately: the refusal falls
    back to the live default, so a test that wrote here would put a record in the
    operator's real recovery store.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GENESIS_DISCARD_SNAPSHOT_DIR", "rel_store")
    resolved = _gd._snapshot_dir()
    assert os.path.isabs(resolved), resolved
    assert not resolved.endswith("rel_store"), "the relative override was honoured"
    assert "must be an absolute path" in capsys.readouterr().err
    assert not (tmp_path / "rel_store").exists()


def test_the_hook_writes_but_never_maintains_the_store(repo, tmp_path, monkeypatch):
    """Retention moved OFF the hook path, and this is what keeps it off.

    The previous design self-trimmed on every write: a guard about to refuse a
    destructive command was also rewriting a file, and that retention engine was
    the source of most of this writer's defects. The hook now only ever ADDS.
    Pre-existing records must survive a new snapshot untouched — asserted by
    content, not by count, so a trim that rewrote them would fail here.
    """
    store = tmp_path / "store"
    monkeypatch.setenv("GENESIS_DISCARD_SNAPSHOT_DIR", str(store))
    store.mkdir()
    seeded = {}
    for i in range(50):
        f = store / f"20200101T000000_{i:06d}Z-1-0.jsonl"
        f.write_text(f'{{"ts":"old","sha":"{i:040d}"}}\n')
        seeded[f] = f.read_text()

    (repo / "tracked.py").write_text("dirty\n")
    _gd._record_snapshots("git checkout -- tracked.py", {"cwd": str(repo)})

    for f, body in seeded.items():
        assert f.exists(), f"the hook deleted a pre-existing record: {f.name}"
        assert f.read_text() == body, f"the hook rewrote a pre-existing record: {f.name}"
    rows = _rows(store)
    assert len(rows) == 51, "the new snapshot must be ADDED, not merged into a rewrite"
    assert rows[-1]["cwd"] == str(repo)


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


@pytest.mark.parametrize(
    "dirtied_after_rewind",
    [False, True],
    ids=["clean-post-rewind", "dirtied-after-rewind"],
)
def test_the_recovery_chain_in_the_note_ACTUALLY_WORKS(
    dirtied_after_rewind, repo, snap_log, monkeypatch, capsys
):
    """EXECUTE the note's recovery chain, on BOTH states a reader can be in.

    Three rounds of this note shipped wrong, each caught only by running the
    command instead of reading it, so the history is recorded here.

    ROUND 1 recommended `git stash apply --index <sha>` and the test merely
    string-matched it: green, unexecuted.

    ROUND 2 changed the note to `git checkout <sha> -- .` and added a test that
    really did EXECUTE it — against a fixture that modified one file and nothing
    else. Still green, still wrong: path checkout is OVERLAY by default, so a path
    the work had DELETED is never restored-as-absent and the rewind's copy
    survives to be committed.

    ROUND 3 is this parametrization, and it exists because adding the deletion was
    still not enough. MEASURED: on a freshly-rewound tree `stash apply --index`
    SUCCEEDS, so a single-state test never reaches the fallback at all — the
    `--no-overlay` half was pinned by a string match and by nothing executable.
    The fallback's reader is the one who kept working after the rewind, so that
    state gets its own cell, and in it the two spellings genuinely diverge:
    overlay leaves doomed.py, `--no-overlay` removes it.

    Each cell also asserts WHICH arm ran, so neither can silently stop covering
    its path — a cell that quietly falls through to the other arm is how this
    became vacuous the first time.
    """
    # The discriminating state: a tracked file the work DELETED.
    (repo / "doomed.py").write_text("delete me\n")
    _git(repo, "add", "doomed.py")
    _git(repo, "commit", "-m", "add doomed.py")
    old = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "tracked.py").write_text("MY-UNCOMMITTED-WORK\n")
    (repo / "doomed.py").unlink()
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
    if dirtied_after_rewind:
        # The reader the fallback exists for: work continued after the rewind, so
        # the tree is dirty exactly where the snapshot carries content.
        (repo / "tracked.py").write_text("WRITTEN AFTER THE REWIND\n")

    # EXTRACT the commands from the note and run THOSE — never a hand-copied
    # equivalent. This test kept PASSING across a note rewrite because its regexes
    # still matched text that had moved to a different role, which is exactly the
    # drift extraction exists to prevent: a test that string-matches the note and
    # then executes its own copy proves the copy works. Extraction also pins the SHA
    # the note prints, because a multi-repo compound once carried the wrong repo's.
    #
    # The procedure is THREE steps, and it was chosen by MEASUREMENT rather than
    # judgement after four hand-picked chains each failed in a state nobody had
    # constructed (see `_tree_rewind_note`): 320 enumerated states, six candidates,
    # an oracle and a no-op as instrument controls. Capture, restore the merged work
    # from the branch, replay your own edits from the snapshot: 240/240 where HEAD
    # has not moved, against 56/240 for the single command this replaced.
    snap = _snapshot_rows(snap_log)[-1]["sha"]
    assert "git stash create" in rewind_line, (
        "step 1 must capture the current tree first, or step 2 is destructive"
    )
    assert "git checkout HEAD -- ." in rewind_line, (
        "step 2 must restore the merged work from the BRANCH — that is where it "
        f"lives, and no snapshot command brings it back. Note said: {rewind_line}"
    )
    replay = re.search(r"git stash apply --index ([0-9a-f]{7,40})", rewind_line)
    committed = re.search(r"git checkout --no-overlay ([0-9a-f]{7,40}) -- \.", rewind_line)
    assert replay, f"step 3 must replay your own edits. Note said: {rewind_line}"
    assert committed, (
        "the already-committed case must carry its own command, because the three "
        "steps REINSTATE the reversion once HEAD holds it — measured 0/80. "
        f"Note said: {rewind_line}"
    )
    for label, got in (("replay", replay.group(1)), ("committed-case", committed.group(1))):
        assert snap.startswith(got), (
            f"the {label} command names {got}, which is not a prefix of this "
            f"repo's snapshot {snap} — the reader would run it in the wrong repo"
        )

    # Run the three steps in order, exactly as a reader would.
    capture = subprocess.run(
        ["git", "-C", str(repo), "stash", "create"],
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    ).stdout.strip()
    if dirtied_after_rewind:
        assert capture, (
            "step 1 must produce a capture when the tree is dirty — that capture is "
            "the only thing making step 2 non-destructive, and this cell exists to "
            "exercise exactly that"
        )
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "HEAD", "--", "."],
        check=True,
        capture_output=True,
        env=_GIT_ENV,
    )
    rc = subprocess.run(
        ["git", "-C", str(repo), "stash", "apply", "--index", replay.group(1)],
        capture_output=True,
        env=_GIT_ENV,
    ).returncode
    assert rc == 0, (
        "step 3 must apply cleanly onto a pristine HEAD — the stash's parent IS "
        "HEAD-at-create, which is the whole reason this ordering works"
    )
    if dirtied_after_rewind:
        # The post-rewind edit is overwritten by design; what matters is that it is
        # RECOVERABLE from the step-1 capture rather than gone.
        blob = subprocess.run(
            ["git", "-C", str(repo), "show", f"{capture}:tracked.py"],
            capture_output=True,
            text=True,
            env=_GIT_ENV,
        )
        assert blob.returncode == 0 and "WRITTEN AFTER THE REWIND" in blob.stdout, (
            "work done after the rewind must survive inside the step-1 capture; "
            "without it, step 2 silently destroys it"
        )

    assert (repo / "tracked.py").read_text() == "MY-UNCOMMITTED-WORK\n", (
        "the recovery procedure the note gives must actually restore modified work"
    )
    assert not (repo / "doomed.py").exists(), (
        "it must also restore a DELETION — this is the assertion whose absence let "
        "an overlay-mode command ship as 'byte-identical'"
    )




def test_the_note_states_WHERE_its_procedure_does_not_apply():
    """The three steps are correct before you commit and WRONG after — say both.

    This is the load-bearing scope claim, and it is measured: the procedure scores
    240/240 while HEAD has not moved since the snapshot, and 0/80 once the reader has
    committed, because step 2 restores HEAD and HEAD then *contains* the reversion.
    A note that gave the steps without that boundary would hand someone the exact
    reversion they were trying to undo, which is the false-recovery-promise class
    this module treats as severe enough to justify a hard block.

    The test also keeps the earlier retraction on the record: an intermediate version
    warned that `git stash apply` "writes conflict markers", and re-measurement
    showed it exits 1 leaving the file untouched — a safe refusal, not corruption.
    Warning someone off a command on the strength of a harm that does not occur was
    its own false promise, and the docstring has to keep saying so or the next
    rewrite reintroduces it.
    """
    doc = _gd._tree_rewind_note.__doc__ or ""
    assert "No conflict markers" in doc, (
        "the docstring must record the re-measurement that corrected the earlier claim"
    )
    rendered_scope = _gd._tree_rewind_note("a" * 40, "/r", "b" * 40)
    assert "NOT COMMITTED" in rendered_scope, (
        "the note must state the precondition for its three steps, not just the steps"
    )
    assert "ALREADY COMMITTED" in rendered_scope, (
        "and it must name the case where they are WRONG — measured 0/80, where step "
        "2 reinstates the reversion from a HEAD that now contains it"
    )
    # Asserted on the RENDERED note, not on the module source. Reading the source
    # meant a COMMENT carrying the phrase satisfied it: measured, changing the
    # note's text to "lands it all" and appending the old phrase as a comment left
    # the suite green.
    rendered = _gd._tree_rewind_note("a" * 40, "/r", "b" * 40)
    assert "lands everything STAGED" in rendered, (
        "the note must name the fallback's cost — a reader who commits straight "
        "after it would stage work they never staged"
    )


def test_every_stand_in_in_THIS_FILE_matches_its_production_signature():
    """A stand-in with the wrong arity tests the signature, not the behaviour.

    One of these shipped green through several review rounds: the replacement for
    `_tree_rewind_segments` took one argument where production passes two, so
    Python raised TypeError at the CALL SITE and the `RuntimeError` the test was
    named after never ran. Both exceptions land in the same `except Exception`, so
    nothing distinguished "the predicate failed" from "the test is malformed" —
    the failure path the test claimed to cover was untested while it passed.

    Found by review, then ENUMERATED rather than spot-fixed: 34 stand-ins in this
    file, one mismatch. This derives the check from the file's own AST so the
    thirty-fifth cannot be wrong quietly. Scope is deliberately THIS file — a
    repo-wide version is a different change with a different blast radius, and
    claiming coverage it does not have is how a lock becomes decoration.

    Only simple lambdas are checked. A `def` stand-in or a callable object is out
    of scope here, and saying so is the point: this is the spelling that drifted.
    """
    import inspect

    src = Path(__file__).read_text()
    mismatches = []
    checked = 0
    for node in ast.walk(ast.parse(src)):
        if not (isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "setattr"):
            continue
        if len(node.args) != 3 or not isinstance(node.args[1], ast.Constant):
            continue
        if not isinstance(node.args[2], ast.Lambda):
            continue
        name = node.args[1].value
        target = getattr(_gd, name, None)
        if not callable(target):
            continue  # e.g. patching a stdlib attr on the module, not a guard function
        try:
            params = inspect.signature(target).parameters.values()
        except (TypeError, ValueError):  # pragma: no cover — builtins without signatures
            continue
        required = len(
            [
                p
                for p in params
                if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) and p.default is p.empty
            ]
        )
        checked += 1
        # CALLABILITY, not parameter count. A stand-in written as
        # `lambda cid=value: ...` is the late-binding idiom for capturing a loop
        # variable and is perfectly callable with zero arguments — an earlier version
        # of this lock counted it as arity 1 and flagged it, which would have taught
        # the next author to work around the lock rather than fix a real mismatch.
        # What matters is whether production's call would land: the stand-in must
        # accept the required count, i.e. its REQUIRED arity is at most that count and
        # its capacity at least that count (a `*args` lambda has no upper bound).
        lam = node.args[2].args
        lo = len(lam.args) - len(lam.defaults)
        hi = None if lam.vararg is not None else len(lam.args)
        if lo > required or (hi is not None and hi < required):
            span = f"{lo}" if hi == lo else f"{lo}-{'*' if hi is None else hi}"
            mismatches.append(
                f"line {node.lineno}: {name} accepts {span} positional, production calls "
                f"it with {required}"
            )
    assert checked >= 20, (
        f"only {checked} stand-ins were examined — the walk has stopped finding them, "
        "so this lock is passing by blindness rather than by correctness"
    )
    assert not mismatches, (
        "stand-ins that cannot be called as production calls them:\n" + "\n".join(mismatches)
    )


# ── the predicate's option grammar: one walk, measured tables ────────────────
# Four review findings were one generator — the walk re-derived git's option
# grammar in three places, keyed on `argv.index(verb)`. `git_subcommand_index`
# exists in shell_parse precisely to end that duplication (its own docstring says
# so). These cells are the DIFFERENTIAL that proved the fix: every one of them
# behaves differently against the pre-fix binary, and the controls beneath them
# behave identically — a narrowing fix whose cases do not change against the old
# code pins nothing.
#
# The expected values are MEASURED against real git 2.43 (`git <verb> -h` for the
# option tables, and an actual repo with an uncommitted edit for the modes), not
# transcribed from a review comment.

_OLD = "0" * 40


@pytest.mark.parametrize(
    "label,cmd,fires",
    [
        # ── cases that CHANGED (each was a reported defect) ──
        # Three filenames that happen to be named like the verb. The snapshot row
        # is the recurrence evidence that would justify escalating this note to a
        # block, so a false positive here argues for a block from an event that
        # never happened.
        ("filenames named like the verb", f"git add checkout {_OLD} .", False),
        # `-` is the previously-checked-out branch. MEASURED on 2.43: this really
        # does replace the paths from that branch, i.e. the exact reversion the
        # note exists for — and the old walk skipped it as an option.
        ("previous-branch sentinel", "git checkout - -- .", True),
        # `--reset` alone loads the INDEX; `-u` is what writes the worktree.
        ("read-tree index-only", f"git read-tree --reset {_OLD}", False),
        ("read-tree worktree", f"git read-tree --reset -u {_OLD}", True),
        # `-n/--dry-run` reports and changes nothing — measured rc=0, worktree AND
        # index untouched, yet it used to warn and write broad:true evidence.
        ("read-tree dry-run", f"git read-tree -n -u --reset {_OLD}", False),
        # `--prefix <dir>/` reads the tree UNDER that subdirectory — measured: it
        # creates sub/new/ and touches nothing else. Scoped, so no whole-tree note.
        ("read-tree scoped by --prefix", f"git read-tree --prefix=sub/new/ -u {_OLD}", False),
        # read-tree's RESULT comes from its LAST tree, so HEAD in first position is
        # not the harmless "put things back" it is for checkout — measured: with a
        # clean refreshed index `-m -u HEAD <old>` succeeds and rewinds to <old>.
        ("read-tree two-tree form", f"git read-tree -m -u HEAD {_OLD}", True),
        # Interactive hunk-picking writes nothing until a human selects hunks, and
        # a session-driven Bash tool has no interactive stdin to select them with.
        ("checkout patch mode", f"git checkout -p {_OLD} -- .", False),
        ("restore patch mode", f"git restore -p --source={_OLD} .", False),
        # After `--` every token is a pathspec — `--source=old` there is a FILE.
        ("restore of a file named --source=old", "git restore -- --source=old .", False),
        # `-S/--staged` restores the index; `-W/--worktree` is the default when
        # neither is given.
        ("restore staged-only", f"git restore --staged --source={_OLD} .", False),
        ("restore -S short", f"git restore -S --source={_OLD} .", False),
        ("restore staged AND worktree", f"git restore -S -W --source={_OLD} .", True),
        ("restore default is worktree", f"git restore --source={_OLD} .", True),
        # ── controls: unchanged by the rewrite, and each one a real shape ──
        ("whole-tree rewind", f"git checkout {_OLD} -- .", True),
        ("plain local discard", "git checkout -- .", False),
        ("bare pathspec", "git checkout .", False),
        ("HEAD alias rewinds nothing merged", "git checkout HEAD -- .", False),
        ("attached short source", f"git restore -s{_OLD} .", True),
        ("directory pathspec", f"git checkout {_OLD} -- tests/", True),
        ("unrelated", "git status", False),
    ],
)
def test_the_rewind_predicate_over_real_git_spellings(label, cmd, fires):
    got = _gd._tree_rewind_segments(cmd, {"tool_input": {"command": cmd}, "cwd": "/tmp"})
    assert bool(got) == fires, (
        f"{label}: expected fires={fires} for {cmd!r}, got {got!r}. A false POSITIVE "
        "manufactures recurrence evidence for a block; a false NEGATIVE is the "
        "silent reversion this guard was built to make conspicuous."
    )


@pytest.mark.parametrize(
    "label,cmd",
    [
        # Every one of these named an option's VALUE as the commit being restored
        # before the value-taking table existed. Naming the real source is the
        # whole point of the note, so a confidently wrong name is worse than the
        # generic note it replaced.
        ("--index-output <file>", f"git read-tree --reset -u --index-output tmpidx {_OLD}"),
        (
            "--exclude-per-directory <file>",
            f"git read-tree --reset -u --exclude-per-directory .gitignore {_OLD}",
        ),
    ],
)
def test_an_options_value_is_never_named_as_the_rewind_source(label, cmd):
    got = _gd._tree_rewind_segments(cmd, {"tool_input": {"command": cmd}, "cwd": "/tmp"})
    assert got, f"{label}: the rewind was not detected at all"
    assert got[0][0] == _OLD, (
        f"{label}: the note names {got[0][0]!r} as the source, which is an option's "
        f"value, not the commit. Expected {_OLD!r}."
    )


def test_the_value_taking_option_tables_cover_every_rewind_verb():
    """A verb added to the trigger set without a table silently inherits the old
    defect, so the two sets are bound to each other rather than kept in step by
    hand."""
    assert set(_gd._TREE_REWIND_OPTS_WITH_ARG) == set(_gd._TREE_REWIND_VERBS), (
        "every rewind verb needs a measured value-taking-option table (an empty "
        "frozenset is a fine answer, but it must be a stated one)"
    )


# ── the advisory must stay DELIVERABLE, not merely produced ──────────────────
# Claude Code persists a hook payload over ~10,000 chars and hands the model a
# preview instead, so an oversized advisory is an ABSENT one — silently. MEASURED
# with a 40-char cwd: one rewind note is ~1.3 KB, so the payload crossed the cap
# at EIGHT repositories (10,766 chars; seven fit, at 9,430), which is precisely the
# the per-repo note was added to serve. These tests drive the REAL emitter; the
# equivalents lived only in a scratchpad harness before, and a scratchpad E2E is
# what let an unverified recovery command ship twice on this same PR.


def _emit(notes: list[str], capsys, monkeypatch=None) -> str:
    """Raw stdout of the real emitter, so the payload SIZE is what is measured.

    Callers that measure the BOUND pin the store path, because the omission line
    names it and its length therefore changes how many notes fit. Left free, these
    tests inherit conftest's tmp_path store — far longer than the real default —
    which enlarged the reserve, kept one note fewer, and masked an off-by-envelope
    overflow that the live end-to-end run caught at once. A size assertion whose
    answer depends on the test directory's name is not a size assertion.
    """
    if monkeypatch is not None:
        monkeypatch.setattr(_gd, "_snapshot_dir", lambda: "/var/genesis/snapshots")
    _gd._emit_additional_context(notes)
    return capsys.readouterr().out


def _note_terminal() -> str:
    """The tail of a rendered note, used to count notes that FINISHED.

    Derived rather than hardcoded: two tests pinned the literal phrase
    "DELETED restored by the rewind." and broke the moment the note was reworded —
    a test coupled to prose instead of to the property it names.
    """
    return _gd._tree_rewind_note("a" * 40, "/r", "b" * 40)[-45:]


def _rewind_note_fixture() -> tuple[str, str]:
    """One realistic note plus the 12-char sha it must always carry intact.

    The cwd is SYNTHETIC and a fixed width. A real path would put this install's
    home directory — and the username inside it — into a public test fixture, and
    it would also make the measured size below vary by machine, so the assertion
    would pass or fail depending on how long someone's checkout path happens to
    be. 40 characters is the figure the cap arithmetic in `_emit_additional_context`
    is stated against.
    """
    sha = "b" * 40
    return _gd._tree_rewind_note("a" * 40, "/" + "p" * 39, sha), sha[:12]


@pytest.mark.parametrize("repos", [8, 20, 200])
def test_the_advisory_stays_under_the_hook_output_cap(repos, capsys):
    note, _sha = _rewind_note_fixture()
    out = _emit([note] * repos, capsys)
    cap = _gd.hook_output.HOOK_STDOUT_CAP
    assert len(out) <= cap, (
        f"{repos} repositories produced a {len(out)}-char payload, over the "
        f"{cap}-char cap — the harness persists that and the model gets a preview, "
        "so the whole advisory is lost exactly where multi-repo support matters"
    )


def test_every_surviving_note_is_COMPLETE_never_amputated(capsys, monkeypatch):
    """The bound omits WHOLE notes; it must never trim the joined text.

    Every note ends in a recovery command carrying a snapshot sha. A mid-value cut
    would hand the reader a truncated sha — a command that fails, or resolves to a
    different object — dressed as a recovery instruction. An omitted note sends
    them to the snapshot log; an amputated one sends them somewhere wrong.

    Asserted STRUCTURALLY, on two properties that cannot pass by luck, because the
    obvious version of this test did. Checking only that no sha looks truncated
    PASSED against a build with the whole-note selection removed: the writer's
    text-trim had landed mid-prose rather than mid-sha that run, so the assertion
    was a coin-flip on the cut offset and pinned nothing. What holds regardless of
    where a cut would land is (a) the writer's trim marker must be ABSENT — if
    selection did its job the backstop never fires — and (b) every note that
    appears must also END, so started-notes == finished-notes.
    """
    note, sha12 = _rewind_note_fixture()
    out = _emit([note] * 200, capsys, monkeypatch)
    assert len(out) <= _gd.hook_output.HOOK_STDOUT_CAP, (
        f"the emitted payload is {len(out)} chars, over the cap — selection must "
        "budget the ENVELOPE too, not just the joined notes"
    )
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "cap]" not in ctx, (
        "the writer's text-trim marker is present, so the joined text was cut "
        "mid-note — whole-note selection is supposed to make that unreachable here"
    )
    started = ctx.count("WHOLE-TREE REWIND")
    finished = ctx.count(_note_terminal())
    assert started == finished, (
        f"{started} notes started but only {finished} finished — a note was cut off"
    )
    found = re.findall(r"git stash apply --index ([0-9a-f]+)", ctx)
    assert found and all(s == sha12 for s in found), (
        f"a recovery sha was truncated: {[s for s in found if s != sha12]}"
    )


def test_the_omission_is_stated_with_an_accurate_count(capsys):
    """A silent drop would be the same defect as the cap, one layer up."""
    note, _sha = _rewind_note_fixture()
    ctx = json.loads(_emit([note] * 20, capsys))["hookSpecificOutput"]["additionalContext"]
    kept = ctx.count("WHOLE-TREE REWIND")
    m = re.search(r"…and (\d+) more repository note", ctx)
    assert m, f"notes were dropped with no omission line. Context: {ctx[-400:]}"
    assert kept + int(m.group(1)) == 20, (
        f"the count lies: {kept} notes shown + {m.group(1)} claimed omitted != 20"
    )
    assert "GENESIS_DISCARD_SNAPSHOT_DIR" in ctx or "snapshot log" in ctx, (
        "an omission must say where the dropped recovery shas can be read"
    )


def test_all_notes_survive_when_they_fit(capsys):
    """The control: the bound must not fire on the ordinary case.

    Without this, a bound that dropped everything would pass every test above.
    """
    note, _sha = _rewind_note_fixture()
    ctx = json.loads(_emit([note] * 3, capsys))["hookSpecificOutput"]["additionalContext"]
    assert ctx.count("WHOLE-TREE REWIND") == 3
    assert "omitted" not in ctx, "nothing was dropped, so nothing may claim it was"


def test_a_single_oversized_note_still_leaves_parseable_json(capsys):
    """The envelope backstop, for the one case selection cannot fix.

    Dropping the only note would be a worse answer than a trimmed one, so it is
    kept and `print_json_bounded` pays for the overage out of the text — the JSON
    must still parse, because a preview instead of JSON loses the payload whole.
    """
    note, _sha = _rewind_note_fixture()
    giant = note + "X" * (_gd.hook_output.DEFAULT_BUDGET + 500)
    out = _emit([giant], capsys)
    assert len(out) <= _gd.hook_output.HOOK_STDOUT_CAP
    payload = json.loads(out)  # must not raise
    assert payload["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "cap]" in payload["hookSpecificOutput"]["additionalContext"], (
        "a trimmed payload must say it was trimmed"
    )


def test_the_bound_degrades_to_the_old_behaviour_if_hook_output_is_absent(monkeypatch, capsys):
    """With the writer unavailable, notes go out unbounded — a lost advisory at
    worst, which is the pre-change behaviour, never a permitted `git clean`.

    SCOPE, because the first version of this docstring overclaimed: this patches the
    already-imported attribute to None, so it pins the RUNTIME degrade, not the
    import guard. Whether a missing module can disarm the block is a different
    question and is tested by driving the real script with the sibling deleted — see
    `test_a_missing_GUARDED_sibling_cannot_disarm_the_clean_block`.
    """
    note, _sha = _rewind_note_fixture()
    monkeypatch.setattr(_gd, "hook_output", None)
    ctx = json.loads(_emit([note] * 20, capsys))["hookSpecificOutput"]["additionalContext"]
    assert ctx.count("WHOLE-TREE REWIND") == 20, (
        "with the writer absent the guard must still emit every note — degrading to "
        "silence would make a cosmetic import load-bearing"
    )


def test_an_oversized_FIRST_note_does_not_silently_drop_the_rest(capsys):
    """The bound's contract is that it never drops anything silently.

    It broke that once: the loop reserves room for the omission line, so when the
    FIRST note costs more than `budget - reserve` it breaks with nothing kept, and
    the `not kept` branch returned `notes[:1]` — discarding every remaining note
    with no omission line. Its comment assumed "a single note exceeds the budget",
    but the condition tested was `budget - reserve` and it never looked at how many
    notes there were. MEASURED: a 9,680-char first note (UNDER the 9,800 budget)
    plus a second note emitted one note and no omission line.
    """
    second = "SECOND-NOTE-SENTINEL"
    big = "X" * (_gd.hook_output.DEFAULT_BUDGET - 120)
    ctx = json.loads(_emit([big, second], capsys))["hookSpecificOutput"]["additionalContext"]
    m = re.search(r"…and (\d+) more repository note", ctx)
    assert m, (
        "a note was dropped with no omission line — the one thing this bound "
        f"promises it never does. Context tail: {ctx[-300:]!r}"
    )
    assert int(m.group(1)) == 1, f"omission count should be 1, said {m.group(1)}"


@pytest.mark.parametrize(
    "label,cmd",
    [
        # `-s` consumed the separator as its value, so `--` was reported as the
        # commit being restored.
        ("restore with no source operand", "git restore -s -- ."),
        # `-b` creates a branch; git rejects that alongside a pathspec, so the
        # command cannot run — and a tripwire row for an impossible event inflates
        # the count that would justify escalating to a block.
        ("branch-creating checkout", f"git checkout -b tmp {_OLD} -- ."),
        ("branch-creating switch", f"git switch -C tmp {_OLD} -- ."),
    ],
)
def test_a_command_git_itself_rejects_is_not_recorded_as_a_rewind(label, cmd):
    got = _gd._tree_rewind_segments(cmd, {"tool_input": {"command": cmd}, "cwd": "/tmp"})
    assert not got, f"{label}: {cmd!r} reported {got!r}"


def test_the_global_C_flag_is_not_read_as_switchs_force_create():
    """git's global `-C <dir>` and `switch -C <branch>` share a spelling.

    The branch-creating exclusion was first written against the whole argv, which
    made `git -C <path> checkout <sha> -- .` look like a branch creation and stopped
    warning about it — a false NEGATIVE on the multi-repository shape this feature
    exists for. The exclusion is scoped to the verb's own options; this pins that.
    """
    cmd = f"git -C /tmp/somewhere checkout {_OLD} -- ."
    got = _gd._tree_rewind_segments(cmd, {"tool_input": {"command": cmd}, "cwd": "/tmp"})
    assert got and got[0][0] == _OLD, f"the rewind must still be detected, got {got!r}"


@pytest.mark.parametrize(
    "label,cmd,broad",
    [
        ("whole worktree", f"git checkout {_OLD} -- .", True),
        ("magic root pathspec", f"git checkout {_OLD} -- :/", True),
        ("read-tree has no pathspec", f"git read-tree --reset -u {_OLD}", True),
        ("one directory", f"git checkout {_OLD} -- docs/", False),
    ],
)
def test_the_event_row_records_whether_the_pathspec_was_the_WHOLE_tree(label, cmd, broad):
    """The recurrence count has to be able to exclude routine work.

    `git checkout origin/main -- docs/` fires this predicate by design — the note's
    own prose says "UNDER THE PATHSPEC YOU GAVE" for that reason — but it is routine
    work, not the incident. The row cannot carry the command text (durable log, and
    a Bash payload can carry credentials), so breadth is carried out of the
    predicate as one boolean that argv already proves.
    """
    got = _gd._tree_rewind_segments(cmd, {"tool_input": {"command": cmd}, "cwd": "/tmp"})
    assert got, f"{label}: not detected"
    assert got[0][3] is broad, f"{label}: broad={got[0][3]}, expected {broad}"


def test_an_unparseable_submodule_command_is_not_told_to_use_a_dead_escape(monkeypatch):
    """The bounds-induced refusal must not promise an override it cannot honour.

    That early return precedes the per-segment override check, so the generic
    block message's closing "append `# discard-override` to proceed" was a route
    that did not exist — MEASURED: an over-length command carrying the sigil still
    returned 2 with that text, leaving no way forward at all. The `clean` twin
    already said the right thing.
    """
    cmd = (
        'echo "'
        + "x" * (shell_parse.MAX_COMMAND_CHARS + 64)
        + '" && git checkout --recurse-submodules .  # discard-override'
    )
    _segs, blind = shell_parse.analyze_checked(cmd)
    assert blind is not None and blind.bounds_induced, "fixture must trip the bound"
    monkeypatch.setattr(
        _gd, "read_payload", lambda: {"tool_input": {"command": cmd}, "cwd": "/tmp"}
    )
    assert _gd.main() == 2, "an unreadable submodule-recursive command must refuse"
    msg = _gd._SUBMODULE_PARSE_FAILED_MSG
    assert "OWN line" in msg, "it must name the route that actually works"
    assert "NOT honoured here" in msg, (
        "and say plainly that the override does not apply on this path, rather than offering it"
    )


def test_an_untokenizable_clean_mentioning_command_is_still_ALLOWED():
    """The fail-closed branch is narrowed to BOUNDS-induced blindness on purpose.

    Nothing pinned that narrowing: widening the branch back to `if blind is not
    None:` left the whole suite green while re-introducing an over-block on the
    measured ~15% of real clean-mentioning commands that merely fail to tokenize
    (an apostrophe in a commit message is the common case). The fixture asserts its
    own blindness KIND, so it cannot quietly start testing the other path.
    """
    cmd = """git commit -m $'don\\'t clean' && git status"""
    _segs, blind = shell_parse.analyze_checked(cmd)
    assert blind is not None and not blind.bounds_induced, (
        "fixture must be untokenizable-but-not-bounds-induced, or it proves nothing"
    )
    assert _gd._clean_violation(cmd) is None, (
        "an untokenizable command that merely MENTIONS clean must not be refused"
    )


@pytest.mark.parametrize("sibling", ["audit_jsonl.py", "discarded_write.py", "hook_output.py"])
def test_a_missing_GUARDED_sibling_cannot_disarm_the_clean_block(tmp_path, sibling):
    """Drive the REAL script with one guarded sibling deleted; the BLOCK must hold.

    The three guarded imports are this module's most safety-critical property — a
    module-load exception exits 1, which Claude Code reads as NON-blocking, so an
    unimportable helper would let an unrecoverable `git clean -fdx` through. Nothing
    tested it: deleting all three `try/except` wrappers is behaviour-identical under
    pytest, because pytest imports the module from an intact tree. So the property
    was unfalsifiable by this suite until this test, which is what let the gap sit.

    A subprocess is the only honest way to ask: the failure happens at module load,
    before any in-process monkeypatch could apply.

    HONEST LIMIT, stated rather than implied: `hook_input` and `shell_parse` are
    imported WITHOUT a guard, so deleting either still exits 1 — and for the
    submodule-recursive block there is no shell-side floor behind it. That is a
    pre-existing shape this module shares with the other guards that import
    `shell_parse` bare, and closing it is the import-time fail-closed work tracked
    separately; it is deliberately NOT papered over here by limiting this test to
    the three modules that ARE guarded.
    """
    hooks = tmp_path / "hooks"
    shutil.copytree(_HOOKS, hooks)
    (hooks / sibling).unlink()

    payload = json.dumps({"tool_input": {"command": "git clean -fdx"}, "cwd": str(tmp_path)})
    proc = subprocess.run(
        [sys.executable, str(hooks / "git_discard_guard.py")],
        input=payload,
        capture_output=True,
        text=True,
        env={**os.environ, "GENESIS_DISCARD_SNAPSHOT_DIR": str(tmp_path / "snaps")},
    )
    assert proc.returncode == 2, (
        f"with {sibling} missing the guard exited {proc.returncode}. Only 2 BLOCKS; "
        "anything else is read as a non-blocking error and the clean RUNS, deleting "
        f"untracked files the snapshot net cannot restore. stderr: {proc.stderr[:400]}"
    )


def test_the_positive_control_an_intact_tree_allows_a_dry_run_clean(tmp_path):
    """The control for the three cells above: a guard that blocked everything would
    pass them. An intact copy of the tree must still allow the dry-run form."""
    hooks = tmp_path / "hooks"
    shutil.copytree(_HOOKS, hooks)
    payload = json.dumps({"tool_input": {"command": _CLEAN_DRY_RUN}, "cwd": str(tmp_path)})
    proc = subprocess.run(
        [sys.executable, str(hooks / "git_discard_guard.py")],
        input=payload,
        capture_output=True,
        text=True,
        env={**os.environ, "GENESIS_DISCARD_SNAPSHOT_DIR": str(tmp_path / "snaps")},
    )
    assert proc.returncode == 0, (
        f"an intact tree refused a dry-run clean (rc={proc.returncode}) — the cells "
        f"above would then prove nothing. stderr: {proc.stderr[:400]}"
    )


def test_rewind_warnings_come_BEFORE_routine_snapshot_notes(
    two_repos, snap_log, monkeypatch, capsys
):
    """Ordering is load-bearing, not presentation: the bound keeps a PREFIX.

    Rewind warnings were appended after the routine snapshot notes, so a tight
    budget kept the furniture and dropped the warnings — MEASURED (Codex P1,
    round 2): 20 dirty repos + 3 rewinds kept 13 routine notes and ZERO rewind
    warnings, recreating the pre-change failure where the model never learns
    merged work was reverted. Ordering also decides what an omission costs: a
    dropped snapshot note's content (cwd + sha) is in the snapshot log the
    remainder line points at; a dropped rewind warning is recorded nowhere else.

    Pinned at the emit level with two repos — the prefix-retention property is
    already pinned separately, and prefix-retention plus warnings-first is what
    closes the P1, so this does not need twenty repositories to prove it.
    """
    a, b, b_old = two_repos
    cmd = f"git -C {a} checkout -- tracked.py && git -C {b} checkout {b_old} -- ."
    monkeypatch.setattr(
        _gd, "read_payload", lambda: {"tool_input": {"command": cmd}, "cwd": str(a.parent)}
    )
    assert _gd.main() == 0
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    first = ctx.splitlines()[0]
    assert "WHOLE-TREE REWIND" in first, (
        f"the rewind warning must be the FIRST note, not appended after the "
        f"routine ones. First line was: {first[:120]!r}"
    )
    assert "snapshotted the worktree" in ctx, (
        "control: the routine notes must still be there — ordering, not omission"
    )


def test_notes_are_sized_by_their_SERIALIZED_cost(capsys, monkeypatch):
    """The payload ships through json.dumps, so raw length under-counts.

    A quote or backslash becomes two characters and a control character six —
    MEASURED (Codex P2, round 2): a note whose cwd carried JSON metacharacters
    cost 1,461 raw and 1,895 serialised, so raw-cost selection approved
    "complete" notes the writer then cut mid-note, removing exactly the recovery
    sha the whole-note selector exists to protect.
    """
    evil_cwd = "/" + '"\\\\' * 60 + "路径" * 30
    note = _gd._tree_rewind_note("a" * 40, evil_cwd, "b" * 40)
    out = _emit([note] * 12, capsys, monkeypatch)
    assert len(out) <= _gd.hook_output.HOOK_STDOUT_CAP
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "cap]" not in ctx, (
        "the writer's trim marker fired, so selection approved notes whose "
        "serialised cost did not fit — a mid-note cut one layer down"
    )
    started = ctx.count("WHOLE-TREE REWIND")
    finished = ctx.count(_note_terminal())
    assert started == finished and started > 0, (
        f"{started} notes started, {finished} finished — a metachar-heavy note was amputated"
    )


def test_a_directory_pathspec_without_a_trailing_slash_is_detected(repo, monkeypatch):
    """`-- src` rewinds src/ recursively, and that spelling is the natural one.

    MEASURED on git 2.43: `git checkout <old> -- src` put every tracked file under
    src/ back to the old content while a file outside src/ stayed untouched — a real,
    scoped rewind. The predicate recognised a directory only by a trailing slash, so
    `src` and `./src` — the two ways anyone would actually type it — were silent.

    Resolved with one `os.path.isdir` against the segment's repository. That is a
    deliberate departure from this module's argv-only-no-filesystem rule, and the
    distinction is that the rule exists to stop it MODELLING git's semantics: a stat
    is a fact, not a model, and not a subprocess.
    """
    (repo / "src").mkdir()
    (repo / "src" / "f.py").write_text("v1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add src")
    old = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "src" / "f.py").write_text("v2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "upstream")

    for spelling in ("src", "./src", "src/"):
        cmd = f"git checkout {old} -- {spelling}"
        got = _gd._tree_rewind_segments(
            cmd, {"tool_input": {"command": cmd}, "cwd": str(repo)}
        )
        assert got, f"{spelling!r} is a directory rewind and must be detected"
        assert got[0][3] is False, (
            f"{spelling!r} is SCOPED to one directory, so it must not be recorded broad"
        )

    # THE SIBLING, and it is the half that bit: teaching the breadth predicate to
    # recognise a slashless directory while the SOURCE predicate still asked only
    # about a trailing slash made them disagree, and the disagreement was immediately
    # a false positive — MEASURED, `git checkout src` (rc=0, an ordinary discard
    # restoring from the index, reverting nothing merged) fired with source `src`.
    # Both ends of one rule move together or neither does.
    for discard in ("git checkout src", "git checkout src/", "git checkout -- src"):
        assert not _gd._tree_rewind_segments(
            discard, {"tool_input": {"command": discard}, "cwd": str(repo)}
        ), f"{discard!r} is an ordinary local discard and must stay silent"

    plain = f"git checkout {old} -- src/f.py"
    assert not _gd._tree_rewind_segments(
        plain, {"tool_input": {"command": plain}, "cwd": str(repo)}
    ), "a single FILE is not a directory rewind — the stat must discriminate, not blanket"


def test_a_missing_path_falls_back_to_the_trailing_slash_rule(repo):
    """The stat fails toward FALSE: a miss is the status quo, never a false note."""
    old = _git(repo, "rev-parse", "HEAD").strip()
    cmd = f"git checkout {old} -- no_such_dir"
    assert not _gd._tree_rewind_segments(
        cmd, {"tool_input": {"command": cmd}, "cwd": str(repo)}
    ), "a path that is not there must not produce a note"
    assert _gd._operand_is_directory("src", None) is False, (
        "no cwd means no answer, and the answer must be the safe one"
    )


def test_the_event_identity_distinguishes_SEPARATE_INVOCATIONS(repo, snap_log, monkeypatch, capsys):
    """Two runs of the same command are two attempts; the count must see both.

    The digest was source+cwd+command, which made an identical rewind run again next
    week collapse into the one event forever — so the tripwire could not measure the
    recurrence it exists for. `tool_use_id` is MEASURED present in the PreToolUse
    payload (58 captured firings on this install) and is shared by the two hook
    wirings, because `bash_safety_hook.sh` pipes the verbatim payload through. So it
    separates invocations while preserving cross-wiring dedup.
    """
    old = _git(repo, "rev-parse", "HEAD").strip()
    cmd = f"git checkout {old} -- ."
    seen = []
    for call_id in ("toolu_FIRST", "toolu_SECOND", "toolu_SECOND"):
        (repo / "tracked.py").write_text(f"dirty-{call_id}\n")
        monkeypatch.setattr(
            _gd,
            "read_payload",
            lambda cid=call_id: {
                "tool_input": {"command": cmd},
                "cwd": str(repo),
                "tool_use_id": cid,
            },
        )
        assert _gd.main() == 0
        capsys.readouterr()
        seen.append({r["event"] for r in _rewind_rows(snap_log)})

    ids = {r["event"] for r in _rewind_rows(snap_log)}
    assert len(ids) == 2, (
        f"two distinct invocations of the same command must be two events, and the "
        f"SAME tool_use_id seen twice (the two hook wirings) must be one; got {ids}"
    )


def test_the_identity_degrades_when_the_payload_carries_no_invocation_id(
    repo, snap_log, monkeypatch, capsys
):
    """One captured payload shape lacks `tool_use_id`, so absence must still work.

    It degrades to the old source+cwd+command digest: coarse for counting, but still
    correct for the property that must not break — the two hook wirings of one
    command agreeing on one row.
    """
    old = _git(repo, "rev-parse", "HEAD").strip()
    cmd = f"git checkout {old} -- ."
    (repo / "tracked.py").write_text("dirty\n")
    monkeypatch.setattr(
        _gd, "read_payload", lambda: {"tool_input": {"command": cmd}, "cwd": str(repo)}
    )
    assert _gd.main() == 0
    capsys.readouterr()
    rows = _rewind_rows(snap_log)
    assert len(rows) == 1 and re.fullmatch(r"[0-9a-f]{16}", rows[0]["event"]), (
        "a payload without an invocation id must still produce one digested event row"
    )


def test_the_count_is_documented_as_ATTEMPTS_not_completed_rewinds():
    """PreToolUse runs BEFORE bash, so a row can precede a rewind that never happens.

    MEASURED: `false && git checkout <old> -- .` changes nothing (shell short-circuit)
    and an invalid ref exits 128 having changed nothing — both still write a row.
    Narrowing the predicate cannot close that, because the gap is WHEN the hook runs.
    The honest fix is to say so wherever the count is described, since the count's
    only purpose is justifying an escalation to a hard block.
    """
    src_text = Path(_gd.__file__).read_text()
    assert "COUNT ATTEMPTS, NOT ROWS" in src_text, (
        "the counting recipe must name what it counts"
    )
    assert "UPPER BOUND" in src_text, (
        "and must say the count is an upper bound on rewinds that happened"
    )
    cmd = "false && git checkout abc -- ."
    assert _gd._tree_rewind_segments(cmd, {"tool_input": {"command": cmd}, "cwd": "/tmp"}), (
        "the predicate still fires on a short-circuited command — that is the "
        "behaviour the documentation now describes rather than pretends away"
    )


def test_a_revision_before_the_separator_is_never_mistaken_for_a_pathspec(repo):
    """`--` removes the ambiguity, so the directory stat must not apply before it.

    MEASURED: with a BRANCH and a DIRECTORY both named `src`, `git checkout src -- .`
    really rewinds the worktree from that branch — and the stat that fixed the
    slashless-directory miss classified `src` as a pathspec and went silent. A false
    NEGATIVE introduced by a false-positive fix, in the direction that matters.

    git reads everything before `--` as a revision and everything after as a
    pathspec, so when a separator is present there is nothing to disambiguate and
    nothing to stat. The stat earns its place only in the genuinely ambiguous case
    git itself has to resolve.
    """
    (repo / "src").mkdir()
    (repo / "src" / "f.py").write_text("v1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add src dir")
    _git(repo, "branch", "src")  # a branch whose name collides with the directory
    _git(repo, "checkout", "-q", "src")
    (repo / "tracked.py").write_text("ON-BRANCH-src\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "work on branch src")
    _git(repo, "checkout", "-q", "main")

    cmd = "git checkout src -- ."
    got = _gd._tree_rewind_segments(cmd, {"tool_input": {"command": cmd}, "cwd": str(repo)})
    assert got and got[0][0] == "src", (
        f"a rewind from branch `src` must be detected even though a src/ directory "
        f"exists — the separator settles it. Got {got!r}"
    )
    assert got[0][3] is True, "the pathspec is `.`, so it is a whole-tree rewind"

    # The control, and the reason the stat exists at all: with NO separator the same
    # token is ambiguous, and an ordinary directory discard must stay silent.
    discard = "git checkout src"
    assert not _gd._tree_rewind_segments(
        discard, {"tool_input": {"command": discard}, "cwd": str(repo)}
    ), "without a separator `src` is a pathspec — an ordinary discard, not a rewind"


def test_a_file_named_like_a_mode_flag_does_not_suppress_the_warning(repo):
    """Mode flags are read from the VERB's options, never from the whole argv.

    MEASURED: `git checkout <old> -- -p .` rewinds the whole worktree while a file
    named `-p` sits in the pathspec list; matching `-p` over argv read that as
    interactive patch mode and suppressed the note entirely. Same collision class as
    git's global `-C` sharing a spelling with switch's force-create `-C`.
    """
    old = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "-p").write_text("awkward name\n")
    (repo / "--staged").write_text("awkward name\n")
    _git(repo, "add", "--", "-p", "--staged")
    _git(repo, "commit", "-m", "files named like flags")

    for cmd in (
        f"git checkout {old} -- -p .",
        f"git restore --source={old} -- --staged .",
    ):
        got = _gd._tree_rewind_segments(
            cmd, {"tool_input": {"command": cmd}, "cwd": str(repo)}
        )
        assert got and got[0][0] == old, (
            f"a pathspec that looks like a mode flag must not silence a real rewind: "
            f"{cmd!r} gave {got!r}"
        )

    # Controls: the SAME spellings as real verb options must still suppress, or this
    # test would pass against a build that simply stopped checking modes.
    for cmd in (f"git checkout -p {old} -- .", f"git restore -S --source={old} ."):
        assert not _gd._tree_rewind_segments(
            cmd, {"tool_input": {"command": cmd}, "cwd": str(repo)}
        ), f"{cmd!r} is interactive or index-only and must stay silent"


# ── the tripwire: recurrence must be COUNTABLE, not argued ───────────────────
# The owner's decision (2026-09-06) was "loud note now, block if it recurs". That
# only means something if recurrence can be measured, so every match writes a
# flag to the snapshot log.


def test_a_rewind_is_flagged_in_the_snapshot_log(repo, snap_log, monkeypatch, capsys):
    old = _git(repo, "rev-parse", "HEAD").strip()
    _rewind_ctx(f"git checkout {old} -- .", repo, monkeypatch, capsys)
    assert len(_rewind_rows(snap_log)) == 1


def test_an_ordinary_discard_is_not_flagged(repo, snap_log, monkeypatch, capsys):
    _rewind_ctx("git checkout -- tracked.py", repo, monkeypatch, capsys)
    assert _snapshot_rows(snap_log), "the ordinary snapshot must still be recorded"
    assert _rewind_rows(snap_log) == [], "the flag must mean something when present"


def test_the_log_row_stays_metadata_only(repo, snap_log, monkeypatch, capsys):
    """The row deliberately carries no command text and no commit-ish: this log is
    durable and a Bash payload can carry credentials."""
    old = _git(repo, "rev-parse", "HEAD").strip()
    _rewind_ctx(f"git checkout {old} -- .", repo, monkeypatch, capsys)
    assert set(_snapshot_rows(snap_log)[-1]) == {"ts", "cwd", "sha"}
    row = _rewind_rows(snap_log)[-1]
    assert set(row) == {"ts", "cwd", "tree_rewind", "broad", "event", "snapshot"}
    assert re.fullmatch(r"[0-9a-f]{16}", row["event"]), (
        "the event identity must be a DIGEST — sixteen hex reverses to nothing, "
        "where any fragment of the command could carry a credential"
    )
    assert old not in json.dumps(_rows(snap_log))


def test_a_rewind_from_a_CLEAN_worktree_is_still_counted(repo, snap_log, monkeypatch, capsys):
    """The tripwire must not depend on a snapshot having been possible.

    This is the case that was warned about and never counted, and no test reached
    it because the shared `_rewind_ctx` helper dirties the tree first. MEASURED on
    the pre-fix code: `git stash create` returns empty on a clean worktree, so the
    loop `continue`d before writing any row — zero rows, while the note fired.

    It is also the likeliest setting for the incident this exists to measure. The
    2026-08-31 reversion happened while applying someone's changes onto a moved
    base; you need no local edits of your own for that, so the most dangerous
    shape was precisely the uncounted one. Budget skips and snapshot failures were
    dropped the same way.
    """
    old = _git(repo, "rev-parse", "HEAD").strip()
    assert not _git(repo, "status", "--porcelain").strip(), (
        "this fixture must be CLEAN — a dirty tree tests the other path"
    )
    monkeypatch.setattr(
        _gd,
        "read_payload",
        lambda: {"tool_input": {"command": f"git checkout {old} -- ."}, "cwd": str(repo)},
    )
    assert _gd.main() == 0
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "WHOLE-TREE REWIND" in ctx, "the note must still fire"
    assert _snapshot_rows(snap_log) == [], (
        "a clean worktree cannot be snapshotted — if this changes, the premise of "
        "this test has moved and the assertion below is measuring something else"
    )
    events = _rewind_rows(snap_log)
    assert len(events) == 1, (
        "the rewind must be COUNTED even with no recovery point — otherwise the "
        "count is a measure of how often stash-create happened to succeed"
    )
    assert events[0]["snapshot"] is None, (
        "and it must say there was no recovery point, rather than implying one"
    )


def test_two_rewinds_of_one_repo_from_different_sources_are_two_events(
    repo, snap_log, monkeypatch, capsys
):
    """Counting events, not repositories.

    A single boolean on one snapshot row collapsed any number of rewinds in a repo
    into one. Distinct sources are distinct events; the SAME source twice in one
    payload is deliberately one, since that is one intent spelled twice.
    """
    first = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "second.py").write_text("x\n")
    _git(repo, "add", "second.py")
    _git(repo, "commit", "-m", "second")
    second = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "tracked.py").write_text("dirty\n")
    cmd = f"git checkout {first} -- . && git checkout {second} -- ."
    monkeypatch.setattr(
        _gd, "read_payload", lambda: {"tool_input": {"command": cmd}, "cwd": str(repo)}
    )
    assert _gd.main() == 0
    capsys.readouterr()
    assert len(_rewind_rows(snap_log)) == 2, "two distinct sources are two events"

    same = f"git checkout {first} -- . && git checkout {first} -- ."
    monkeypatch.setattr(
        _gd, "read_payload", lambda: {"tool_input": {"command": same}, "cwd": str(repo)}
    )
    assert _gd.main() == 0
    capsys.readouterr()
    assert len(_rewind_rows(snap_log)) == 3, (
        "the same source twice in one payload is ONE further event, not two"
    )


def test_an_overridden_rewind_is_silent_but_still_counted(repo, snap_log, monkeypatch, capsys):
    """`# discard-override` says "I know" — so drop the note, but still record it.
    Whether to escalate to a block is a question about how often this HAPPENS, and
    an acknowledged rewind is still a rewind."""
    old = _git(repo, "rev-parse", "HEAD").strip()
    ctx = _rewind_ctx(f"git checkout {old} -- .  # discard-override", repo, monkeypatch, capsys)
    assert "WHOLE-TREE REWIND" not in ctx
    assert len(_rewind_rows(snap_log)) == 1


def test_a_broken_rewind_predicate_never_costs_the_snapshot(repo, snap_log, monkeypatch, capsys):
    """The snapshot is the actual recovery mechanism; this note is a courtesy on
    top. A bug in the courtesy must not take the mechanism down with it."""
    old = _git(repo, "rev-parse", "HEAD").strip()
    # ARITY MATTERS: production calls `_tree_rewind_segments(cmd, payload)`. A
    # one-argument stand-in raised TypeError at the CALL SITE, before its body ran,
    # so the RuntimeError this test names never happened — and both land in the same
    # `except Exception`, so it passed while testing a different failure entirely.
    # A stand-in that does not match the signature tests the signature.
    monkeypatch.setattr(
        _gd,
        "_tree_rewind_segments",
        lambda cmd, payload: (_ for _ in ()).throw(RuntimeError("boom")),
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

    by_cwd = {row["cwd"]: row["sha"] for row in _snapshot_rows(snap_log)}
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
    flagged = {row["cwd"] for row in _rewind_rows(snap_log)}
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


def test_an_unavailable_writer_says_so_on_stderr(repo, snap_log, monkeypatch, capsys):
    """The guard's own message must not point at evidence it never emits.

    `_record_snapshots` tells the operator "NOT logged — see the [audit-log] line
    on stderr for why". When `audit_jsonl` failed to import, no such line existed:
    the durable recovery pointer was dropped and the only explanation offered was
    a reference to a line that was never printed (Codex P2, PR #1609). The sibling
    condition in `git_push_guard._flush_overrides` already reports itself; this is
    the other half of that rule.
    """
    (repo / "tracked.py").write_text("dirty\n")
    monkeypatch.setattr(_gd, "audit_jsonl", None)
    notes = _gd._record_snapshots("git checkout -- tracked.py", {"cwd": str(repo)})
    captured = capsys.readouterr()
    assert notes and "NOT logged" in notes[0], notes
    assert "[audit-log]" in captured.err, (
        "the note sends the operator to an [audit-log] line that was never printed"
    )
    assert _snap_files(snap_log) == []


def test_the_superseded_file_knob_is_reported_not_silently_ignored(snap_log, monkeypatch, capsys):
    """`GENESIS_DISCARD_SNAPSHOT_LOG` named a FILE and this store is a DIRECTORY.

    An install that set the old knob would otherwise keep writing to the new
    default while its tooling read the old path — snapshots appearing to have
    stopped, with nothing said (Codex P2, PR #1609). Deliberately NOT translated:
    a file path does not carry a correct directory, and inventing one would put
    recovery records somewhere nobody chose. The resolved directory must therefore
    still be the one the NEW knob names.
    """
    monkeypatch.setenv("GENESIS_DISCARD_SNAPSHOT_LOG", "/tmp/legacy-snapshots.jsonl")
    resolved = _gd._snapshot_dir()
    err = capsys.readouterr().err
    assert "GENESIS_DISCARD_SNAPSHOT_LOG" in err, "the superseded knob was dropped in silence"
    assert "GENESIS_DISCARD_SNAPSHOT_DIR" in err, "the notice does not name the replacement"
    assert resolved == str(snap_log), "the legacy value must not steer the store"


def test_no_notice_when_the_superseded_knob_is_unset(snap_log, monkeypatch, capsys):
    """The control. A notice on every ordinary run is a notice nobody reads."""
    monkeypatch.delenv("GENESIS_DISCARD_SNAPSHOT_LOG", raising=False)
    assert _gd._snapshot_dir() == str(snap_log)
    assert "GENESIS_DISCARD_SNAPSHOT_LOG" not in capsys.readouterr().err
