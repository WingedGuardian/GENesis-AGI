"""Tests for the pre-push privacy hook — two verdicts, split by confidence.

The hook DENIES a push carrying a value this install actually has (a
release-fingerprint literal or a personal email) and merely ADVISES on a generic
private-range literal. That split is the point: a known value cannot be a
placeholder, so refusing it has no false positives; a generic literal is
legitimate constantly, so refusing THAT would make a check that cries wolf.

Fixtures use generic literals (100.64.0.1, 8.8.8.8) and a synthetic fingerprint
file, never this install's real values — so this file leaks nothing and the CI
install-IP scan has nothing to find in it.
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
    known, generic = hook._scan(_LEAK_DIFF)
    assert generic, "portability scanner should flag the CGNAT literal"
    assert any(getattr(f, "file", None) == "x.py" for f in generic)
    assert known == [], "a generic literal is not one of this install's values"


def test_scan_clean_diff_no_findings():
    known, generic = hook._scan(_CLEAN_DIFF)
    assert known == []
    assert generic == []


def test_scan_puts_a_generic_literal_in_the_ADVISORY_half():
    """A documentation address is not this install's — it must not be refused."""
    known, generic = hook._scan(_LEAK_DIFF)
    assert generic, "a CGNAT literal should be reported"
    assert known == [], "a generic literal is not a known-value match"


# ── main() end-to-end (git helpers monkeypatched) ──────────────────────


def _run_main(monkeypatch, capsys, *, command, public, diff):
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": "/tmp"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(hook, "_targets_public_repo", lambda remote, cwd: public)
    monkeypatch.setattr(hook, "_outgoing_diff", lambda cwd, source_ref=None: diff)
    hook.main()
    return capsys.readouterr().out


def test_main_advises_but_does_not_block_a_generic_literal(monkeypatch, capsys):
    out = _run_main(
        monkeypatch, capsys, command="git push origin feat", public=True, diff=_LEAK_DIFF
    )
    payload = json.loads(out)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert payload["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in payload["hookSpecificOutput"]
    assert "Pre-push privacy review" in ctx
    assert "x.py" in ctx


def test_the_advisory_does_not_promise_something_it_cannot_do(monkeypatch, capsys):
    """An advisory reaches the model in the SAME result as the completed push.

    Wording it as "confirm before it lands" describes a checkpoint that does not
    exist, and that false confidence is what let a real value through once.
    """
    out = _run_main(
        monkeypatch, capsys, command="git push origin feat", public=True, diff=_LEAK_DIFF
    )
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "Before it lands" not in ctx
    assert "going ahead" in ctx


def _fingerprint_file(tmp_path, *literals):
    fp = tmp_path / "release-fingerprints.txt"
    fp.write_text("# synthetic\n" + "\n".join(literals) + "\n")
    return fp


def test_main_DENIES_a_push_carrying_a_known_install_value(monkeypatch, capsys, tmp_path):
    """The acceptance bar: the exact defect this change exists for.

    A value present in the install's fingerprint file has no placeholder
    reading, so the push is refused rather than reported.
    """
    secret = "10.11.12.13"  # stands in for a real install literal
    monkeypatch.setenv(
        "GENESIS_RELEASE_FINGERPRINTS", str(_fingerprint_file(tmp_path, secret))
    )
    diff = (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n"
        f"+HOST = '{secret}'\n"
    )
    out = _run_main(monkeypatch, capsys, command="git push origin feat", public=True, diff=diff)
    hso = json.loads(out)["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "BLOCKED" in hso["permissionDecisionReason"]
    assert "x.py" in hso["permissionDecisionReason"]
    assert "additionalContext" not in hso, "a denial must not also emit advisory text"


def test_a_known_value_denies_even_alongside_generic_hits(monkeypatch, capsys, tmp_path):
    """The known-value verdict must not be diluted by ordinary noise."""
    secret = "10.11.12.13"
    monkeypatch.setenv(
        "GENESIS_RELEASE_FINGERPRINTS", str(_fingerprint_file(tmp_path, secret))
    )
    diff = (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +2 @@\n"
        "+GENERIC = '100.64.0.1'\n"
        f"+HOST = '{secret}'\n"
    )
    out = _run_main(monkeypatch, capsys, command="git push origin feat", public=True, diff=diff)
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_a_known_value_on_a_NON_public_push_is_not_blocked(monkeypatch, capsys, tmp_path):
    """Real values are allowed on a private fork — that is what it is for."""
    secret = "10.11.12.13"
    monkeypatch.setenv(
        "GENESIS_RELEASE_FINGERPRINTS", str(_fingerprint_file(tmp_path, secret))
    )
    diff = (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n"
        f"+HOST = '{secret}'\n"
    )
    out = _run_main(monkeypatch, capsys, command="git push private feat", public=False, diff=diff)
    assert out == ""


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


# ── the bypasses a security review found, each pinned ───────────────────────
def test_the_deny_reason_never_echoes_the_offending_value():
    """A block message that quotes the secret publishes what it is refusing.

    `_check_emails` interpolates the address into `message`, and every scanner's
    `detail` carries the raw source line — so the renderer prints LOCATION and
    CATEGORY only. Verified against the scanner that actually interpolates,
    because testing only the fingerprint path (whose message is a constant)
    would pass while the leaking path went unchecked.
    """
    from genesis.contribution import sanitize

    diff = (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n"
        "+OWNER = 'realperson@personal.example'\n"
    )
    findings = sanitize._check_emails(sanitize.parse_diff(diff))
    assert findings, "the email scanner should fire on this fixture"
    assert any("realperson@personal.example" in f.message for f in findings), (
        "precondition: the scanner DOES interpolate the literal"
    )

    rendered = "\n".join(hook._render(findings))
    assert "realperson@personal.example" not in rendered
    assert "x.py" in rendered


def test_the_same_repo_via_ssh_and_https_is_the_same_destination():
    """A second remote in the other URL form was a total bypass.

    `_norm_url` alone compares the forms unequal, so a push to the identical
    public repo over SSH resolved as "not origin" and skipped the scan entirely
    — no deny, no advisory, no output at all.
    """
    https = "https://github.com/Owner/Repo.git"
    ssh = "git@github.com:Owner/Repo.git"
    assert hook._norm_url(https) != hook._norm_url(ssh), "precondition: strings differ"
    assert hook._canonical_repo(https) == hook._canonical_repo(ssh)
    assert hook._canonical_repo(https) != hook._canonical_repo(
        "https://github.com/Other/Repo.git"
    )


@pytest.mark.parametrize(
    ("cmd", "expected"),
    [
        ("git push", None),
        ("git push origin", None),
        ("git push origin feature", "feature"),
        ("git push origin mybranch:main", "mybranch"),
        ("git push origin HEAD:refs/heads/x", "HEAD"),
        ("git push origin +force:main", "force"),
        ("git push -u origin feat", "feat"),
        ("git push origin --delete old", ""),
    ],
)
def test_the_scan_follows_the_ref_actually_being_pushed(cmd, expected):
    """`git push origin other:main` sends a ref that is NOT checked out.

    The scan diffed HEAD unconditionally, so pushing a branch you had not
    checked out was measured against unrelated content and passed with no
    signal.
    """
    assert hook._push_source_ref(cmd) == expected


def test_an_unresolvable_diff_says_so_instead_of_going_quiet(monkeypatch, capsys):
    """Silence must not be indistinguishable from "scanned, found nothing".

    A shallow clone or an unfetched origin/main yields no diff, and the hook
    used to return without a word — identical output to a clean push.
    """
    out = _run_main(
        monkeypatch, capsys, command="git push origin feat", public=True, diff=""
    )
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "NOTHING was scanned" in ctx
    assert "not as clean" in ctx


def test_a_branch_deletion_is_not_scanned(monkeypatch, capsys):
    """Deleting a remote ref sends no content."""
    out = _run_main(
        monkeypatch, capsys, command="git push origin --delete old", public=True, diff=_LEAK_DIFF
    )
    assert out == ""


def test_main_actually_passes_the_parsed_ref_to_the_scan(monkeypatch, capsys):
    """Parsing the refspec is useless unless main() hands it to the diff.

    Without this, deleting the argument from the call site leaves the parser
    tests above green while the scan quietly goes back to diffing HEAD — the
    built-but-not-wired shape, invisible because the unit under test still works.
    """
    seen: dict = {}
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "git push origin mybranch:main"},
        "cwd": "/tmp",
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(hook, "_targets_public_repo", lambda remote, cwd: True)

    def fake_diff(cwd, source_ref=None):
        seen["ref"] = source_ref
        return _CLEAN_DIFF

    monkeypatch.setattr(hook, "_outgoing_diff", fake_diff)
    hook.main()
    capsys.readouterr()
    assert seen["ref"] == "mybranch", "the scan did not follow the pushed ref"


def test_targets_public_repo_uses_the_canonical_comparison(monkeypatch):
    """The canonicaliser is useless unless the DECISION consults it.

    Testing `_canonical_repo` alone leaves the call site free to drift back to
    string equality, which is the bypass itself: a push to the same public repo
    over SSH would again resolve as "not origin" and skip the scan.
    """
    def fake_git(args, cwd):
        if args[:3] == ["remote", "get-url", "--push"]:
            return "https://github.com/Owner/Repo.git"  # origin, HTTPS form
        return ""

    monkeypatch.setattr(hook, "_git", fake_git)
    # An scp-style SSH literal for the SAME repo must be recognised as public.
    assert hook._targets_public_repo("git@github.com:Owner/Repo.git", None) is True
    # A genuinely different repo must not be.
    assert hook._targets_public_repo("git@github.com:Someone/Else.git", None) is False
