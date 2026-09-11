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
    # _scan now returns (file, line, message) tuples: the three layers it merges
    # (install values, credential shapes, gitleaks) produce different objects, so
    # a common shape is what lets them be deduped and reported together.
    assert any(file == "x.py" for file, _line, _message in findings)


def test_scan_clean_diff_no_findings():
    assert hook._scan(_CLEAN_DIFF) == []


# ── main() end-to-end (git helpers monkeypatched) ──────────────────────


def _run_main(monkeypatch, capsys, *, command, public, diff):
    """Run the hook; return (stdout, stderr, exit_code).

    The hook BLOCKS now, so it raises SystemExit(2) on a finding. Returning the
    code rather than letting the exception escape keeps every caller able to
    assert on the distinction that matters: a block (2) versus a pass (0).
    """
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": "/tmp"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(hook, "_targets_public_repo", lambda remote, cwd: public)
    monkeypatch.setattr(hook, "_outgoing_diff", lambda cwd: diff)
    code = 0
    try:
        hook.main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    cap = capsys.readouterr()
    return cap.out, cap.err, code


def test_main_blocks_a_public_push_carrying_a_leak(monkeypatch, capsys):
    """It BLOCKS now, where it used to advise.

    The advisory deferred enforcement to the CI leak-detector — which never runs
    on a branch with no PR, i.e. exactly the branches that needed it. A safety net
    hung on a job that does not execute is not a safety net.
    """
    out, err, code = _run_main(
        monkeypatch, capsys, command="git push origin feat", public=True, diff=_LEAK_DIFF
    )
    assert code == 2, "a finding must block"
    assert "BLOCKED" in err
    assert "x.py" in err
    assert out == "", "a block goes to stderr; stdout is for the hook JSON contract"


def test_the_block_names_every_way_to_clear_it(monkeypatch, capsys):
    """A block with no stated remedy is a trap, and gets switched off."""
    _, err, _ = _run_main(
        monkeypatch, capsys, command="git push origin feat", public=True, diff=_LEAK_DIFF
    )
    assert "pragma: allowlist secret" in err
    assert "gitleaks:allow" in err
    assert "genesis:verified-generic" in err


def test_main_quiet_on_clean_diff(monkeypatch, capsys):
    out, err, code = _run_main(
        monkeypatch, capsys, command="git push origin feat", public=True, diff=_CLEAN_DIFF
    )
    assert (out, err, code) == ("", "", 0)


def test_main_noop_on_non_origin_push(monkeypatch, capsys):
    """A private remote is where real install values are allowed to live."""
    out, err, code = _run_main(
        monkeypatch, capsys, command="git push private feat", public=False, diff=_LEAK_DIFF
    )
    assert (out, err, code) == ("", "", 0)


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
    out, err, code = _run_main(
        monkeypatch, capsys, command="git push origin feat", public=True, diff=_LEAK_DIFF
    )
    assert code == 0, "FAIL OPEN on an error — a crashed scanner must not wedge pushes"
    assert out == ""


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


# ─── Part D: shape detection, annotation, and the whole-branch scan ──────────


def _diff(*lines: str) -> str:
    body = "".join(f"+{line}\n" for line in lines)
    return (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
        f"@@ -0,0 +1,{len(lines)} @@\n{body}"
    )


@pytest.mark.parametrize(
    ("label", "line"),
    [
        ("github PAT", 'TOKEN = "ghp_' + "A" * 40 + '"'),
        ("anthropic key", 'KEY = "sk-ant-' + "B" * 40 + '"'),
        ("openai key", 'KEY = "sk-' + "C" * 40 + '"'),
        ("aws access key id", 'AWS_ACCESS_KEY_ID = "AKIA' + "D" * 16 + '"'),
        ("groq key", 'KEY = "gsk_' + "E" * 40 + '"'),
        ("url user:pass", 'URL = "https://admin:hunter2@internal.example.com/x"'),
        ("hardcoded password", 'DATABASE_PASSWORD = "correct-horse-battery"'),
    ],
)
def test_a_credential_shape_this_install_has_never_seen_is_caught(label, line):
    """SHAPE, not memory — the whole point of Part D.

    None of these values exists on this box, so the install-specific scanners
    (which only know values this install has seen) cannot reach them. Before this
    change every one of them would have pushed silently.
    """
    assert hook._scan(_diff(line)), f"{label} must be detected"


@pytest.mark.parametrize(
    "marker",
    ["# pragma: allowlist secret", "# gitleaks:allow", "# genesis:verified-generic"],
)
def test_each_annotation_spelling_clears_a_line(marker):
    """Three spellings because three tools are involved and none owns the others'.

    Requiring only ours would mean re-annotating lines already marked for
    detect-secrets or gitleaks.
    """
    line = 'TOKEN = "ghp_' + "A" * 40 + '"'
    assert hook._scan(_diff(line)), "precondition: unannotated, it is caught"
    assert not hook._scan(_diff(f"{line}  {marker}")), f"{marker} must clear it"


def test_annotation_also_clears_the_install_specific_scanners():
    """The most legitimate hits come from those, so exempting only the new layers
    would leave the common false positive unclearable — e.g. a CIDR constant in a
    network classifier, or a reserved-domain fixture."""
    leak = _LEAK_DIFF.replace("\n", "", 0)
    assert hook._scan(leak), "precondition: the install pattern is caught"
    annotated = "\n".join(
        ln + "  # genesis:verified-generic" if ln.startswith("+") and not ln.startswith("+++")
        else ln
        for ln in leak.splitlines()
    )
    assert not hook._scan(annotated + "\n")


def test_an_assignment_from_the_environment_is_not_a_secret():
    """`KEY = os.environ[...]` NAMES a credential; it does not contain one.

    MEASURED: this class was 8 of 11 false blocks in the first pass over 40
    merged commits, and it is the single most common credential-shaped line in
    real source.
    """
    for line in (
        'ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]',
        "GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')",
        "API_KEY = config.api_key",
        "SECRET_TOKEN = None",
        'API_KEY = f"{prefix}-{suffix}"',
    ):
        assert not hook._scan(_diff(line)), f"a reference must not block: {line}"


def test_a_hardcoded_literal_still_blocks():
    """The other side of the same filter — it must not have blinded the check."""
    assert hook._scan(_diff('DATABASE_PASSWORD = "correct-horse-battery"'))


def test_findings_never_echo_the_matched_value():
    """Reporting the secret to report the secret leaks it one place further.

    This output reaches a terminal and a transcript.
    """
    secret = "ghp_" + "Z" * 40
    found = hook._scan(_diff(f'TOKEN = "{secret}"'))
    assert found
    for _file, _line, message in found:
        assert secret not in message
        assert "ZZZZ" not in message


def test_the_diff_is_the_whole_branch_not_the_unpushed_delta(monkeypatch):
    """Scanning only new commits makes already-pushed content invisible.

    That produced the exact silence-then-noise this change fixes: the same lines
    flagged on a first push and went quiet on every push after, so a re-push
    reported clean on a branch that was not. The base must come from the
    merge-base with main, never from origin/<branch>.
    """
    calls = []

    def _fake_git(args, cwd=None):
        calls.append(args)
        if args[:1] == ["merge-base"]:
            return "abc123"
        if args[:1] == ["diff"]:
            return ""
        return ""

    monkeypatch.setattr(hook, "_git", _fake_git)
    hook._outgoing_diff(None)
    assert ["merge-base", "origin/main", "HEAD"] in calls
    assert not any(
        a[:1] == ["rev-parse"] and any("origin/" in str(x) for x in a) for a in calls
    ), "must not resolve origin/<branch> — that is the incremental scan"
