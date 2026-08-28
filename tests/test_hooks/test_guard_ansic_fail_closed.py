"""Fail-closed blind-spot net for the ANSI-C ($'...') guard bypass.

shell_parse models an ANSI-C `$'...'` span as a plain single quote, so a `\\'`
inside is misread as the closing quote: the enclosing `$()` closes early and a
trailing `&& git push/commit` is swallowed into a redirect target. `analyze()`
then drops the real git segment and the push / commit-no-verify / merge gates
silently no-op (fail OPEN). This was reproduced live on both hooks.

The fix nets around the blind spot in the CALLERS (shell_parse's own ANSI-C fix
is a separate follow-up): when a command is not cleanly parseable AND mentions a
gated op AND the parse surfaced no matching segment, the guard ASKS the human
instead of deciding. Asking (not blocking) is the load-bearing choice: a hard
block must be surgically precise about which unparseable commands are real, and
precision is exactly what an unreliable parse cannot deliver -- every narrowing
conjunct became a new way to starve the trigger, while over-blocking broke benign
shapes. With an ask, a false positive costs one confirmation and a miss is the
pre-existing status quo, so the trigger can be broad.

The invariants below are therefore: a bypass shape must NEVER be a silent allow,
and a benign shape must NEVER be a hard block.

Trigger literals are assembled from fragments so this file's own text does not
carry them (matches the convention in test_shell_parse.py).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parent.parent.parent
_HOOKS_DIR = _WORKTREE / "scripts" / "hooks"
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

import shell_parse as sp  # noqa: E402

_PUSH_GUARD = _HOOKS_DIR / "git_push_guard.py"
_COMMIT_GUARD = _WORKTREE / "scripts" / "review_enforcement_commit.py"
_PY = sys.executable

# ── trigger literals assembled from fragments (kept out of this file's text) ──
GIT = "git"
PUSH = "pu" + "sh"
FORCE = "--" + "for" + "ce"
NV = "--no-" + "ver" + "ify"
ADMIN = "--" + "admin"
COMMIT = "com" + "mit"

# The ANSI-C obfuscation prefix that defeats shell_parse's quote scanner.
ANSIC = "echo ok 2>$(echo $'a\\'b)c') && "


def _decision(r: subprocess.CompletedProcess) -> str:
    """The guard's verdict: "ask" | "block" | "allow"."""
    if '"ask"' in (r.stdout or ""):
        return "ask"
    return "block" if r.returncode == 2 else "allow"


def _run(script: Path, cmd: str, cwd: str | None = None) -> subprocess.CompletedProcess:
    payload: dict = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    if cwd is not None:
        payload["cwd"] = cwd
    return subprocess.run(
        [_PY, str(script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        env=dict(os.environ),
    )


# ══════════════════════════════════════════════════════════════════════════
# shell_parse.untokenizable
# ══════════════════════════════════════════════════════════════════════════
class TestUntokenizable:
    def test_ansic_escaped_quote_is_untokenizable(self):
        assert sp.untokenizable(ANSIC + f"{GIT} {PUSH} origin main {FORCE}") is True

    def test_plain_command_is_tokenizable(self):
        assert sp.untokenizable(f"{GIT} {PUSH} origin main {FORCE}") is False

    def test_tokenizable_ansic_message_is_tokenizable(self):
        # $'msg' with no escaped quote tokenizes fine — must NOT be flagged.
        assert sp.untokenizable(f"{GIT} {COMMIT} -m $'l1\\nl2'") is False

    def test_heredoc_apostrophe_body_is_untokenizable(self):
        # The probe reads the RAW command deliberately. An apostrophe in a
        # here-doc body genuinely shifts analyze()'s segmentation, so this MUST
        # read as a blind spot — normalizing it away disarmed the net on an
        # ordinary shape (a real gated command after such a here-doc was allowed).
        cmd = f"{GIT} {COMMIT} -F - <<'EOF'\nit's a message with an apostrophe\nEOF"
        assert sp.untokenizable(cmd) is True


# ══════════════════════════════════════════════════════════════════════════
# git_push_guard — the net flips the ANSI-C bypasses to BLOCK (verify-RED)
# ══════════════════════════════════════════════════════════════════════════
class TestPushGuardNet:
    def test_ansic_force_push_blocked(self, tmp_path):
        r = _run(_PUSH_GUARD, ANSIC + f"{GIT} {PUSH} origin main {FORCE}", cwd=str(tmp_path))
        assert _decision(r) in ("ask", "block"), r.stdout + r.stderr

    def test_ansic_gh_merge_admin_blocked(self, tmp_path):
        r = _run(_PUSH_GUARD, ANSIC + f"gh pr merge 5 {ADMIN}", cwd=str(tmp_path))
        assert _decision(r) in ("ask", "block"), r.stdout + r.stderr

    def test_plain_force_push_still_blocks(self, tmp_path):
        r = _run(_PUSH_GUARD, f"{GIT} {PUSH} origin main {FORCE}", cwd=str(tmp_path))
        assert _decision(r) in ("ask", "block"), r.stdout + r.stderr

    def test_benign_heredoc_commit_not_blocked(self, tmp_path):
        # A plain heredoc commit carries no push/force/no-verify — the push
        # guard must NOT block it (locks the FP surface).
        cmd = f"{GIT} {COMMIT} -F - <<'EOF'\nfix: a normal message\nEOF"
        r = _run(_PUSH_GUARD, cmd, cwd=str(tmp_path))
        assert _decision(r) == "allow", r.stdout + r.stderr

    def test_forged_heredoc_semicolon_comment_opener(self, tmp_path):
        # `;#` starts a bash comment just as ` #` does. A stripper that modeled
        # only the whitespace form honored the forged opener and deleted the
        # executed line (measured fail-open). The raw probe has no such seam.
        cmd = f"echo hi;# <<'EOF'\n{ANSIC}{GIT} {PUSH} origin main {FORCE}\nEOF"
        r = _run(_PUSH_GUARD, cmd, cwd=str(tmp_path))
        assert _decision(r) in ("ask", "block"), r.stdout + r.stderr

    def test_heredoc_body_apostrophe_then_real_gated_op(self, tmp_path):
        # THE regression that killed the normalizing design: an ordinary quoted
        # here-doc whose body contains a contraction, followed by a real gated
        # command. bash runs it; analyze() loses the segment; stripping the body
        # made the residue tokenize and the guard fell silent.
        cmd = f"cat > f <<'EOF'\ndon't touch\nEOF\n{GIT} {PUSH} origin main {FORCE}"
        r = _run(_PUSH_GUARD, cmd, cwd=str(tmp_path))
        assert _decision(r) in ("ask", "block"), r.stdout + r.stderr

    def test_heredoc_body_apostrophe_then_real_commit(self, tmp_path):
        cmd = f"cat > f <<'EOF'\ndon't touch\nEOF\n{GIT} {COMMIT} {NV} -am x"
        r = _run(_COMMIT_GUARD, cmd, cwd=str(tmp_path))
        assert _decision(r) in ("ask", "block"), r.stdout + r.stderr

    def test_forged_heredoc_quoted_opener_blocked(self, tmp_path):
        # Forge: a quoted `<<EOF` on line 1 + matching `EOF` on line 3 would trick
        # a quote-blind stripper into deleting the executed push on line 2.
        cmd = f'echo "x <<EOF"\n{ANSIC}{GIT} {PUSH} origin main {FORCE}\nEOF'
        r = _run(_PUSH_GUARD, cmd, cwd=str(tmp_path))
        assert _decision(r) in ("ask", "block"), r.stdout + r.stderr

    def test_forged_heredoc_comment_opener_blocked(self, tmp_path):
        cmd = f"echo hi # <<EOF\n{ANSIC}{GIT} {PUSH} origin main {FORCE}\nEOF"
        r = _run(_PUSH_GUARD, cmd, cwd=str(tmp_path))
        assert _decision(r) in ("ask", "block"), r.stdout + r.stderr


# ══════════════════════════════════════════════════════════════════════════
# review_enforcement_commit — the net flips the ANSI-C commit bypass to BLOCK
# ══════════════════════════════════════════════════════════════════════════
class TestCommitGuardNet:
    def test_ansic_commit_no_verify_blocked(self, tmp_path):
        r = _run(_COMMIT_GUARD, ANSIC + f"{GIT} {COMMIT} {NV}", cwd=str(tmp_path))
        assert _decision(r) in ("ask", "block"), r.stdout + r.stderr

    def test_ansic_commit_to_main_blocked(self, tmp_path):
        r = _run(_COMMIT_GUARD, ANSIC + f"{GIT} {COMMIT} -m x", cwd=str(tmp_path))
        assert _decision(r) in ("ask", "block"), r.stdout + r.stderr

    def test_forged_heredoc_opener_blocked(self, tmp_path):
        # Forge: quoted `<<EOF` opener must not let the commit --no-verify bypass slip.
        cmd = f'echo "x <<EOF"\n{ANSIC}{GIT} {COMMIT} {NV}\nEOF'
        r = _run(_COMMIT_GUARD, cmd, cwd=str(tmp_path))
        assert _decision(r) in ("ask", "block"), r.stdout + r.stderr

    def test_net_message_absent_for_tokenizable_commit(self, tmp_path):
        # A tokenizable commit may still be blocked by the review-marker/main
        # rules, but MY net's tokenizability message must NOT be the cause.
        r = _run(_COMMIT_GUARD, f"{GIT} {COMMIT} -m 'plain message'", cwd=str(tmp_path))
        assert _decision(r) != "block", r.stdout + r.stderr

    def test_benign_heredoc_commit_message_not_net_blocked(self, tmp_path):
        # A legit heredoc commit message must not trip MY net (analyze sees the
        # commit; the heredoc body is stripped) — it may be blocked for other
        # reasons, but not with the tokenizability message.
        cmd = f"{GIT} {COMMIT} -F - <<'EOF'\nfix: it's a normal message\nEOF"
        r = _run(_COMMIT_GUARD, cmd, cwd=str(tmp_path))
        assert _decision(r) != "block", r.stdout + r.stderr


# ══════════════════════════════════════════════════════════════════════════
# Acceptance corpus — bash-verified shapes retained from the superseded
# parser-side attempt (PR #1513, closed unmerged: "the bash-verified corpus of
# shapes developed here is retained as the acceptance suite for that work").
# These caught two real false positives in the first cut of this net.
# ══════════════════════════════════════════════════════════════════════════
class TestAcceptanceCorpus:
    """The net must fire on the un-parseable shapes and stay silent on the
    accident-plausible LEGIT ones (an apostrophe in a trailing comment, an
    ANSI-C commit message) — bash runs the latter exactly as written."""

    def test_apostrophe_in_trailing_comment_not_net_blocked(self, tmp_path):
        # `# don't` makes shlex raise, but bash never executes a comment and
        # analyze() sees the real commit → the net must NOT fire (daily friction).
        cmd = f'{GIT} {COMMIT} -m "fix: thing"  # don\'t forget'
        r = _run(_COMMIT_GUARD, cmd, cwd=str(tmp_path))
        assert _decision(r) != "block", r.stdout + r.stderr

    def test_apostrophe_in_comment_not_net_blocked_push_guard(self, tmp_path):
        cmd = f"{GIT} status  # don't {PUSH} yet"
        r = _run(_PUSH_GUARD, cmd, cwd=str(tmp_path))
        assert _decision(r) != "block", r.stdout + r.stderr

    def test_legit_ansic_commit_message_not_net_blocked(self, tmp_path):
        # An ANSI-C message is the canonical way to embed an apostrophe. The
        # commit segment IS parsed, so the blind-spot net must stand down and
        # leave the verdict to the real review/branch rules — which may well
        # block for their own reasons. Assert only that the NET did not decide.
        cmd = f"{GIT} {COMMIT} -m $'fix: it\\'s done'"
        r = _run(_COMMIT_GUARD, cmd, cwd=str(tmp_path))
        assert "could not be parsed safely" not in (r.stdout + r.stderr)

    def test_over_strip_cannot_starve_the_flag_match(self, tmp_path):
        """An ANSI-C desync can land a `<<WORD` inside the falsely-unquoted window,
        so the stripper deletes the very line carrying `--force`. If the flag were
        searched only in the STRIPPED text the match would be starved and the net
        would silently stand down (a reproduced fail-OPEN). The search runs on the
        ORIGINAL text, so it cannot be starved by stripping."""
        cmd = f"x=$'a\\'b<<PWN'\n{GIT} {PUSH} origin main {FORCE}\nPWN"
        r = _run(_PUSH_GUARD, cmd, cwd=str(tmp_path))
        assert _decision(r) in ("ask", "block"), r.stdout + r.stderr

    def test_over_strip_starvation_commit_side(self, tmp_path):
        cmd = f"x=$'a\\'b<<PWN'\n{GIT} {COMMIT} {NV}\nPWN"
        r = _run(_COMMIT_GUARD, cmd, cwd=str(tmp_path))
        assert _decision(r) in ("ask", "block"), r.stdout + r.stderr

    def test_decoy_segment_still_reaches_a_human(self, tmp_path):
        """DOCUMENTED RESIDUE (deliberate evasion — NOT closed by this PR).

        A decoy `git push origin main` before an ANSI-C desync makes analyze()
        surface one gated segment, so the net (which fires only when NO gated
        segment was parsed) stands down and the hidden force-push is neither
        blocked nor mentioned in the approval prompt. Closing it needs
        per-occurrence accounting of every gated flag against parsed segment spans
        — more argv→effect modelling, which is the non-convergent tail this design
        deliberately avoids. Sits in the same accepted-residue bucket as
        eval / `python -c` / dynamic construction (2026-08-12 decision).

        Locked so a future fix flips this deliberately rather than silently.
        """
        cmd = f"{GIT} {PUSH} origin main && x=$'a\\'b<<PWN'\n{GIT} {PUSH} origin evil {FORCE}\nPWN"
        r = _run(_PUSH_GUARD, cmd, cwd=str(tmp_path))
        assert _decision(r) != "block", r.stdout + r.stderr

    def test_line_continuation_is_documented_residue(self, tmp_path):
        """DOCUMENTED RESIDUE (not closed by this PR).

        A `\\`-newline continuation tokenizes cleanly, so the tokenizability
        probe never fires — analyze() mis-attributing it is the SEPARATE
        mis-segmentation class (follow-up `737cdea8`). Locked here so the
        boundary is explicit and a future fix flips this test deliberately
        rather than silently.
        """
        cmd = f"{GIT} \\\n {PUSH} origin main {FORCE}"
        r = _run(_PUSH_GUARD, cmd, cwd=str(tmp_path))
        assert _decision(r) != "block", r.stdout + r.stderr


# ══════════════════════════════════════════════════════════════════════════
# review_enforcement_commit is now run_guard-wrapped (fail CLOSED on a crash)
# ══════════════════════════════════════════════════════════════════════════
class TestCommitGuardFailsClosedOnCrash:
    """CC's PreToolUse contract is "exit 2 = block; ANY other code = non-blocking
    error → the tool RUNS", so a bare ``main()`` made every uncaught exception in
    this gate a silent FAIL-OPEN on a commit. run_guard converts that to exit 2."""

    def test_module_is_run_guard_wrapped(self):
        src = _COMMIT_GUARD.read_text()
        assert 'run_guard(main, "review_enforcement_commit")' in src
        assert "\n    main()\n" not in src  # the bare call is gone

    def test_unexpected_exception_exits_2(self, tmp_path):
        # Drive the REAL module through run_guard with a main() that raises.
        prog = "\n".join(
            [
                "import sys",
                f"sys.path.insert(0, {str(_WORKTREE / 'scripts')!r})",
                f"sys.path.insert(0, {str(_HOOKS_DIR)!r})",
                "from hook_input import run_guard",
                "def boom():",
                "    raise RuntimeError('unexpected guard bug')",
                "run_guard(boom, 'review_enforcement_commit')",
            ]
        )
        r = subprocess.run([_PY, "-c", prog], capture_output=True, text=True, timeout=30)
        assert _decision(r) in ("ask", "block"), r.stdout + r.stderr
        assert "failing CLOSED" in r.stderr


# ══════════════════════════════════════════════════════════════════════════
# GENERATED acceptance matrix.
#
# Corpus replay alone is insufficient: it only contains shapes that happened to
# be run before, which is exactly how the `git status # don't commit yet` false
# positive shipped past an 18k-command sweep. This enumerates the CROSS PRODUCT
# of the axes that actually drive the decision, so an untested cell fails loudly
# instead of silently.
#
# Invariants (not exact verdicts — the point is the direction of failure):
#   * a cell that really executes a gated op is NEVER a silent allow;
#   * a cell that executes nothing gated is NEVER a hard block.
# ══════════════════════════════════════════════════════════════════════════
_DESYNC = "x=$'a\\'b'\n"  # ANSI-C desync: makes the parser drop what follows

_GATED_OPS = [
    ("push_force", f"{GIT} {PUSH} origin main {FORCE}"),
    ("push_plain", f"{GIT} {PUSH} origin main"),
    ("gh_merge", "gh pr merge 5"),
    ("gh_merge_admin", f"gh pr merge 5 {ADMIN}"),
    ("commit_nv", f"{GIT} {COMMIT} {NV}"),
    ("commit_plain", f"{GIT} {COMMIT} -m x"),
]

# Contexts that HIDE a real, executed gated op from the parser.
_HIDING = [
    ("ansic_prefix", lambda op: ANSIC + op),
    ("desync_line", lambda op: _DESYNC + op),
    ("forged_heredoc", lambda op: f'echo "x <<EOF"\n{ANSIC}{op}\nEOF'),
    ("unquoted_heredoc_subst", lambda op: f"cat <<EOF\n$(x=$'a\\'b'; {op})\nEOF"),
]

# Contexts where the gated WORD appears but nothing gated executes.
_INERT = [
    ("in_comment", lambda op: f"{GIT} status # don't {op} yet"),
    ("in_ansic_data", lambda op: f"echo $'don\\'t {op} here'"),
    ("in_quoted_heredoc", lambda op: f"cat <<'EOF'\nplease don't {op}\nEOF"),
    ("in_double_quotes", lambda op: f'echo "reminder: {op} later"'),
]


def _guard_for(op: str):
    return _COMMIT_GUARD if COMMIT in op else _PUSH_GUARD


def _parser_sees_gated_op(cmd: str) -> bool:
    """Whether analyze() resolved the gated op itself.

    When it did, the ORDINARY gates own the verdict (and may legitimately allow
    — e.g. a commit in a non-repo cwd). The blind-spot net's contract binds only
    where the parser went blind, so that is what the invariant below tests.
    """
    for seg in sp.analyze(cmd):
        if sp.git_subcommand(seg.argv) in ("push", "merge", "commit"):
            return True
        if sp.gh_pr_subcommand(seg.argv) == "merge":
            return True
    return False


@pytest.mark.parametrize("op_name,op", _GATED_OPS, ids=[n for n, _ in _GATED_OPS])
@pytest.mark.parametrize("ctx_name,wrap", _HIDING, ids=[n for n, _ in _HIDING])
def test_matrix_hidden_gated_op_is_never_silently_allowed(
    op_name, op, ctx_name, wrap, tmp_path
):
    """A gated op the PARSER CANNOT SEE must never be silently allowed."""
    cmd = wrap(op)
    if _parser_sees_gated_op(cmd):
        pytest.skip("parser resolved the op — the ordinary gates own this verdict")
    r = _run(_guard_for(op), cmd, cwd=str(tmp_path))
    assert _decision(r) in ("ask", "block"), (
        f"{ctx_name}/{op_name} was silently ALLOWED\n{r.stdout}{r.stderr}"
    )


@pytest.mark.parametrize("op_name,op", _GATED_OPS, ids=[n for n, _ in _GATED_OPS])
@pytest.mark.parametrize("ctx_name,wrap", _INERT, ids=[n for n, _ in _INERT])
def test_matrix_inert_mention_is_never_hard_blocked(
    op_name, op, ctx_name, wrap, tmp_path
):
    r = _run(_guard_for(op), wrap(op), cwd=str(tmp_path))
    assert _decision(r) != "block", (
        f"{ctx_name}/{op_name} was HARD BLOCKED\n{r.stdout}{r.stderr}"
    )


# ══════════════════════════════════════════════════════════════════════════
# The net must never DOWNGRADE an existing hard block to a prompt.
#
# The net is evaluated where analyze() found no gated segment, which is also
# true for commands other rules hard-block (a direct sqlite write, a commit
# hook-skip). Returning an `ask` from inside the net pre-empted those rules and
# turned a policy block into a dialog — measured, and invisible to invariants
# that only assert "not silently allowed". These pin the verdict exactly.
# ══════════════════════════════════════════════════════════════════════════
_SQLITE = "sql" + "ite3"
_DML = "INS" + "ERT INTO t VALUES(1)"
_REPO = str(_WORKTREE)


class TestNetDoesNotDowngradeHardBlocks:
    @pytest.mark.parametrize(
        "suffix",
        ["", "  # don't " + PUSH + " yet"],
        ids=["plain", "apostrophe_comment"],
    )
    def test_direct_sqlite_write_still_hard_blocks(self, suffix):
        # This block exists ONLY in the push guard — nothing else backstops it.
        cmd = f'{_SQLITE} /tmp/x.db "{_DML}"{suffix}'
        r = _run(_PUSH_GUARD, cmd, cwd=_REPO)
        assert _decision(r) == "block", r.stdout + r.stderr

    @pytest.mark.parametrize(
        "suffix", ["", "  # don't forget"], ids=["plain", "apostrophe_comment"]
    )
    def test_commit_hook_skip_still_hard_blocks(self, suffix):
        cmd = f'{GIT} {COMMIT} {NV} -m "x"{suffix}'
        r = _run(_PUSH_GUARD, cmd, cwd=_REPO)
        assert _decision(r) == "block", r.stdout + r.stderr
