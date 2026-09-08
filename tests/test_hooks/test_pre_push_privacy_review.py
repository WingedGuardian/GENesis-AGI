"""Tests for the pre-push privacy hook.

TWO VERDICTS. A CLASS finding (RFC1918 shapes, /home/<user>, emails) is advisory:
it emits `hookSpecificOutput.additionalContext` and exits 0, because those
regexes have real false positives. A FINGERPRINT match exits 2 and blocks: those
patterns are curated per-install to name that install's own values, and a push
cannot be taken back. (Not "exact literals" — the sanitizer compiles each entry
as a regex; an entry is as precise as whoever wrote it.) Fixtures use a generic CGNAT literal (100.64.0.1)
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


def test_scan_flags_install_pattern(fingerprints):
    # The `fingerprints` fixture is REQUIRED, not decoration: without it `_scan`
    # falls through to the developer's real ~/.genesis file, which does not exist
    # on CI — so the fingerprint assertion below would be vacuous there and merely
    # environment-dependent locally.
    class_findings, fingerprint_findings = hook._scan(_LEAK_DIFF)
    assert class_findings, "portability scanner should flag the CGNAT literal"
    assert any(getattr(f, "file", None) == "x.py" for f in class_findings)
    assert fingerprint_findings == [], (
        "a class-pattern hit must not land in the fingerprint bucket — that bucket "
        "is what blocks, and the two verdicts must not blur"
    )


def test_scan_clean_diff_no_findings(fingerprints):
    assert hook._scan(_CLEAN_DIFF) == ([], [])


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


# ── the BLOCKING path: exact fingerprint matches ───────────────────────
#
# Class findings stay advisory because their regexes have real false positives
# (this repo's own fixtures carry a dozen). A fingerprint is an exact literal
# from the install's own file, so a hit IS the private value — and a push cannot
# be taken back, which is why this one blocks rather than warns.

_SECRET = "sw-fixture-secret-slug-42"
_FINGERPRINT_DIFF = (
    f"diff --git a/t.py b/t.py\n--- a/t.py\n+++ b/t.py\n@@ -1 +1 @@\n+PROJECT = '{_SECRET}'\n"
)


@pytest.fixture
def fingerprints(tmp_path, monkeypatch):
    """Point the hook at a synthetic fingerprint file — never the real one."""
    fp = tmp_path / "release-fingerprints.txt"
    fp.write_text(f"# a comment line, ignored\n\n{_SECRET}\n")
    monkeypatch.setenv("GENESIS_RELEASE_FINGERPRINTS", str(fp))
    return fp


def _run_main_full(monkeypatch, capsys, *, command, public, diff):
    """Like _run_main but returns (exit_code, stdout, stderr)."""
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": "/tmp"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(hook, "_targets_public_repo", lambda remote, cwd: public)
    # A CONFIDENT resolution — the uncertain case has its own test. Without this
    # the real resolver runs git in a temp cwd, resolves nothing, and every block
    # test silently degrades to the advisory path.
    monkeypatch.setattr(hook, "_target_resolved", lambda remote, cwd: True)
    monkeypatch.setattr(hook, "_outgoing_diff", lambda cwd: diff)
    code = 0
    try:
        hook.main()
    except SystemExit as exc:
        code = exc.code
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def test_fingerprint_match_blocks_the_push(monkeypatch, capsys, fingerprints):
    """The whole point: exit 2, which is what PreToolUse treats as a block."""
    code, out, err = _run_main_full(
        monkeypatch, capsys, command="git push origin feat", public=True, diff=_FINGERPRINT_DIFF
    )
    assert code == 2, "an exact fingerprint match must BLOCK, not advise"
    assert out == "", "a block speaks on stderr; stdout stays free of JSON"
    assert "t.py" in err, "the author needs the location"


def test_block_message_never_echoes_the_matched_value(monkeypatch, capsys, fingerprints):
    """Same contract as the CI scanner: name the location, never the content.
    A transcript is one of the places the value should not end up."""
    _code, _out, err = _run_main_full(
        monkeypatch, capsys, command="git push origin feat", public=True, diff=_FINGERPRINT_DIFF
    )
    assert _SECRET not in err


def test_class_findings_alone_never_block(monkeypatch, capsys, fingerprints):
    """A fingerprint file is present and simply does not match. Class hits keep
    their advisory verdict — the false-positive rate is why they have it."""
    code, out, err = _run_main_full(
        monkeypatch, capsys, command="git push origin feat", public=True, diff=_LEAK_DIFF
    )
    assert code == 0
    assert err == ""
    assert "Pre-push privacy review" in json.loads(out)["hookSpecificOutput"]["additionalContext"]


def test_missing_fingerprint_file_never_blocks(monkeypatch, capsys, tmp_path):
    """Fail OPEN on an absent file. A fresh clone has no fingerprints, and a hook
    that blocked every push there would cost more than the leak it prevents."""
    monkeypatch.setenv("GENESIS_RELEASE_FINGERPRINTS", str(tmp_path / "nope.txt"))
    code, _out, err = _run_main_full(
        monkeypatch, capsys, command="git push origin feat", public=True, diff=_FINGERPRINT_DIFF
    )
    assert code == 0
    assert err == ""


def test_override_sigil_downgrades_to_an_announced_advisory(monkeypatch, capsys, fingerprints):
    """The deliberate case — pushing the fingerprint file itself, say. It goes
    through, and it says out loud that it did; a silent override would be worse
    than no override."""
    code, out, err = _run_main_full(
        monkeypatch,
        capsys,
        command="git push origin feat  # privacy-override",
        public=True,
        diff=_FINGERPRINT_DIFF,
    )
    assert code == 0, "the sigil must clear the block"
    assert err == ""
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "OVERRIDDEN" in ctx and "privacy-override" in ctx
    assert _SECRET not in ctx, "even when overridden, do not echo the value"


def test_a_quoted_sigil_is_not_an_override(monkeypatch, capsys, fingerprints):
    """The sigil must be a real trailing comment, not the word appearing inside a
    quoted string somewhere in the command — that is the shared parser's job and
    this pins that we use it rather than a local regex."""
    code, _out, _err = _run_main_full(
        monkeypatch,
        capsys,
        command="git push origin feat && echo 'not a # privacy-override marker'",
        public=True,
        diff=_FINGERPRINT_DIFF,
    )
    assert code == 2, "a quoted mention must not waive the block"


def test_a_non_public_push_is_never_blocked(monkeypatch, capsys, fingerprints):
    """Real install values are allowed on a private fork; the gate is about
    publication, not about the value existing."""
    code, out, err = _run_main_full(
        monkeypatch, capsys, command="git push private feat", public=False, diff=_FINGERPRINT_DIFF
    )
    assert (code, out, err) == (0, "", "")


def test_scanner_failure_still_fails_open(monkeypatch, capsys, fingerprints):
    """The fail direction, restated for the blocking path: a hook bug must not
    become an unpushable repo."""

    def _boom(_diff):
        raise RuntimeError("scanner exploded")

    monkeypatch.setattr(hook, "_scan", _boom)
    code, out, err = _run_main_full(
        monkeypatch, capsys, command="git push origin feat", public=True, diff=_FINGERPRINT_DIFF
    )
    assert (code, out, err) == (0, "", "")


def test_the_sigil_is_registered_with_the_shared_parser():
    """An unregistered sigil still matches when queried for itself, but reads as
    prose and ENDS the leading run for anything written after it — silently
    disabling a second sigil on the same command."""
    import shell_parse

    assert "privacy-override" in shell_parse._KNOWN_SIGILS
    assert shell_parse.has_trailing_override(
        "git push origin x  # privacy-override ci-override", "ci-override"
    ), "a sigil written after privacy-override must still be seen"


# ── the input side, converted from advisory to blocking tolerances ─────
#
# The verdict changed from "print a warning" to "refuse the push", but the
# helpers that decide WHETHER and WHAT to scan were written when a miss cost a
# missing warning. Under a gate the same miss is a fail-OPEN on a publication
# guard. Each test below pins one of those conversions; each fails against the
# first draft of this change, which is what makes them worth having.


def test_every_ordinary_push_spelling_is_seen(fingerprints):
    """The parser conversion. A local `re.split` on `&&|;|&|||` never splits on a
    NEWLINE, so this repo's own idiom — commit on one line, push on the next —
    read as a single non-push segment and skipped the gate entirely, while
    `git_push_guard` on the same tool call parsed it fine and asked for approval.
    An operator would see 'this push was reviewed' with no privacy verdict behind
    it, which is worse than either hook alone.
    """
    for cmd in (
        "git push origin feat",
        "git status\ngit push origin feat",
        "git add -A && git commit -m x\ngit push origin feat",
        "cd /work\ngit push origin feat",
        "/usr/bin/git push origin feat",
        "time git push origin feat",
    ):
        assert hook._push_segments(cmd), f"push not seen in {cmd!r}"


def test_non_pushes_are_still_ignored():
    """The conversion must not widen into 'scan everything' — a guard that fires
    on unrelated commands gets routed around."""
    for cmd in ("git commit -m x", "echo git push", "git log --oneline", "grep -r push src/"):
        assert not hook._push_segments(cmd), f"false positive on {cmd!r}"


def test_the_sigil_is_scoped_to_the_push_segment(fingerprints):
    """The sigil conversion. `_has_trailing_override` breaks at the first unquoted
    `#` in whatever it is handed and its token scan crosses newlines, so passing
    the WHOLE command made 'trailing comment' mean 'any comment anywhere' — a note
    written for a following `rm`, or on a later line, waived a security gate on a
    push the author never meant to override."""
    for cmd in (
        "git push origin feat && rm -f /tmp/x  # privacy-override cleanup",
        "git push origin feat\necho done  # privacy-override",
    ):
        segs = hook._push_segments(cmd)
        assert segs, cmd
        assert not any(
            hook.has_trailing_override(s.raw, "privacy-override") for s in segs
        ), f"a comment on ANOTHER command waived the block: {cmd!r}"


def test_the_sigil_still_works_on_the_push_itself(fingerprints):
    """Guard-the-guard for the test above: narrowing the scope must not break the
    override, or the block would have no escape at all."""
    segs = hook._push_segments("git add -A\ngit push origin feat  # privacy-override")
    assert any(hook.has_trailing_override(s.raw, "privacy-override") for s in segs)


# ── _outgoing_diff: the range model, on a real repo ────────────────────
#
# This helper had NO test, which is exactly why it shipped scanning the net diff
# — the one model its own docstring argues is wrong for a publication gate.


def _repo(tmp_path):
    """A real git repo with an `origin` and one commit already pushed."""
    import subprocess as sp

    origin, work = tmp_path / "origin.git", tmp_path / "work"
    sp.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    sp.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    g = ["git", "-C", str(work)]
    sp.run([*g, "remote", "add", "origin", str(origin)], check=True)
    sp.run([*g, "config", "user.email", "t@t"], check=True)
    sp.run([*g, "config", "user.name", "t"], check=True)
    (work / "a.txt").write_text("base\n")
    sp.run([*g, "add", "a.txt"], check=True)
    sp.run([*g, "commit", "-qm", "base"], check=True)
    sp.run([*g, "push", "-q", "origin", "main"], check=True)
    return work, g


def test_a_value_added_then_scrubbed_is_still_seen(tmp_path):
    """THE range defect, replayed. Commit A adds the value, commit B removes it.
    The net `diff base..HEAD` is clean — and the value still ships in A's patch,
    published forever. That is precisely the scenario the block message calls
    unfixable, so the gate that exists to prevent it must not be blind to it.
    """
    import subprocess as sp

    work, g = _repo(tmp_path)
    (work / "leak.py").write_text("TOKEN = 'sw-fixture-secret-slug-42'\n")
    sp.run([*g, "add", "leak.py"], check=True)
    sp.run([*g, "commit", "-qm", "adds it"], check=True)
    (work / "leak.py").write_text("TOKEN = 'placeholder'\n")
    sp.run([*g, "add", "leak.py"], check=True)
    sp.run([*g, "commit", "-qm", "scrubs it"], check=True)

    hook._deadline = None
    diff = hook._outgoing_diff(str(work))
    assert _SECRET in diff, (
        "the scrubbed value is absent from what gets scanned — a net-diff model, "
        "which is the exact blind spot this gate exists to close"
    )


def test_a_leak_already_on_the_remote_is_still_seen(tmp_path):
    """The second narrowing: anchoring on `origin/<branch>` means that after the
    first push the scan only ever sees the newest commits, so a leak already
    pushed to the branch silently stops being reported."""
    import subprocess as sp

    work, g = _repo(tmp_path)
    sp.run([*g, "checkout", "-q", "-b", "feat"], check=True)
    (work / "leak.py").write_text(f"TOKEN = '{_SECRET}'\n")
    sp.run([*g, "add", "leak.py"], check=True)
    sp.run([*g, "commit", "-qm", "adds it"], check=True)
    sp.run([*g, "push", "-q", "origin", "feat"], check=True)
    (work / "other.txt").write_text("unrelated\n")
    sp.run([*g, "add", "other.txt"], check=True)
    sp.run([*g, "commit", "-qm", "unrelated follow-up"], check=True)

    hook._deadline = None
    assert _SECRET in hook._outgoing_diff(str(work)), (
        "a value already pushed to this branch dropped out of the scan"
    )


def test_clean_branch_scans_clean(tmp_path):
    """Guard-the-guard: the two tests above would also pass if `_outgoing_diff`
    simply returned the whole of history. It must not."""
    import subprocess as sp

    work, g = _repo(tmp_path)
    (work / "b.txt").write_text("nothing private\n")
    sp.run([*g, "add", "b.txt"], check=True)
    sp.run([*g, "commit", "-qm", "ordinary work"], check=True)

    hook._deadline = None
    diff = hook._outgoing_diff(str(work))
    assert "nothing private" in diff, "the new commit should be in range"
    assert "base" not in diff.replace("rebase", ""), "history before the merge-base is out of range"


# ── target resolution: uncertainty advises, it never blocks ────────────


def test_an_unresolvable_remote_advises_rather_than_blocks(monkeypatch, capsys, fingerprints):
    """The resolution conversion. 'Uncertain → treat as public' was right for an
    advisory (over-informing is free) and wrong for a gate: it strands an operator
    whose remote merely failed to resolve, and offers them only a sigil whose
    message says they are deliberately publishing — which they are not."""
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "git push notaremote feat"},
        "cwd": "/tmp",
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(hook, "_targets_public_repo", lambda remote, cwd: True)
    monkeypatch.setattr(hook, "_target_resolved", lambda remote, cwd: False)
    monkeypatch.setattr(hook, "_outgoing_diff", lambda cwd: _FINGERPRINT_DIFF)
    code = 0
    try:
        hook.main()
    except SystemExit as exc:
        code = exc.code
    cap = capsys.readouterr()
    assert code == 0, "an unresolvable remote must not hard-block"
    assert cap.err == ""
    assert "Pre-push privacy review" in cap.out, "but it must still warn"


# ── one parser, both facts (CodeRabbit Major on #1857) ─────────────────
#
# Reusing the shared parser for DETECTION while the remote still came from a
# local re.split left two parsers in one hook that disagreed, and each was wrong
# in a different direction — both ending in a false exit-2 block on a command
# that publishes nothing to the public repo.


def test_a_ref_named_push_is_not_a_push():
    """`"push" in argv` is a membership test, so a REF named push made
    `git checkout push` look like a push — scanned, and refusable. The subcommand
    is what decides."""
    for cmd in ("git checkout push", "git branch push", "git switch push"):
        assert not hook._push_segments(cmd), f"{cmd!r} is not a push"
    assert hook._push_segments("git push origin x"), "a real push must still be seen"


def test_the_remote_survives_a_multi_line_command():
    """The other direction: the shared parser found the push in a multi-line
    command and the local splitter did not, so the remote came back None, was
    folded to "" (= the default remote), and resolved as origin — a push to a
    PRIVATE fork could then be blocked as public."""
    assert hook._push_remote("git status\ngit push private feat") == "private"
    assert hook._push_remote("git add -A && git commit -m x\ngit push private feat") == "private"


def test_remote_is_read_from_the_same_segment_that_found_the_push():
    """The class fix, asserted directly: whatever segment establishes 'this is a
    push' is the segment the remote is read from, so the two facts cannot come
    from different readings of the command. The flag here takes a value, which is
    the shape a naive 'first non-flag token' scan gets wrong."""
    cmd = "git status\ngit push --receive-pack=/x private feat"
    segs = hook._push_segments(cmd)
    assert segs
    assert hook._remote_from_argv(segs[0].argv) == "private"


def test_advisory_does_not_claim_PUBLIC_when_the_remote_did_not_resolve(
    monkeypatch, capsys, fingerprints
):
    """An advisory that overstates its own certainty teaches the reader to
    discount it. On the unresolved path, 'the PUBLIC repo' asserts as fact the
    one thing that could not be determined."""
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "git push notaremote feat"},
        "cwd": "/tmp",
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(hook, "_targets_public_repo", lambda remote, cwd: True)
    monkeypatch.setattr(hook, "_target_resolved", lambda remote, cwd: False)
    monkeypatch.setattr(hook, "_outgoing_diff", lambda cwd: _FINGERPRINT_DIFF)
    hook.main()
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "PUBLIC repo" not in ctx
    assert "did not resolve" in ctx
