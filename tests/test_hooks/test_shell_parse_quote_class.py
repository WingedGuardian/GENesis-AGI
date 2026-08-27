"""Class fix: shell_parse's hand-rolled quote scanners must be ANSI-C ($'…') /
locale ($"…") aware and honor unquoted-backslash escaping (line-continuation,
\\' outside quotes), so a gated git action cannot hide from analyze()-based
guards behind an ACCIDENT-PLAUSIBLE lexical form.

Ground truth (verified against real bash 5.x): each obfuscated command below
runs a real `git push --force` / `git commit --no-verify`; the heredoc-body form
runs git as DATA (never executed) and is DOCUMENTED ACCEPTED RESIDUE.

Scope boundary (consciously bounded — see shell_parse module docstring): DELIBERATE
evasion and dynamic construction (eval / python -c / $(printf…) / aliases / heredoc
bodies) are accepted residue per the 2026-08-12 decision (review_enforcement_commit
lines ~686-697); those are NOT expected to be caught and are locked as residue below.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

_WT = Path(__file__).resolve().parents[2]
_HOOKS = _WT / "scripts" / "hooks"
_SCRIPTS = _WT / "scripts"
_PY = sys.executable
if str(_HOOKS) not in sys.path:
    sys.path.insert(0, str(_HOOKS))

_spec = importlib.util.spec_from_file_location("shell_parse", _HOOKS / "shell_parse.py")
sp = importlib.util.module_from_spec(_spec)
sys.modules["shell_parse"] = sp
_spec.loader.exec_module(sp)

P = "pu" + "sh"
NV = "--no" + "-verify"
F = "--" + "force"

# ANSI-C obfuscation: x=$'a\'b'  (the \' must NOT close the span; bash runs the
# tail as a real command). Assembled so the literal stays intact.
_ANSI = "x=$'a\\'b'"
_LC = "\\\n"  # backslash-newline line continuation (bash removes it)


def _sees_git(cmd: str, sub: str) -> bool:
    segs = sp.analyze(cmd)
    return any(s.exe == "git" and sp.git_subcommand(s.argv) == sub for s in segs)


def _run_hook(
    script_rel: str, command: str, script_dir: Path = _HOOKS
) -> subprocess.CompletedProcess:
    payload = json.dumps({"tool_input": {"command": command}, "tool_name": "Bash"})
    return subprocess.run(
        [_PY, str(script_dir / script_rel)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=15,
    )


class TestAnalyzeSeesGatedActionThroughLexicalForms:
    """RED before the fix: analyze() mis-segments these and the git action vanishes."""

    def test_ansi_c_then_push(self):
        assert _sees_git(f"{_ANSI} && git {P} {F}", P)

    def test_line_continuation_push(self):
        assert _sees_git(f"git {_LC}{P} {F}", P)

    def test_ansi_c_then_commit_no_verify(self):
        cmd = f"{_ANSI} && git commit {NV} -m x"
        assert any(sp.commit_skips_hooks(s.argv) for s in sp.analyze(cmd))

    def test_control_plain_push_still_seen(self):
        assert _sees_git(f"git {P} {F}", P)


class TestPushGuardBlocksThroughLexicalForms:
    """E2E through the real hook (stdin payload, as CC invokes it)."""

    def test_ansi_c_force_push_blocks(self):
        r = _run_hook("git_push_guard.py", f"{_ANSI} && git {P} origin main {F}")
        assert r.returncode == 2, f"expected BLOCK, got {r.returncode}: {r.stderr}"

    def test_line_continuation_force_push_blocks(self):
        r = _run_hook("git_push_guard.py", f"git {_LC}{P} origin main {F}")
        assert r.returncode == 2, f"expected BLOCK, got {r.returncode}: {r.stderr}"

    def test_ansi_c_commit_no_verify_blocks(self):
        r = _run_hook("git_push_guard.py", f"{_ANSI} && git commit {NV} -m x")
        assert r.returncode == 2, f"expected BLOCK, got {r.returncode}: {r.stderr}"

    def test_control_no_verify_still_blocks(self):
        r = _run_hook("git_push_guard.py", f"git commit {NV} -m x")
        assert r.returncode == 2


class TestTwinCommitGateInheritsFix:
    """review_enforcement_commit.py shares the analyze()-based commit gate."""

    def test_ansi_c_commit_no_verify_blocks_at_commit_hook(self):
        r = _run_hook("review_enforcement_commit.py", f"{_ANSI} && git commit {NV} -m x", _SCRIPTS)
        assert r.returncode == 2, f"expected BLOCK, got {r.returncode}: {r.stderr}"


class TestNoFalseFireOnLegitForms:
    """Monotonic: the fix must not start blocking legit commands."""

    def test_legit_ansi_c_commit_message_not_blocked(self):
        # $'l1\nl2' as a legit multi-line message, no gated flag → allow.
        r = _run_hook("git_push_guard.py", "git commit -m $'l1\\nl2'")
        assert r.returncode == 0, r.stderr

    def test_apostrophe_in_trailing_comment_not_blocked(self):
        # The false-fire that killed the rejected shlex-probe approach.
        r = _run_hook("git_push_guard.py", f"git {P} {F} # don't")
        # push present → the push gate applies (ask/deny), but this must NOT be a
        # crash/parse-block. In a non-interactive test env the push path returns
        # its own code; we only assert the parser didn't corrupt into an error exit.
        assert r.returncode in (0, 2)


class TestDocumentedAcceptedResidue:
    """LOCK the boundary: these DELIBERATE/dynamic forms are accepted residue
    (not caught). A future change that 'fixes' one is a CONSCIOUS choice, not an
    incremental patch — this test failing means the boundary moved on purpose."""

    def test_eval_wrapped_push_is_accepted_residue(self):
        # eval executes its string arg; no Bash-string guard can see it. RESIDUE.
        assert not _sees_git(f"eval 'git {P} {F}'", P)

    def test_python_c_push_is_accepted_residue(self):
        cmd = 'python3 -c \'import subprocess; subprocess.run(["git","' + P + "\"])'"
        assert not _sees_git(cmd, P)


class TestCommentAwarenessNoSwallow:
    """A ``#`` comment runs to end-of-line (bash). A quote char inside it (any English
    contraction — don't / it's / can't) must NOT open a span that swallows the newline
    and hides a following gated command. This is the accident-plausible bypass the
    security review reproduced live (pre-existing; folded into this PR as same-class)."""

    def test_comment_apostrophe_then_push_blocks(self):
        r = _run_hook("git_push_guard.py", f"git status # don't worry\ngit {P} origin main {F}")
        assert r.returncode == 2, f"comment-apostrophe swallowed the push: {r.stderr}"

    def test_comment_apostrophe_then_commit_nv_blocks(self):
        r = _run_hook("git_push_guard.py", f"git status # don't\ngit commit {NV} -m x")
        assert r.returncode == 2, r.stderr

    def test_comment_apostrophe_at_review_enforcement_commit(self):
        r = _run_hook(
            "review_enforcement_commit.py", f"git status # don't\ngit commit {NV} -m x", _SCRIPTS
        )
        assert r.returncode == 2, r.stderr

    def test_control_plain_comment_then_commit_nv_blocks(self):
        r = _run_hook("git_push_guard.py", f"git status # note\ngit commit {NV} -m x")
        assert r.returncode == 2

    def test_analyze_splits_across_comment_apostrophe(self):
        assert _sees_git(f"git status # don't\ngit {P} {F}", P)

    def test_multiline_double_quote_still_one_span(self):
        # A real newline INSIDE a double-quoted string must still be ONE segment —
        # comment-awareness must not break legit multi-line quotes.
        segs = sp.analyze('git commit -m "line1\nline2"')
        assert len(segs) == 1 and sp.git_subcommand(segs[0].argv) == "commit"

    def test_hash_glued_to_word_not_a_comment(self):
        # `#` NOT preceded by a boundary is not a comment; treating it as one would
        # strip trailing content and could hide a following gated token (safe-direction
        # guardrail — the push after && must stay visible).
        assert _sees_git(f"echo a#b && git {P} {F}", P)

    def test_subshell_group_close_glued_comment_apostrophe_blocks(self):
        # A bare subshell-group close `)` glued to a comment is a REAL bash comment;
        # an apostrophe in it must not swallow the following gated command.
        r = _run_hook(
            "review_enforcement_commit.py", f"(git status)#don't\ngit commit {NV} -m x", _SCRIPTS
        )
        assert r.returncode == 2, r.stderr

    def test_expansion_close_glued_hash_not_a_comment(self):
        # `$(cmd)#...` is word-continuation, NOT a comment — the following SAME-LINE
        # gated command must stay visible. (Naively treating any `)` as a comment
        # preceder would fail-open here — this locks the distinction.)
        segs = sp.analyze(f"foo=$(date)#c; git commit {NV} -m x")
        assert any(sp.commit_skips_hooks(s.argv) for s in segs)

    def test_backtick_close_glued_hash_not_a_comment(self):
        segs = sp.analyze(f"foo=`date`#c; git commit {NV} -m x")
        assert any(sp.commit_skips_hooks(s.argv) for s in segs)
