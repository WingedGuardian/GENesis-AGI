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
