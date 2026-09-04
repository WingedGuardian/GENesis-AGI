"""Tests for the pre-push privacy ADVISORY hook.

The hook is non-blocking: it emits `hookSpecificOutput.additionalContext` on
findings and ALWAYS exits 0. Fixtures use a generic CGNAT literal (100.64.0.1)
that the contribution sanitizer's portability scanner flags but that is NOT
this install's real address (so this test file leaks nothing and is not caught
by the CI install-IP scan).
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts" / "hooks"))
sys.path.insert(0, str(_ROOT / "src"))

import pre_push_privacy_review as hook  # noqa: E402

_LEAK_DIFF = (
    "diff --git a/x.py b/x.py\n"
    "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n"
    "+HOST = '100.64.0.1'\n"  # generic CGNAT — flagged by the sanitizer, not real
)
_CLEAN_DIFF = (
    "diff --git a/x.py b/x.py\n"
    "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n"
    "+HOST = '8.8.8.8'\n"  # public address — no portability hit
)


# ── _push_remote parsing (pure) ────────────────────────────────────────


def test_push_remote_bare():
    assert hook._push_remote("git push") == ""


def test_push_remote_named():
    assert hook._push_remote("git push origin feature") == "origin"
    assert hook._push_remote("git push private myfix") == "private"


def test_push_remote_flags_before_remote():
    assert hook._push_remote("git push -u origin HEAD") == "origin"
    assert hook._push_remote("git -C /repo push origin main") == "origin"
    assert hook._push_remote("git push --force-with-lease origin br") == "origin"


def test_push_remote_env_prefix():
    assert hook._push_remote("GIT_SSH=x git push origin main") == "origin"


def test_push_remote_not_a_push():
    assert hook._push_remote("git commit -m x") is None
    assert hook._push_remote("git log --oneline") is None
    # 'git' and 'push' present but 'git' is not the command word:
    assert hook._push_remote("echo git push") is None
    assert hook._push_remote("grep -r push src/") is None


def test_push_remote_compound_command():
    # A push anywhere in a compound command is detected.
    assert hook._push_remote("git add -A && git push origin main") == "origin"
    assert hook._push_remote("git push | tee log.txt") == ""


def test_push_remote_skips_super_prefix_value():
    # `git --super-prefix <path> push <remote>`: --super-prefix consumes its
    # value. If the parser does not skip that value it lands on the path token,
    # never sees `push`, and misreads the command as "not a push" → the advisory
    # scan silently SKIPS. The privacy hook's git-global value-flag set was
    # missing --super-prefix (the guard + shell_parse copies already had it);
    # locked identical by test_value_flag_consistency.
    assert hook._push_remote("git --super-prefix /tmp/sp push origin main") == "origin"


def test_effective_cwd_skips_super_prefix_to_find_dash_C():
    # --super-prefix must be consumed WITH its value so a following `-C <dir>` is
    # still found — otherwise the push is scanned against the wrong cwd.
    cwd = hook._effective_cwd("git --super-prefix /tmp/sp -C /work push origin main", "/payload")
    assert cwd == "/work"


# ── _scan (reuses the sanitizer's cheap regex scanners) ────────────────


def test_scan_flags_install_pattern():
    findings = hook._scan(_LEAK_DIFF)
    assert findings, "portability scanner should flag the CGNAT literal"
    assert any(getattr(f, "file", None) == "x.py" for f in findings)


def test_scan_clean_diff_no_findings():
    assert hook._scan(_CLEAN_DIFF) == []


# ── main() end-to-end (git helpers monkeypatched) ──────────────────────


def _run_main(monkeypatch, capsys, *, command, public, diff):
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": "/tmp"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(hook, "_targets_public_repo", lambda remote, cwd: public)
    monkeypatch.setattr(hook, "_outgoing_diff", lambda cwd: diff)
    hook.main()
    return capsys.readouterr().out


def test_main_emits_advisory_on_public_push_with_leak(monkeypatch, capsys):
    out = _run_main(
        monkeypatch, capsys, command="git push origin feat", public=True, diff=_LEAK_DIFF
    )
    payload = json.loads(out)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert payload["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in payload["hookSpecificOutput"]
    assert "Pre-push privacy review" in ctx
    assert "x.py" in ctx


def test_main_quiet_on_clean_diff(monkeypatch, capsys):
    out = _run_main(
        monkeypatch, capsys, command="git push origin feat", public=True, diff=_CLEAN_DIFF
    )
    assert out == ""


def test_main_noop_on_non_origin_push(monkeypatch, capsys):
    out = _run_main(
        monkeypatch, capsys, command="git push private feat", public=False, diff=_LEAK_DIFF
    )
    assert out == ""


def test_main_noop_on_non_push(monkeypatch, capsys):
    payload = {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    hook.main()
    assert capsys.readouterr().out == ""


def test_main_never_raises_and_stays_quiet_on_error(monkeypatch, capsys):
    # A scanner blowup must NEVER escape (would non-zero-exit → block the push).
    def _boom(_diff):
        raise RuntimeError("scanner exploded")

    monkeypatch.setattr(hook, "_scan", _boom)
    out = _run_main(
        monkeypatch, capsys, command="git push origin feat", public=True, diff=_LEAK_DIFF
    )
    assert out == ""  # swallowed, exit 0


# ── shared git budget + URL normalization (review SHOULD-FIX + NOTE) ────


def test_git_bails_when_budget_exhausted(monkeypatch):
    """Once the shared wall-clock budget is blown, _git returns None WITHOUT
    spawning another git process — so chained calls can't approach the hook's
    CC timeout (a PreToolUse timeout is treated as a block)."""
    import time as _t

    def _boom_run(*a, **k):
        raise AssertionError("git must not run once the budget is exhausted")

    monkeypatch.setattr(hook, "_deadline", _t.monotonic() - 1.0)
    monkeypatch.setattr(hook.subprocess, "run", _boom_run)
    assert hook._git(["rev-parse", "HEAD"], None) is None


def test_effective_cwd_git_dash_c():
    # `git -C <dir> push` runs in <dir>, overriding the payload cwd.
    assert hook._effective_cwd("git -C /wt push -u origin br", "/main") == "/wt"


def test_effective_cwd_leading_cd():
    # `cd <dir> && git push` runs in <dir>.
    assert hook._effective_cwd("cd /wt && git push origin br", "/main") == "/wt"


def test_effective_cwd_git_c_overrides_cd():
    # `-C` on the push wins over a preceding `cd`.
    assert hook._effective_cwd("cd /a && git -C /b push", "/main") == "/b"


def test_effective_cwd_relative_cd_resolves_against_payload():
    assert hook._effective_cwd("cd sub && git push", "/base") == "/base/sub"


def test_effective_cwd_bare_push_uses_payload():
    assert hook._effective_cwd("git push origin br", "/main") == "/main"


def test_targets_public_repo_normalizes_url(monkeypatch):
    """An explicit-URL push to the same repo as origin but spelled differently
    (``.git`` suffix / trailing slash) still counts as the public repo."""
    monkeypatch.setattr(
        hook,
        "_git",
        lambda args, cwd: (
            "https://github.com/Org/Repo.git"
            if args[:3] == ["remote", "get-url", "--push"]
            else None
        ),
    )
    assert hook._targets_public_repo("https://github.com/Org/Repo", None) is True
    assert hook._targets_public_repo("https://github.com/Org/Repo/", None) is True
    assert hook._targets_public_repo("https://github.com/Org/Other", None) is False


# ── unresolvable push cwd ────────────────────────────────────────────────────
#
# The advisory used to JOIN an unresolved `cd` target onto the payload cwd,
# producing a directory that cannot exist (e.g. "<cwd>/$W"). Every git call
# there fails, the outgoing diff comes back empty, and the hook returns with NO
# output — so a push it could not scope looked exactly like a clean one.
#
# MEASURED on this install's 508 real pushes, by replaying each through the OLD
# resolver and asking whether its answer could exist as a directory:
#   126 (24.8%) impossible because `~` was never expanded
#    13  (2.6%) impossible because `$`/backtick was never expanded
# Every one of those scans silently produced nothing. 13/508 is also the rate at
# which the new notice fires.
#
# The contract is ADVISORY, so "unresolvable" must never block and must never
# fabricate. It reports that the scan could not be scoped, and says why.

_PUSH = "git " + "push"


def test_cd_into_a_variable_is_unresolvable():
    """The real 2026-09-03 shape: `W=<path>; cd $W && <push> -u origin br`."""
    got = hook._effective_cwd(f"W=/some/path; cd $W && {_PUSH} -u origin br", "/main")
    assert got is hook._CWD_UNRESOLVED


def test_cd_into_a_quoted_variable_is_unresolvable():
    # shlex strips the quotes, so this reaches the resolver as a bare `$W`.
    got = hook._effective_cwd(f'cd "$W" && {_PUSH} origin br', "/main")
    assert got is hook._CWD_UNRESOLVED


def test_cd_into_a_braced_variable_is_unresolvable():
    got = hook._effective_cwd(f"cd ${{W}} && {_PUSH} origin br", "/main")
    assert got is hook._CWD_UNRESOLVED


def test_cd_into_a_command_substitution_is_unresolvable():
    got = hook._effective_cwd(f"cd `pwd`/x && {_PUSH} origin br", "/main")
    assert got is hook._CWD_UNRESOLVED


def test_cd_into_a_glob_is_unresolvable():
    got = hook._effective_cwd(f"cd /a/*/b && {_PUSH} origin br", "/main")
    assert got is hook._CWD_UNRESOLVED


def test_cd_dash_is_unresolvable():
    """`cd -` is the previous directory — not knowable from the command text."""
    got = hook._effective_cwd(f"cd - && {_PUSH} origin br", "/main")
    assert got is hook._CWD_UNRESOLVED


def test_bare_cd_is_unresolvable():
    """A bare `cd` goes to $HOME; it was previously IGNORED, silently leaving
    the payload cwd in place so a different repo was scanned."""
    got = hook._effective_cwd(f"cd && {_PUSH} origin br", "/main")
    assert got is hook._CWD_UNRESOLVED


def test_relative_cd_without_a_payload_cwd_is_unresolvable():
    """Previously returned the bare relative string, which was then handed to
    subprocess(cwd=...) and resolved against the HOOK's own cwd — a silently
    wrong tree rather than an honest 'unknown'."""
    got = hook._effective_cwd(f"cd sub && {_PUSH} origin br", None)
    assert got is hook._CWD_UNRESOLVED


def test_tilde_cd_expands():
    """Previously produced '<cwd>/~/wt' — a path that cannot exist."""
    import os as _os

    got = hook._effective_cwd(f"cd ~/wt && {_PUSH} origin br", "/main")
    assert got == _os.path.join(_os.path.expanduser("~"), "wt")


def test_absolute_dash_C_recovers_from_an_unresolvable_cd():
    """`-C <abs>` fully determines the repo, so a prior unresolvable cd is moot.

    NOT a regression pin — MEASURED old=ALLOW / new=ALLOW: the pre-change
    `_resolve` also short-circuited on `isabs`, so this returned `/wt` before the
    fix too. It is here as a guard against the NEW code OVER-poisoning — an
    unresolvable cd must not swallow a `-C` that fully determines the answer —
    and is labelled so it is not mistaken for proof of the fix. Every other new
    test in this section does discriminate against the old hook."""
    got = hook._effective_cwd("cd $W && git -C /wt push origin br", "/main")
    assert got == "/wt"


def test_relative_dash_C_after_an_unresolvable_cd_stays_unresolvable():
    got = hook._effective_cwd("cd $W && git -C sub push origin br", "/main")
    assert got is hook._CWD_UNRESOLVED


def test_unresolvable_cwd_emits_a_notice_instead_of_silence(monkeypatch, capsys):
    """ACCEPTANCE BAR. Replays the real push shape end-to-end through main().

    Before the fix this printed NOTHING: the fabricated path made every git call
    fail, the diff came back empty, and main() returned at the empty-diff guard.
    The advisory's whole job is to inform, so silence here is a false all-clear.
    """
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": f"W=/some/path; cd $W && {_PUSH} -u origin br"},
        "cwd": "/main",
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    hook.main()
    out = capsys.readouterr().out
    assert out.strip(), "advisory emitted nothing for a push it could not scope"
    payload_out = json.loads(out)
    ctx = payload_out["hookSpecificOutput"]["additionalContext"]
    assert "could not" in ctx.lower() or "unresolved" in ctx.lower()
    # An advisory must never carry a decision.
    assert "permissionDecision" not in json.dumps(payload_out)


def test_unresolvable_cwd_still_exits_zero():
    """The advisory contract: NEVER block, whatever it could not work out.

    Runs the REAL process. An earlier version of this test called ``main()``
    in-process with no assertion at all — and since ``main()`` wraps everything
    in ``except Exception: return`` it could not fail against ANY implementation,
    including a deliberately broken one. It also never observed an exit code,
    which is the one thing its name promised. Caught in review.
    """
    import subprocess

    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": f"cd $W && {_PUSH} origin br"},
        "cwd": "/main",
    }
    hook_path = Path(hook.__file__)
    proc = subprocess.run(
        [sys.executable, str(hook_path)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == "", "an advisory must not write to stderr"
    assert "permissionDecision" not in proc.stdout, "an advisory carries no decision"
    assert proc.stdout.strip(), "and it must still SAY it could not scope the scan"


def test_subshell_scopes_the_cd_and_is_unresolvable():
    """REGRESSION PIN for the worst case in this class, found in review.

    `( cd /wt && git push )` has `(` as its first token, never `cd`, so the cd
    was invisible and the PAYLOAD cwd survived. The hook then scanned a
    DIFFERENT repository and — if that one was clean — emitted nothing: a false
    all-clear about a tree that was never pushed. Worse than the bare-variable
    case that started this, because it reports on the wrong repo rather than on
    nothing.
    """
    assert hook._effective_cwd(f"( cd /wt && {_PUSH} origin br )", "/main") is (
        hook._CWD_UNRESOLVED
    )
    assert hook._effective_cwd(f"{{ cd /wt && {_PUSH} origin br; }}", "/main") is (
        hook._CWD_UNRESOLVED
    )


@pytest.mark.parametrize("target", ["/a/b[1]", "/a/{x}", "/a/(x)", "/a/<x>"])
def test_shell_metacharacters_are_unresolvable(target):
    """The char set was narrower than the sibling copies', so these resolved to
    literal paths that cannot exist — and an impossible path fails SILENTLY,
    which is the whole defect. Found in review; the set now matches
    review_enforcement_commit's."""
    got = hook._effective_cwd(f"cd {target} && {_PUSH} origin br", "/main")
    assert got is hook._CWD_UNRESOLVED


def test_backslash_escape_resolves_the_way_bash_does():
    """DOCUMENTED DIVERGENCE from the raw-segment sibling copies.

    They see `cd /a/b\\c` before any shell processing, cannot tell what bash will
    do with the backslash, and refuse. This copy is handed a shlex-split token,
    and shlex has already applied bash's escape semantics — bash enters `/a/bc`
    and so do we. Being MORE precise than the siblings is fine; being silently
    different is not, which is why it is pinned here and listed in the parity
    lock's divergence table."""
    assert hook._effective_cwd(f"cd /a/b\\c && {_PUSH} origin br", "/main") == "/a/bc"


def test_quoted_path_with_spaces_still_resolves():
    """The other direction — the widened set must not swallow legitimate paths.
    A token that still contains whitespace after shlex got there from a QUOTED
    path, which is a faithful literal."""
    assert hook._effective_cwd(f"cd '/a b' && {_PUSH} origin br", "/main") == "/a b"


# --- tilde quoting: bash expands it, shlex has already thrown the quotes away --


def test_a_tilde_whose_literal_form_also_exists_is_unresolved(tmp_path, monkeypatch):
    """REGRESSION PIN for the wrong-repo scan cross-model review found.

    Bash expands a leading `~` only when it is UNQUOTED: `cd "~/wt"` enters a
    LITERAL directory named `~/wt`. This hook is handed a shlex-split token, so
    the quoting is already gone and both readings are candidates. It expanded
    unconditionally, so when a literal `~`-named directory also existed it
    resolved to a DIFFERENT real tree and scanned that one — reporting a clean
    result about a repository nobody pushed.
    """
    (tmp_path / "~" / "wt").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)

    assert hook._cd_target("~/wt") is hook._CWD_UNRESOLVED


def test_an_ordinary_tilde_still_resolves(tmp_path, monkeypatch):
    """The other direction, and the reason the refusal is narrow.

    `cd ~/<repo> && git push` is 172 of 708 real pushes on this install (24.3%).
    Refusing every `~` — the first proposal — would have cost a quarter of all
    pushes the scan this hook exists to perform, to close a case measured at
    zero occurrences. Without a literal counterpart there is no ambiguity, so
    the expansion stands.
    """
    monkeypatch.chdir(tmp_path)  # no literal `~` directory here

    import os

    assert hook._cd_target("~/wt") == os.path.expanduser("~/wt")


def test_the_tilde_ambiguity_is_reported_through_the_production_path(tmp_path, monkeypatch):
    """Through `_effective_cwd`, not `_cd_target` alone.

    Production never calls `_cd_target` directly. A sibling lock records that
    routing a check through the wrong entry point once hid a fix that had
    already landed, so the ambiguity is asserted where the caller actually
    reads it.
    """
    (tmp_path / "~" / "wt").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)

    got = hook._effective_cwd("cd ~/wt && git push origin main", str(tmp_path))

    assert got is hook._CWD_UNRESOLVED
