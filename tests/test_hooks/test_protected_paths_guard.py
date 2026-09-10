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

# The cap is READ from the parser, never restated here: a fixture built from a
# literal length stops crossing the bound the moment the bound moves, and a test
# that no longer reaches its subject goes on passing for another reason.
#
# Imported by PATH rather than by `spec_from_file_location` + `module_from_spec`,
# which a sibling test uses and which does NOT register the module in `sys.modules`:
# `shell_parse` defines a dataclass, and `@dataclass` looks its own module up there,
# so that form raises AttributeError at COLLECTION unless something else happened to
# import shell_parse first. The sibling gets away with it only because it loads a
# guard that imports shell_parse normally one line earlier.
sys.path.insert(0, str(_WORKTREE / "scripts" / "hooks"))
import shell_parse  # noqa: E402


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


class TestShellVariableOperands:
    """REGRESSION (Codex P1, 2026-08-02): a protected target assigned to a
    shell-local variable in the same command — `TARGET=~/genesis/data; rm -rf
    "$TARGET"` — is invisible to expandvars, so the operand `$TARGET` didn't
    match; bash then deletes the DB dir. An rm operand carrying an unresolved
    `$var` now triggers a WHOLE-command substring fallback (the literal is in
    the assignment segment)."""

    def test_var_assignment_then_rm_blocks(self, fake_home):
        r = _run(f'TARGET={H}/genesis/data; rm -rf "$TARGET"', fake_home)
        assert r.returncode == 2

    def test_var_assignment_db_file_blocks(self, fake_home):
        r = _run(f'T={H}/genesis/data/genesis.db; rm -f "$T"', fake_home)
        assert r.returncode == 2

    def test_unresolved_var_no_protected_mention_allowed(self, fake_home):
        """An opaque $var rm with NO protected path anywhere → allowed."""
        r = _run('rm -rf "$BUILD_DIR"', fake_home)
        assert r.returncode == 0, r.stderr

    def test_resolvable_env_var_to_protected_blocks(self, fake_home):
        """A resolvable env var pointing at a protected dir is caught directly."""
        import os

        env = dict(os.environ)
        env["HOME"] = str(fake_home)
        env["GD"] = f"{fake_home}/genesis/data"
        import json
        import subprocess

        payload = json.dumps({"tool_input": {"command": "rm -rf $GD"}, "tool_name": "Bash"})
        r = subprocess.run(
            [_PYTHON, str(_SCRIPT)], input=payload, capture_output=True, text=True, env=env
        )
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


class TestQuotedParenRedirectTargetRegression:
    """Codex P1 (2026-08-26): shell_parse's quote-blind ``$()`` balancer
    (``_redirect_target_end``) mis-bounded a ``$(…)`` redirect target whose body
    held a QUOTED or ESCAPED ``)``, dumping back into the quote-aware outer word-
    scan mid-string so a following ``&& rm <protected>`` was consumed INTO the
    redirect target — ``analyze()`` then emitted no ``rm`` segment and this guard
    went blind. Verified against the real guard: pre-fix these PASS THROUGH
    (returncode 0); the ``rm`` after the redirect MUST be seen and blocked."""

    def test_single_quoted_paren_target_then_rm_dir_blocks(self, fake_home):
        r = _run(f"echo ok 2>$(printf ')') && rm -rf {H}/genesis/data", fake_home)
        assert r.returncode != 0, (
            f"protected-dir rm slipped past guard: out={r.stdout!r} err={r.stderr!r}"
        )

    def test_codex_exact_db_file_deletion_blocks(self, fake_home):
        # Codex's exact reported command form (a specific protected FILE).
        r = _run(f"echo ok 2>$(printf ')') && rm {H}/genesis/data/genesis.db", fake_home)
        assert r.returncode != 0, (
            f"production-DB deletion slipped past guard: out={r.stdout!r} err={r.stderr!r}"
        )

    def test_double_quoted_paren_target_then_rm_ancestor_blocks(self, fake_home):
        r = _run(f'echo ok 2>$(echo ")") && rm -rf {H}/genesis', fake_home)
        assert r.returncode != 0, (
            f"protected-ancestor rm slipped past guard: out={r.stdout!r} err={r.stderr!r}"
        )

    def test_quoted_paren_target_without_protected_rm_still_allowed(self, fake_home):
        # The fix must SPLIT the segment correctly, NOT over-block: no protected
        # target here, so the command stays allowed (guards against over-gating).
        r = _run("echo ok 2>$(printf ')') && echo done", fake_home)
        assert r.returncode == 0, (
            f"benign command wrongly blocked: out={r.stdout!r} err={r.stderr!r}"
        )


class TestBoundedParseNeverDowngradesToTheWeakerCheck:
    """A parse stopped by a shell_parse BOUND must not fall back to substring matching.

    The substring fallback is STRICTLY WEAKER than the parse it replaces, and weakest
    exactly where the command is most destructive. The parse catches an ANCESTOR of a
    protected directory (`prot.startswith(expanded + "/")`) and a GLOB over its
    contents (`fnmatch`); a substring test catches NEITHER, because a protected path
    is not a substring of a command naming its PARENT.

    MEASURED at depth 9 before this was fixed — the default branch refused all three,
    this guard refused only the last:

        rm -rf $HOME/genesis        (ancestor)  -> ALLOWED
        rm -rf $HOME/genesis/*      (glob)      -> ALLOWED
        rm -rf $HOME/genesis/data   (exact)     -> refused

    The acceptance test that missed this used the exact path, the one shape the weak
    check does catch, so it passed over a hole that swallowed the whole install.
    """

    # `genesis/data` is on _PROTECTED_RELATIVE; `genesis` is its parent and is NOT.
    @pytest.mark.parametrize(
        "shape,target",
        [
            ("ancestor", f"{H}/genesis"),
            ("glob", f"{H}/genesis/*"),
            ("exact", f"{H}/genesis/data"),
        ],
    )
    def test_a_buried_rm_is_refused_whatever_the_path_shape(self, fake_home, shape, target):
        buried = "$(" * 9 + f"rm -rf {target}" + ")" * 9
        r = _run(buried, fake_home)
        assert r.returncode == 2, (
            f"{shape}: an rm the parser could not read was ALLOWED. The parse is "
            f"bounded, so 'no protected operand found' may mean 'stopped looking' — "
            f"and for this shape the substring fallback cannot see it either.\n"
            f"out={r.stdout!r} err={r.stderr!r}"
        )

    def test_an_over_length_rm_is_refused_too(self, fake_home):
        """The other bound reaches the same weak fallback, so it gets the same test.

        The length is DERIVED from the cap, never written out. This test shipped with
        a literal 40,000 — correct against the 32,768 cap it was written for, and
        silently below the cap once that moved to 49,152 in the same branch. It went
        on passing, because `rm -rf $HOME/genesis` is refused by the ORDINARY parse
        path as an ancestor of a protected directory: a test of the length bound that
        never reached the length bound, green for a reason that had nothing to do with
        its name.

        So the exit code alone cannot attribute the refusal. The stderr assertion is
        what does: only the bounds branch says the command is too long, and a guard
        with no bounds handling at all would still return 2 here.
        """
        over = f'rm -rf {H}/genesis "' + "x" * shell_parse.MAX_COMMAND_CHARS + '"'
        assert len(over) > shell_parse.MAX_COMMAND_CHARS, (
            "the fixture must actually cross the cap, or this test is about the "
            "ordinary parse path wearing the length bound's name"
        )
        r = _run(over, fake_home)
        assert r.returncode == 2, (
            f"an over-length rm naming a protected ancestor was ALLOWED.\n"
            f"out={r.stdout!r} err={r.stderr!r}"
        )
        assert "longer than" in r.stderr, (
            "the refusal did not come from the LENGTH bound — this test can pass on "
            f"the ordinary parse path, which is how it stayed green while vacuous.\n"
            f"out={r.stdout!r} err={r.stderr!r}"
        )

    def test_an_ordinary_unreadable_command_without_rm_is_untouched(self, fake_home):
        """The control on the other side: this guard only ever refuses rm commands.

        Without it, the tests above would pass equally well against a guard that
        refused every unreadable command, which would be a different and much worse
        change than the one being made.
        """
        buried = "$(" * 9 + "echo hello" + ")" * 9
        r = _run(buried, fake_home)
        assert r.returncode == 0, (
            f"a buried non-rm command was blocked: out={r.stdout!r} err={r.stderr!r}"
        )


# ══════════════════════════════════════════════════════════════════════════
# A BLIND SPOT MUST NOT DISARM THE PRECISE SCAN
#
# For a NON-BOUNDS blind spot this guard runs its substring fallback, which its
# own comment calls STRICTLY WEAKER than the segment scan — a substring test
# cannot see an ANCESTOR of a protected directory, nor a GLOB over its contents,
# because neither contains a protected path as a substring. That fallback used
# to RETURN, so any such blind spot skipped the scan entirely.
#
# Latent while `untokenizable` was the only non-bounds cause; reachable once
# there were two. MEASURED base-vs-branch, pairing a verb the shell builds with
# an rm of the PARENT of the production database went BLOCK -> ALLOW. The
# fallback now ADDS to the scan rather than replacing it.
# ══════════════════════════════════════════════════════════════════════════

# A segment whose operation the parser cannot establish, so `analyze_checked`
# reports a non-bounds blind spot. Fixture data, per the sibling guard suites.
_BLINDING_PREFIX = "git pus{h..h} origin main && "


@pytest.mark.parametrize(
    "target",
    [f"{H}/genesis", f"{H}/genesis/*"],
    ids=["ancestor", "glob"],
)
def test_a_blind_spot_does_not_disarm_the_precise_scan(target, fake_home):
    """The two shapes the substring fallback structurally cannot see.

    Each must still be refused when the command ALSO carries a blind spot.

    The control is what makes this mean anything, and it is not decoration: the
    same rm WITHOUT the prefix must already be refused, so a fixture whose
    protected paths failed to resolve — or a guard that refused everything —
    cannot pass this silently. The pair is the assertion.
    """
    plain = _run(f"rm -rf {target}", fake_home)
    assert plain.returncode == 2, (
        "CONTROL: the bare rm was not refused, so this fixture's protected paths "
        f"do not resolve and the assertion below proves nothing.\n{plain.stderr}"
    )
    blinded = _run(f"{_BLINDING_PREFIX}rm -rf {target}", fake_home)
    assert blinded.returncode == 2, (
        "an rm the precise scan catches was ALLOWED because the command also "
        "carried a blind spot — the substring fallback replaced the scan instead "
        f"of adding to it, and it cannot see this shape.\n{blinded.stderr}"
    )


def test_the_blinding_prefix_really_blinds(fake_home):
    """The other half of the control: prove the prefix does what it claims.

    If it stopped raising a blind spot, the tests above would still pass — via
    the ordinary path — while covering nothing. Asserted against the parser
    directly rather than inferred from a verdict.
    """
    _segs, blind = shell_parse.analyze_checked(_BLINDING_PREFIX + "rm -rf /tmp/x")
    assert blind is not None and not blind.bounds_induced, (
        "the prefix no longer produces a NON-BOUNDS blind spot, so the "
        f"fall-through tests above exercise the ordinary path: {blind}"
    )
