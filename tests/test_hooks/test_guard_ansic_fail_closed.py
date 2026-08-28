"""Fail-closed blind-spot net for the ANSI-C ($'...') guard bypass.

shell_parse models an ANSI-C `$'...'` span as a plain single quote, so a `\\'`
inside is misread as the closing quote: the enclosing `$()` closes early and a
trailing `&& git push/commit` is swallowed into a redirect target. `analyze()`
then drops the real git segment and the push / commit-no-verify / merge gates
silently no-op (fail OPEN). This was reproduced live on both hooks.

The fix nets around the blind spot in the CALLERS (shell_parse's own ANSI-C fix
is a separate follow-up): a command that is not cleanly tokenizable (after
stripping heredoc bodies, which are stdin DATA not executed argv) AND references
a gated op is blocked. Mirrors the proven probe in protected_paths_guard.

Trigger literals are assembled from fragments so this file's own text does not
carry them (matches the convention in test_shell_parse.py).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

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
# shell_parse.strip_heredoc_bodies — line-faithful, UNDER-strip biased
# ══════════════════════════════════════════════════════════════════════════
class TestStripHeredocBodies:
    def test_keeps_introducing_line_trailer(self):
        # The executed `&& git push --force` is on the SAME line as <<EOF (its
        # body starts on the NEXT line) — it must SURVIVE stripping, else a
        # naive DOTALL stripper would delete executed code and open the gate.
        cmd = f"cat <<EOF && {GIT} {PUSH} origin main {FORCE}\nEOF"
        out = sp.strip_heredoc_bodies(cmd)
        assert f"{GIT} {PUSH}" in out
        assert FORCE in out

    def test_drops_body_lines(self):
        cmd = "cat <<EOF\nsecret body line\nEOF\necho done"
        out = sp.strip_heredoc_bodies(cmd)
        assert "secret body line" not in out
        assert "echo done" in out

    def test_quoted_delimiter(self):
        cmd = f"cat <<'EOF' && {GIT} {PUSH} -f\nbody\nEOF"
        out = sp.strip_heredoc_bodies(cmd)
        assert f"{GIT} {PUSH}" in out
        assert "body" not in out

    def test_dash_tab_indent(self):
        cmd = f"cat <<-EOF && {GIT} {PUSH} -f\n\tbody\n\tEOF"
        out = sp.strip_heredoc_bodies(cmd)
        assert f"{GIT} {PUSH}" in out

    def test_missing_closer_runs_to_eof(self):
        cmd = f"cat <<EOF && {GIT} {PUSH} -f\nbody never closes"
        out = sp.strip_heredoc_bodies(cmd)
        assert f"{GIT} {PUSH}" in out  # introducing-line trailer survives

    def test_no_heredoc_unchanged(self):
        cmd = f"{GIT} {COMMIT} -m 'hello world'"
        assert sp.strip_heredoc_bodies(cmd) == cmd

    # ── forge-resistance: a quote-BLIND stripper deletes the executed line
    # between a FORGED (quoted/commented) opener and a matching delimiter,
    # which is a fail-OPEN bypass. The executed line MUST survive. ──────────
    def test_forged_opener_in_double_quotes_not_honored(self):
        cmd = f'echo "x <<EOF"\n{GIT} {PUSH} origin main {FORCE}\nEOF'
        assert f"{GIT} {PUSH}" in sp.strip_heredoc_bodies(cmd)

    def test_forged_opener_in_single_quotes_not_honored(self):
        cmd = f"echo 'x <<EOF'\n{GIT} {PUSH} origin main {FORCE}\nEOF"
        assert f"{GIT} {PUSH}" in sp.strip_heredoc_bodies(cmd)

    def test_forged_opener_in_comment_not_honored(self):
        cmd = f"echo hi # <<EOF\n{GIT} {PUSH} origin main {FORCE}\nEOF"
        assert f"{GIT} {PUSH}" in sp.strip_heredoc_bodies(cmd)

    def test_forged_opener_in_ansic_not_honored(self):
        # An ANSI-C $'...<<EOF...' opener is inside a quote span → not honored;
        # and the $' construct keeps the residual un-tokenizable anyway.
        cmd = f"echo $'x <<EOF'\n{GIT} {PUSH} origin main {FORCE}\nEOF"
        assert f"{GIT} {PUSH}" in sp.strip_heredoc_bodies(cmd)

    def test_here_string_not_treated_as_heredoc(self):
        # `<<<` is a here-STRING (no body) — must not start body-stripping.
        cmd = f"cat <<<'data'\n{GIT} {PUSH} {FORCE}"
        assert f"{GIT} {PUSH}" in sp.strip_heredoc_bodies(cmd)


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

    def test_heredoc_tokenizable_after_strip(self):
        # A legit heredoc is untokenizable to shlex only because of its body;
        # with strip_heredocs=True it must read as tokenizable (no false net).
        cmd = f"{GIT} {COMMIT} -F - <<'EOF'\nit's a message with an apostrophe\nEOF"
        assert sp.untokenizable(cmd, strip_heredocs=True) is False


# ══════════════════════════════════════════════════════════════════════════
# git_push_guard — the net flips the ANSI-C bypasses to BLOCK (verify-RED)
# ══════════════════════════════════════════════════════════════════════════
class TestPushGuardNet:
    def test_ansic_force_push_blocked(self, tmp_path):
        r = _run(_PUSH_GUARD, ANSIC + f"{GIT} {PUSH} origin main {FORCE}", cwd=str(tmp_path))
        assert r.returncode == 2, r.stderr

    def test_ansic_gh_merge_admin_blocked(self, tmp_path):
        r = _run(_PUSH_GUARD, ANSIC + f"gh pr merge 5 {ADMIN}", cwd=str(tmp_path))
        assert r.returncode == 2, r.stderr

    def test_plain_force_push_still_blocks(self, tmp_path):
        r = _run(_PUSH_GUARD, f"{GIT} {PUSH} origin main {FORCE}", cwd=str(tmp_path))
        assert r.returncode == 2, r.stderr

    def test_benign_heredoc_commit_not_blocked(self, tmp_path):
        # A plain heredoc commit carries no push/force/no-verify — the push
        # guard must NOT block it (locks the FP surface).
        cmd = f"{GIT} {COMMIT} -F - <<'EOF'\nfix: a normal message\nEOF"
        r = _run(_PUSH_GUARD, cmd, cwd=str(tmp_path))
        assert r.returncode == 0, r.stderr

    def test_forged_heredoc_quoted_opener_blocked(self, tmp_path):
        # Forge: a quoted `<<EOF` on line 1 + matching `EOF` on line 3 would trick
        # a quote-blind stripper into deleting the executed push on line 2.
        cmd = f'echo "x <<EOF"\n{ANSIC}{GIT} {PUSH} origin main {FORCE}\nEOF'
        r = _run(_PUSH_GUARD, cmd, cwd=str(tmp_path))
        assert r.returncode == 2, r.stderr

    def test_forged_heredoc_comment_opener_blocked(self, tmp_path):
        cmd = f"echo hi # <<EOF\n{ANSIC}{GIT} {PUSH} origin main {FORCE}\nEOF"
        r = _run(_PUSH_GUARD, cmd, cwd=str(tmp_path))
        assert r.returncode == 2, r.stderr


# ══════════════════════════════════════════════════════════════════════════
# review_enforcement_commit — the net flips the ANSI-C commit bypass to BLOCK
# ══════════════════════════════════════════════════════════════════════════
class TestCommitGuardNet:
    def test_ansic_commit_no_verify_blocked(self, tmp_path):
        r = _run(_COMMIT_GUARD, ANSIC + f"{GIT} {COMMIT} {NV}", cwd=str(tmp_path))
        assert r.returncode == 2, r.stderr

    def test_ansic_commit_to_main_blocked(self, tmp_path):
        r = _run(_COMMIT_GUARD, ANSIC + f"{GIT} {COMMIT} -m x", cwd=str(tmp_path))
        assert r.returncode == 2, r.stderr

    def test_forged_heredoc_opener_blocked(self, tmp_path):
        # Forge: quoted `<<EOF` opener must not let the commit --no-verify bypass slip.
        cmd = f'echo "x <<EOF"\n{ANSIC}{GIT} {COMMIT} {NV}\nEOF'
        r = _run(_COMMIT_GUARD, cmd, cwd=str(tmp_path))
        assert r.returncode == 2, r.stderr

    def test_net_message_absent_for_tokenizable_commit(self, tmp_path):
        # A tokenizable commit may still be blocked by the review-marker/main
        # rules, but MY net's tokenizability message must NOT be the cause.
        r = _run(_COMMIT_GUARD, f"{GIT} {COMMIT} -m 'plain message'", cwd=str(tmp_path))
        assert "not safely tokenizable" not in r.stderr

    def test_benign_heredoc_commit_message_not_net_blocked(self, tmp_path):
        # A legit heredoc commit message must not trip MY net (analyze sees the
        # commit; the heredoc body is stripped) — it may be blocked for other
        # reasons, but not with the tokenizability message.
        cmd = f"{GIT} {COMMIT} -F - <<'EOF'\nfix: it's a normal message\nEOF"
        r = _run(_COMMIT_GUARD, cmd, cwd=str(tmp_path))
        assert "not safely tokenizable" not in r.stderr


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
        assert "not safely tokenizable" not in r.stderr

    def test_apostrophe_in_comment_not_net_blocked_push_guard(self, tmp_path):
        cmd = f"{GIT} status  # don't {PUSH} yet"
        r = _run(_PUSH_GUARD, cmd, cwd=str(tmp_path))
        assert "not safely tokenizable" not in r.stderr

    def test_legit_ansic_commit_message_not_net_blocked(self, tmp_path):
        # An ANSI-C message is the canonical way to embed an apostrophe; the
        # commit segment IS parsed, so the net must defer to the real gates.
        cmd = f"{GIT} {COMMIT} -m $'fix: it\\'s done'"
        r = _run(_COMMIT_GUARD, cmd, cwd=str(tmp_path))
        assert "not safely tokenizable" not in r.stderr

    def test_over_strip_cannot_starve_the_flag_match(self, tmp_path):
        """An ANSI-C desync can land a `<<WORD` inside the falsely-unquoted window,
        so the stripper deletes the very line carrying `--force`. If the flag were
        searched only in the STRIPPED text the match would be starved and the net
        would silently stand down (a reproduced fail-OPEN). The search runs on the
        ORIGINAL text, so it cannot be starved by stripping."""
        cmd = f"x=$'a\\'b<<PWN'\n{GIT} {PUSH} origin main {FORCE}\nPWN"
        r = _run(_PUSH_GUARD, cmd, cwd=str(tmp_path))
        assert r.returncode == 2, r.stderr

    def test_over_strip_starvation_commit_side(self, tmp_path):
        cmd = f"x=$'a\\'b<<PWN'\n{GIT} {COMMIT} {NV}\nPWN"
        r = _run(_COMMIT_GUARD, cmd, cwd=str(tmp_path))
        assert r.returncode == 2, r.stderr

    def test_documented_residue_decoy_segment(self, tmp_path):
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
        assert "not safely tokenizable" not in r.stderr

    def test_documented_residue_line_continuation(self, tmp_path):
        """DOCUMENTED RESIDUE (not closed by this PR).

        A `\\`-newline continuation tokenizes cleanly, so the tokenizability
        probe never fires — analyze() mis-attributing it is the SEPARATE
        mis-segmentation class (follow-up `737cdea8`). Locked here so the
        boundary is explicit and a future fix flips this test deliberately
        rather than silently.
        """
        cmd = f"{GIT} \\\n {PUSH} origin main {FORCE}"
        r = _run(_PUSH_GUARD, cmd, cwd=str(tmp_path))
        assert "not safely tokenizable" not in r.stderr


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
        assert r.returncode == 2, r.stderr
        assert "failing CLOSED" in r.stderr
