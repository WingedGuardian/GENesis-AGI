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


class TestExecutionPrefixesDoNotHideTheTarget:
    """A deletion behind `eval`/`sudo`/… is not a command this guard has read.

    ``analyze()`` reports the PREFIX as the segment's exe, so a protected
    deletion behind one was skipped entirely — no `rm` segment, nothing to
    check, allowed. That was masked for exactly one spelling: before the
    continuation fold landed, a backslash-newline mis-split the command and the
    real deletion fell into its own segment, where the guard did see it.
    Folding correctly removed that accident, which is what exposed the gap.

    The gap is older and wider than the accident. The JOINED spelling was never
    covered at all, on this revision or its parent — so the fold does not create
    a hole, it stops papering over two spellings of one that already existed.

    Two mechanisms, because one is not enough and saying so is the point.

    RESOLUTION handles the prefixes that pass argv through unchanged: they get an
    entry in ``shell_parse._WRAPPER_SPEC``, ``analyze()`` reports the wrapped
    executable, and the ordinary rm/rmdir branch sees the deletion. Exact, and it
    improves every consumer of the parser rather than this guard alone. But a
    table is a WHITELIST and therefore fails OPEN on its complement, which is
    open-ended — ``setpriv``, ``systemd-run``, ``unshare``, ``flock``,
    ``runuser`` and others run a command and are not in it.

    So a CONSERVATIVE CHECK covers the complement: where a segment's executable
    is itself a known prefix (nothing was unwrapped) or its leading word
    re-parses its argument (no table can unwrap it), the guard declines to
    certify and falls back to the whole-command check it already uses for an
    untokenizable command. That only refuses when a protected path is actually
    present, so an ordinary prefixed command is untouched.

    The narrowness is load-bearing and was MEASURED, not assumed. An earlier
    revision fired whenever the leading word was any prefix, and refused four
    ordinary commands — searching a protected directory for a pattern, reading a
    file in one, listing one. Firing on a prefix the parser ALREADY resolved buys
    nothing: it tells us exactly what runs, and if that is not a deletion there is
    nothing to be conservative about.
    """

    @pytest.mark.parametrize(
        "prefix",
        ["eval", "sudo", "env", "command", "exec", "timeout 5", "xargs", "nice"],
    )
    def test_a_protected_deletion_behind_a_prefix_is_refused(self, prefix, fake_home):
        r = _run(f"{prefix} rm -rf {H}/genesis/data", fake_home)
        assert r.returncode == 2, (
            f"{prefix!r} hid a protected deletion: out={r.stdout!r} err={r.stderr!r}"
        )

    def test_the_continuation_spelling_is_refused_too(self, fake_home):
        # The shape the cross-model review reported: the fold joins these, so the
        # guard must not depend on the old mis-split to notice the target.
        r = _run(f"eval \\\nrm -rf {H}/genesis/data", fake_home)
        assert r.returncode == 2, f"out={r.stdout!r} err={r.stderr!r}"

    def test_a_prefix_without_a_protected_path_is_still_allowed(self, fake_home):
        # The over-block control, and it is the load-bearing one: without it,
        # "refuse everything behind a prefix" would score green on every case
        # above while making the guard useless.
        r = _run("eval echo hello && sudo systemctl status a-service", fake_home)
        assert r.returncode == 0, f"benign prefixed command blocked: err={r.stderr!r}"

    def test_a_prefix_deleting_an_unprotected_path_is_still_allowed(self, fake_home):
        r = _run(f"eval rm -rf {H}/scratch/build", fake_home)
        assert r.returncode == 0, f"benign prefixed deletion blocked: err={r.stderr!r}"

    @pytest.mark.parametrize(
        "label,cmd_tpl",
        [
            # Shapes NO wrapper table can resolve: each arrives as one opaque
            # token, or puts something other than the executable at argv[0].
            ("reparsed-single-quoted", "eval '{RM} -rf {H}/genesis/data'"),
            ("reparsed-double-quoted", 'eval "{RM} -rf {H}/genesis/data"'),
            ("compound-body", "coproc NAME {{ {RM} -rf {H}/genesis/data; }}"),
            # Prefixes absent from the table entirely — the open complement that a
            # whitelist structurally cannot cover.
            ("unlisted-setpriv", "setpriv --reuid 0 {RM} -rf {H}/genesis/data"),
            ("unlisted-systemd-run", "systemd-run {RM} -rf {H}/genesis/data"),
            ("unlisted-unshare", "unshare -m {RM} -rf {H}/genesis/data"),
            # Deliberately NOT given a table entry: this one re-roots the
            # filesystem, so resolving it would hand a path guard operands that
            # mean something else. Refused rather than resolved.
            ("reroots-the-filesystem", "chroot --skip-chdir /nr {RM} -rf {H}/genesis/data"),
        ],
        ids=[
            "reparsed-single-quoted",
            "reparsed-double-quoted",
            "compound-body",
            "unlisted-setpriv",
            "unlisted-systemd-run",
            "unlisted-unshare",
            "reroots-the-filesystem",
        ],
    )
    def test_an_unresolvable_prefix_is_refused_not_certified(self, label, cmd_tpl, fake_home):
        """The complement of the wrapper table — where a whitelist fails open.

        Every shape here runs a real deletion under bash and resolves to something
        that is not ``rm``, so the ordinary branch skips it. Each was MEASURED as
        allowed before this change. They are covered by the conservative check
        rather than by resolution, which is the whole reason that check exists.

        ``coproc NAME <simple command>`` is deliberately NOT in this table. An
        earlier revision locked it as residue, on the belief that the name occupies
        the command position. Bash does not do that — measured, that spelling
        reports ``NAME: command not found`` and runs nothing, so it is a non-event
        rather than a bypass and locking it asserted nothing at all. The compound
        form above is the named spelling that actually executes.
        """
        cmd = cmd_tpl.format(RM="rm", H=H)
        r = _run(cmd, fake_home)
        assert r.returncode == 2, (
            f"{label}: an unresolvable prefix must be refused, not certified. "
            f"out={r.stdout!r} err={r.stderr!r}"
        )

    @pytest.mark.parametrize(
        "label,cmd_tpl",
        [
            ("search-a-protected-dir", "sudo grep -r '{RM} -rf' {H}/genesis/data"),
            ("git-log-grep", "env git -C {H}/genesis/data log --grep='{RM} -rf'"),
            ("read-a-file-there", "timeout 5 cat {H}/genesis/data/notes.md  # {RM} later"),
            ("list-the-directory", "eval ls -la {H}/genesis/data"),
            ("find-by-name", "nice find {H}/genesis/data -name '*.{RM}'"),
        ],
        ids=[
            "search-a-protected-dir",
            "git-log-grep",
            "read-a-file-there",
            "list-the-directory",
            "find-by-name",
        ],
    )
    def test_a_resolved_prefix_over_a_protected_path_is_still_allowed(
        self, label, cmd_tpl, fake_home
    ):
        """A prefix the parser RESOLVED tells us what runs — do not second-guess it.

        These decide whether the conservative check is narrow enough to ship. Each
        contains BOTH a deletion word and a protected path behind a prefix, which
        is exactly what the fallback keys on, while deleting nothing. The parser
        resolves the prefix, the resolved executable is not a deletion, and so
        there is nothing left to be conservative about.

        MEASURED, and this is why the trigger is not simply "leading word is a
        prefix": an earlier revision used that broader rule and refused four of
        these five. Firing on a prefix the parser already resolved buys no
        coverage and costs ordinary commands.
        """
        cmd = cmd_tpl.format(RM="rm", H=H)
        r = _run(cmd, fake_home)
        assert r.returncode == 0, f"{label}: a resolved prefix was over-blocked. err={r.stderr!r}"


class TestTheConservativeCheckIsPerSegment:
    """Three ways a WHOLE-COMMAND question let a real deletion through.

    The conservative check landed asking its questions of the command line
    rather than of each segment, and a cross-model review found one bypass per
    question. They are one defect: a command is a LIST of things that run, and a
    predicate over the list cannot answer "is THIS one safe".

    Each test below pairs the bypass with the shape it must not start blocking,
    because every one of these fixes widens a refusal.
    """

    def test_an_unrelated_deletion_earlier_on_the_line_does_not_excuse_a_later_one(
        self, fake_home
    ):
        """"Does ANY segment resolve to rm?" — one harmless deletion in /tmp
        answered yes and switched the fallback off for every other segment, so
        the deletion behind the prefix was never examined."""
        r = _run(f"rm /tmp/harmless; setpriv --no-new-privs rm -r {H}/genesis/data", fake_home)
        assert r.returncode == 2, f"a resolved rm excused a hidden one: err={r.stderr!r}"

    def test_the_prefixed_branch_sees_an_ANCESTOR_not_just_a_literal_alias(self, fake_home):
        """A substring scan looks for the protected path's own spelling, so by
        construction it cannot see the operand forms `_operand_blocks` exists
        for. `$HOME` names no protected directory and removes all of them."""
        r = _run(f"setpriv --no-new-privs rm -r {H}", fake_home)
        assert r.returncode == 2, f"an ancestor deletion passed: err={r.stderr!r}"

    def test_the_prefixed_branch_sees_a_GLOB_target(self, fake_home):
        """The other operand form a substring scan cannot express."""
        r = _run(f"setpriv --no-new-privs rm -rf {H}/genesis/da*", fake_home)
        assert r.returncode == 2, f"a glob deletion passed: err={r.stderr!r}"

    def test_a_prefixed_deletion_INSIDE_a_nested_script_is_seen(self, fake_home):
        """"Top-level segments only" skipped exactly the segments `analyze()`
        works to surface: `sh -c '…'` yields its inner command at depth 1."""
        r = _run(f"bash -c 'setpriv --no-new-privs rm -r {H}/genesis/data'", fake_home)
        assert r.returncode == 2, f"a nested prefixed deletion passed: err={r.stderr!r}"

    def test_prose_in_a_heredoc_is_STILL_not_scanned(self, fake_home):
        """THE reason the depth filter existed, and why it is kept on the
        substring branch alone.

        MEASURED when it was absent: a 40,925-character heredoc writing a plan
        document was refused, because two lines deep inside the prose began with
        a shell name. Reading the OPERANDS of a deletion that is really in the
        argv is a different act from scanning text for a path — the first is now
        allowed at any depth, the second still is not.
        """
        body = "\n".join(
            [
                "setpriv is one way to run a command under reduced privileges.",
                f"Never point it at {H}/genesis/data.",
                "eval is another, and is worse.",
            ]
        )
        r = _run(f"cat > /tmp/plan.md <<'EOF'\n{body}\nEOF\nrm /tmp/scratch", fake_home)
        assert r.returncode == 0, f"heredoc prose was scanned: err={r.stderr!r}"

    def test_an_unmodeled_prefix_over_a_protected_path_with_no_deletion_is_allowed(
        self, fake_home
    ):
        """The over-block control for the widened branch. `setpriv` is not in the
        wrapper table, so nothing is resolved and the segment reaches the new
        code — with no deletion word in it, nothing may be refused."""
        r = _run(f"setpriv --no-new-privs grep -r needle {H}/genesis/data", fake_home)
        assert r.returncode == 0, f"a read behind an unmodeled prefix blocked: err={r.stderr!r}"

    def test_a_prefixed_deletion_of_an_UNPROTECTED_path_is_allowed(self, fake_home):
        """The other half of the control: the new branch must decide on the
        TARGET, not on the presence of a prefix plus a deletion."""
        r = _run(f"setpriv --no-new-privs rm -rf {H}/scratch/build", fake_home)
        assert r.returncode == 0, f"a benign prefixed deletion blocked: err={r.stderr!r}"
