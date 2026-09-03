"""Fail-closed blind-spot net for the parser's quoting blind spot.

The parser's model of one quoting form is narrower than the shell's, so a
command can segment differently from the way it executes. When that happens the
real git segment is absent from the parse, and the gates that look for one see
the same empty result they would see if no git command were present at all --
they no-op, which is a fail-OPEN. The cases are held as fixture data below
rather than described here; the property that matters is that an empty parse is
not evidence of absence.

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
and a benign shape must never be REFUSED WHERE SOMEONE CAN APPROVE IT.

That second one used to read "a benign shape must NEVER be a hard block", full
stop, and that is false — it was written from the interactive path and quietly
assumed a human. An unattended session has nobody to answer an ask, so the only
choices there are refuse or allow-unverified; a security gate does not pick the
second, and the cost argument that buys a broad trigger ("a false positive costs
one confirmation") does not survive the move. Narrowing the predicate until the
old invariant held was attempted for three rounds and does not converge: on the
blind path the parser cannot distinguish an executed gated verb from a quoted or
commented one, and that inability IS the premise of the net.

So unattended refusals of benign shapes are accepted, with a condition that is
asserted rather than assumed — the refusal must be RECOVERABLE on its own,
naming both the cause and the rewrite that avoids it, so a session with no human
can act on it unaided.

Trigger literals are assembled from fragments so this file's own text does not
carry them (matches the convention in test_shell_parse.py).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
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

# The one-line spelling every continuation case is compared against: the fix's
# guarantee is that a continued command resolves exactly like this one.
_JOINED_FORCE_PUSH = f"{GIT} {PUSH} origin main {FORCE}"

# The obfuscation prefix these cases are built from; see the module
# docstring for why it is held as fixture data rather than described.
ANSIC = "echo ok 2>$(echo $'a\\'b)c') && "


def _decision(r: subprocess.CompletedProcess) -> str:
    """The guard's verdict: "ask" | "block" | "allow"."""
    if '"ask"' in (r.stdout or ""):
        return "ask"
    return "block" if r.returncode == 2 else "allow"


_TEST_SLUG = "testowner/testrepo"

# ALLOWLIST, not a subtract-list. Two ambient inputs reached the guards through
# this helper and each made the suite report on something other than the code:
# an inherited GENESIS_CC_SESSION made it test its CALLER's mode, and an
# inherited GIT_DIR re-pointed repo-state gates at the developer's real
# repository (both MEASURED verdict-changing). Both were fixed one key at a
# time, a review round apart — which is the instance-not-class pattern this
# allowlist exists to end. Anything not named here simply does not reach a
# guard, so the next ambient input cannot leak in by default; adding one is a
# deliberate, reviewable edit.
#
# Still inherited on purpose: PATH (git/gh must be findable — their ABSENCE
# raises loudly in the fixture rather than silently changing a verdict), TMPDIR
# and the locale vars (no verdict influence).
#
# HOME and GENESIS_HOME are not allow-listed either, but omitting them is NOT
# enough and the previous version of this comment claimed otherwise. On POSIX
# `os.path.expanduser("~")` falls back to the PASSWD-database home when HOME is
# unset, so the guards went on resolving the developer's real ~/.genesis —
# reading config through `_canonical_public_repo()` and, on an ls-remote hit,
# WRITING the push allowlist there. A test run could mutate the real store, and
# ambient config could change a verdict. Absence is not isolation; both are
# PINNED to a sandbox below.
_NEUTRAL_ENV = frozenset(
    {"PATH", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT", "PYTHONHASHSEED"}
)

# One empty directory for the whole module. Nothing here is meant to persist;
# the point is only that it is NOT the real home, so neither a read nor a write
# can reach the developer's state.
#
# TemporaryDirectory rather than mkdtemp: a bare mkdtemp leaks a directory per
# run, and TMPDIR here points at the Claude Code working temp, which a watchdog
# kills sessions over when it fills. The finalizer cleans up at interpreter exit.
_SANDBOX_HOME_TD = tempfile.TemporaryDirectory(prefix="guard-suite-home-")
_SANDBOX_HOME = _SANDBOX_HOME_TD.name


def _child_env(cwd: str | None = None, dispatched: str | None = None) -> dict[str, str]:
    """The ONE place that decides what a guard child may see.

    Built in two separate places before this, which is how the two ambient-input
    bugs below each had to be fixed twice. One definition, two callers.
    """
    env = {k: v for k, v in os.environ.items() if k in _NEUTRAL_ENV}
    # PINNED, not omitted. On POSIX `expanduser("~")` falls back to the passwd
    # home when HOME is unset, so omitting it left the guards reading real config
    # and writing the real push allowlist. GENESIS_HOME too: it overrides the
    # ~/.genesis derivation outright, so leaving it ambient reopens the same hole
    # from the other side.
    env["HOME"] = _SANDBOX_HOME
    env["GENESIS_HOME"] = str(Path(_SANDBOX_HOME) / ".genesis")
    env["GIT_CONFIG_GLOBAL"] = os.devnull  # no developer gitconfig (gpgsign etc.)
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    if cwd is not None:
        # The SAME class as HOME, entering through the directory instead of a
        # variable. TMPDIR is allow-listed as "no verdict influence", but it
        # decides where tmp_path lives, and tmp_path is the guard's cwd — so if
        # the temp root happens to sit inside a git repo on main, repo-state
        # rules evaluate against THAT repo. MEASURED: moving --basetemp inside a
        # main-branch repo turns three passing cases into
        # "Direct commits to main are not allowed". A ceiling stops git walking
        # up out of the scratch dir. It must be the PARENT: naming the directory
        # itself does not stop the walk that starts there.
        env["GIT_CEILING_DIRECTORIES"] = str(Path(cwd).parent)
    if dispatched is not None:
        env["GENESIS_CC_SESSION"] = dispatched
    return env


def _names_a_usable_escape(guidance: str) -> bool:
    """Does the refusal name a route that WORKS, for both shapes that reach it?

    Two distinct commands land on this leg and they need different advice:
    a real git invocation that merely quotes badly (re-quote it, or -F <file>),
    and — the dominant one in practice — prose being written to a file whose
    text happens to contain a contraction and the word push or merge. No amount
    of re-quoting a here-doc fixes the second; the route out is to write the
    file with a tool instead. A message that only covers the first is a wall for
    the more common case, so require both.
    """
    covers_command = "-f <file>" in guidance or "plain quotes" in guidance
    covers_prose = "write tool" in guidance or "instead of a here-doc" in guidance
    return covers_command and covers_prose


def test_the_sandbox_home_is_what_the_guard_actually_RESOLVES(tmp_path):
    """SHOULD-FIX 4 — the isolation fix had nothing pinning it.

    MEASURED: reverting the HOME/GENESIS_HOME pin to the real home left this
    suite fully green, so the guards would silently go back to reading the
    developer's config and writing their real push allowlist. A fix no test can
    fail on is a comment with extra steps.

    Assert on the CHILD's resolution rather than on a verdict, because a verdict
    is the same whether the pin works or not — and an empty sandbox directory is
    likewise identical for a working pin and an inert one.
    """
    probe = (
        "import os,sys;"
        f"sys.path.insert(0,{str(_HOOKS_DIR)!r});"
        "import push_allowlist as pa;"
        "print(os.path.expanduser('~'))"
    )
    r = subprocess.run(
        [_PY, "-c", probe],
        capture_output=True,
        text=True,
        timeout=30,
        env=_child_env(),
        cwd=str(tmp_path),
    )
    resolved = r.stdout.strip()
    assert resolved == _SANDBOX_HOME, (
        "the guard child resolves a home that is NOT the sandbox, so ambient "
        f"config can reach it and a write can escape into the real store.\n"
        f"resolved={resolved!r} sandbox={_SANDBOX_HOME!r}\n{r.stderr}"
    )
    assert Path.home() != Path(_SANDBOX_HOME), (
        "CONTROL: the sandbox IS the real home, so the assertion above is "
        "trivially satisfied and proves nothing."
    )


def _unpushed_repo(tmp_path: Path, monkeypatch) -> str:
    """A repo whose branch is NOT on its remote, so `gh pr create` IS gated.

    Two conditions BOTH have to hold, and missing either makes the bypass test
    vacuous — control and variant both return `allow`, which is indistinguishable
    from "the op was never gated here":

    1. (RETRACTED — this fixture rests on condition 2 alone.) The docstring
       used to claim the create gate is scoped to the canonical public repo, and
       that the `_TEST_CANONICAL_PUBLIC_REPO` seam pins it. Neither half holds:
       the seam is set with monkeypatch in the PARENT and the child environment
       is allow-listed, so it never reaches the guard; and the create path
       consults `_pr_create_would_publish`, not the canonical-repo check.
       MEASURED: swapping the remote slug for an unrelated one changes no
       verdict in any cell. The remote URL is still set, deliberately using a
       placeholder slug rather than this install's real one, which would be an
       install-specific value in a tracked test.
    2. `_pr_create_would_publish` only gates a create that might push or fork.
       An unpushed branch on an unreachable host makes the `ls-remote` check
       uncertain, which the guard treats as gated by contract.

    The host is `.invalid` (RFC 2606), so the DNS lookup fails immediately —
    no real network dependency in the test.
    """
    monkeypatch.setenv("_TEST_CANONICAL_PUBLIC_REPO", _TEST_SLUG)

    # Every step is CHECKED, and the end state is ASSERTED. Discarding these
    # return codes made the fixture fail silently: with a developer global
    # carrying `[commit] gpgsign = true`, `git commit` exits 128, the repo ends
    # with no HEAD — and the tests built on it still passed, because the control
    # they feed returns `ask` even for a bare directory that is not a repo at
    # all. A fixture that cannot fail is the same defect as a test that cannot
    # fail. (The gpgsign path is also neutralised at source by _run's
    # GIT_CONFIG_GLOBAL pin, but the fixture asserts its own state regardless.)
    env = _child_env(str(tmp_path))

    def run(*a: str) -> subprocess.CompletedProcess:
        r = subprocess.run(a, cwd=tmp_path, capture_output=True, text=True, env=env)
        assert r.returncode == 0, f"fixture step {a!r} failed ({r.returncode}): {r.stderr}"
        return r

    run("git", "init", "-q", "-b", "feature-x")
    run("git", "config", "user.email", "t@example.invalid")
    run("git", "config", "user.name", "t")
    (tmp_path / "f.txt").write_text("hi")
    run("git", "add", "f.txt")
    run("git", "commit", "-qm", "init")
    run("git", "remote", "add", "origin", f"https://example.invalid/{_TEST_SLUG}.git")

    assert run("git", "rev-parse", "HEAD").stdout.strip(), "fixture repo has no commit"
    branch = run("git", "branch", "--show-current").stdout.strip()
    assert branch == "feature-x", f"fixture branch is {branch!r}, not the unpushed one"
    return str(tmp_path)


def _run(
    script: Path,
    cmd: str,
    cwd: str | None = None,
    *,
    dispatched: str | None = None,
) -> subprocess.CompletedProcess:
    """Run a guard against *cmd* in a KNOWN environment, never the caller's.

    Two properties of the child env are pinned deliberately, because both were
    wrong once and each made the suite report on something other than the code:

    ``cwd`` is applied to BOTH the payload and the child's working directory.
    Gates that read repo state (branch, remote, pushed-ness) use the process
    cwd, so a payload-only cwd silently evaluated them against whatever
    directory pytest ran from — this worktree, whose branch IS pushed. That made
    a `gh pr create` control report "already on the remote" and look un-gated,
    when the guard was correct and the harness was lying.

    ``GENESIS_CC_SESSION`` is STRIPPED unless a test opts in. Forwarding the
    ambient marker meant the suite tested its CALLER's mode: launched from a
    dispatched session it inherited ``=1``, and every case asserting the
    interactive ask direction failed because production correctly hard-denies in
    dispatched mode (measured: 21 failures). CI happens to run non-dispatched,
    so this would never have surfaced there — the suite would just have been
    quietly mode-dependent. Dispatch state is now an explicit argument, so each
    test states the mode it means to exercise.
    """
    payload: dict = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    if cwd is not None:
        payload["cwd"] = cwd
    env = _child_env(cwd, dispatched)
    return subprocess.run(
        [_PY, str(script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        cwd=cwd,
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
        # The probe reads the RAW command deliberately. Ordinary punctuation in
        # quoted multi-line input genuinely shifts analyze()'s segmentation, so
        # this MUST read as a blind spot. Pre-processing the text to quieten the
        # prompt was measured removing the very evidence the probe looks for.
        cmd = f"{GIT} {COMMIT} -F - <<'EOF'\nit's a message with an apostrophe\nEOF"
        assert sp.untokenizable(cmd) is True


# ══════════════════════════════════════════════════════════════════════════
# git_push_guard — the net flips the unparseable cases to BLOCK (verify-RED)
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
        # THE regression that killed the normalizing design, kept as fixture
        # data. The shape is ordinary rather than adversarial, which is the
        # whole point: a normalizer built to quieten common input removed the
        # signal on input a developer writes without thinking about it.
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
# review_enforcement_commit — the net flips the unparseable commit to BLOCK
# ══════════════════════════════════════════════════════════════════════════
class TestGhPrCreateIsCovered:
    """`gh pr create` is the FOURTH gated op; the first cut of the net omitted it.

    Found by an independent adversarial review of the diff, then reproduced:
    an ANSI-C-hidden create on an unpushed branch was ALLOWED while the plain
    form asked. Same fail-open the net exists to close, for an op left out of
    the mention set and out of the segment check.
    """

    def test_plain_create_is_gated_baseline(self, tmp_path, monkeypatch):
        """CONTROL — without this the bypass test below proves nothing.

        If the baseline does not gate, variant and control are both `allow` and
        a 'bypass' result is indistinguishable from 'this op was never gated
        here'. This is the check that caught the first, meaningless version of
        the reproduction.
        """
        cwd = _unpushed_repo(tmp_path, monkeypatch)
        r = _run(_PUSH_GUARD, "gh pr create --title x --body y", cwd=cwd)
        assert _decision(r) in ("ask", "block"), r.stdout + r.stderr

    def test_hidden_create_reaches_a_human(self, tmp_path, monkeypatch):
        """VERIFY-RED: allowed before the fix (mention set lacked any create term)."""
        cwd = _unpushed_repo(tmp_path, monkeypatch)
        r = _run(_PUSH_GUARD, f"{ANSIC}gh pr create --title x --body y", cwd=cwd)
        assert _decision(r) in ("ask", "block"), r.stdout + r.stderr

    def test_create_alone_does_not_trip_the_net(self, tmp_path):
        """The mention test is `gh` AND `create`, not the word `create`.

        MEASURED over 11,488 real commands (328 un-tokenizable): a bare
        `\\bcreate\\b` alternative adds 6 new prompts, all benign here-doc
        Python; the conjunction adds zero. This pins the narrower predicate so a
        later 'simplification' to a single alternation has to fail here first.

        The input has to be UNTOKENIZABLE or this pins nothing: the net fires
        only on the blind path, so with a cleanly-parsing command a broadened
        predicate would change no verdict and this test would pass either way.
        An earlier version used exactly such an input and was therefore vacuous
        with respect to the claim above.
        """
        cmd = "cat > f <<'EOF'\ndon't create the file yet\nEOF"
        r = _run(_PUSH_GUARD, cmd, cwd=str(tmp_path))
        assert _decision(r) == "allow", r.stdout + r.stderr


class TestDispatchMarkerIsExact:
    """The dispatch marker is the exact string "1" — never truthiness.

    `cc/invoker.py` stamps "1"; every other consumer compares exactly
    (git_push_guard._is_dispatched, pretool_check, genesis_stop_hook,
    outcome_verification_hook). A truthiness test reads GENESIS_CC_SESSION=0 —
    an operator explicitly turning it OFF — as dispatched, and hard-BLOCKS a
    benign unparseable command that the interactive path should merely ask
    about. Over-blocking is the failure direction this design exists to avoid.
    """

    # Un-tokenizable, and merely MENTIONS the word — no `git commit` adjacency.
    # The interactive path asks about this; the dispatched path must NOT refuse
    # it, because refusing is unappealable where nobody can confirm.
    _BENIGN = "echo $'don\\'t " + COMMIT + " this'"
    # Un-tokenizable AND carries a real `git commit`. This is what the dispatched
    # deny leg exists for, and what it must still catch after the narrowing.
    _GATED = "echo $'a\\'b)c' && " + GIT + " " + COMMIT + " " + NV + " -m x"

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "2"])
    def test_non_one_values_are_not_dispatched(self, tmp_path, value):
        """VERIFY-RED: every one of these hard-blocked before the exact compare."""
        r = _run(_COMMIT_GUARD, self._BENIGN, cwd=str(tmp_path), dispatched=value)
        assert _decision(r) != "block", f"{value}: {r.stdout + r.stderr}"

    def test_exact_one_is_dispatched(self, tmp_path):
        """CONTROL — the deny leg must still fire for the real marker AND a real op.

        Without this the tests above are satisfied by a guard that never denies
        at all, which is the same shape as a predicate that measures zero false
        positives by never firing.

        Uses the GATED input deliberately. An earlier version of this control
        used the benign one and passed only because the deny leg was
        over-broad — so it would have gone green while the guard refused
        `c.commit()` in a here-doc. A control has to demand the behaviour for a
        case that genuinely warrants it, or it just re-measures the bug.
        """
        r = _run(_COMMIT_GUARD, self._GATED, cwd=str(tmp_path), dispatched="1")
        assert _decision(r) == "block", r.stdout + r.stderr

    def test_benign_mention_when_dispatched_is_refused_RECOVERABLY(self, tmp_path):
        """The cost of the unattended deny leg, pinned rather than wished away.

        This test used to assert `!= "block"` and was described as the lock that
        justified narrowing the deny predicate. Narrowing was abandoned: it does
        not converge, because on the blind path the parser cannot tell an
        executed gated verb from one inside quoted data, which is the premise of
        the net. So this shape IS refused when unattended, deliberately.

        What is asserted instead is that the refusal can be acted on without a
        human — cause and rewrite both named. That is the whole difference
        between an accepted cost and a wedged session, and asserting it is what
        caught the commit gate naming the rewrite only on its interactive `ask`,
        i.e. giving the guidance exclusively to the session that could have
        asked for it.
        """
        r = _run(_COMMIT_GUARD, self._BENIGN, cwd=str(tmp_path), dispatched="1")
        if _decision(r) != "block":
            return  # parseable after all — nothing to recover from
        guidance = r.stderr.lower()
        assert "parse" in guidance and _names_a_usable_escape(guidance), (
            "an unattended refusal must name both the cause and the way out — "
            f"there is no one to ask.\n{r.stderr}"
        )

    def test_unset_is_not_dispatched(self, tmp_path):
        """Absent marker — the helper strips it, so this is the real default."""
        r = _run(_COMMIT_GUARD, self._BENIGN, cwd=str(tmp_path))
        assert _decision(r) != "block", r.stdout + r.stderr

    @pytest.mark.parametrize(
        "key,value",
        [
            # Each entry is an ambient input MEASURED to change a guard verdict
            # through this helper. One test per key, so the next leak fails with
            # an obvious cause instead of as a scattered wave of failures in
            # whichever mode or machine happens to be unlucky.
            ("GENESIS_CC_SESSION", "1"),  # made the suite test its CALLER's mode
            ("GIT_DIR", str(_WORKTREE / ".git")),  # re-points repo-state gates
            ("GIT_WORK_TREE", str(_WORKTREE)),  # ditto, the other half of the pair
            ("CDPATH", "/home"),  # review_enforcement_commit -> _CWD_UNKNOWN deny
        ],
    )
    def test_helper_does_not_inherit_ambient_context(self, tmp_path, monkeypatch, key, value):
        """LOCK: the suite tests its own context, never the caller's.

        Two defects of this exact shape were found by external review rather
        than by the suite — the helper forwarded the caller's environment, and
        separately passed `cwd` only in the payload and never to the child. Both
        made the harness describe a world that was not the one under test, and
        both were fixed one key at a time. This locks the CLASS: the child sees
        an allowlist, so an ambient value cannot reach a guard by default.
        """
        monkeypatch.setenv(key, value)
        r = _run(_COMMIT_GUARD, self._BENIGN, cwd=str(tmp_path))
        assert _decision(r) != "block", (
            f"ambient {key} leaked into the guard subprocess: " + r.stdout + r.stderr
        )


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
        # A legit multi-line message must not trip MY net. The reason is NOT
        # that the body is stripped — nothing strips it any more, and the raw
        # text here is genuinely untokenizable. The net stands down because
        # analyze() still resolved the segment, and the net only fires where it
        # found none. It may be blocked for other reasons, never with the
        # tokenizability message.
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
        """DOCUMENTED RESIDUE — a shape this net does not close.

        Closing this class needs per-occurrence accounting of every gated flag
        against parsed segment spans: more argv-to-effect modelling, which is the
        non-convergent tail this design deliberately avoids. Same accepted bucket
        as eval / dynamic construction (2026-08-12 decision).

        The mechanism is deliberately NOT written out here. This repository is
        public and the shape is not closed, so an explanation of why the guard
        misses it would narrow the search for anyone reading. The assertion stays
        — it locks the residue so a future fix flips it deliberately rather than
        silently — and whoever does that work can derive the reason from the
        code in a minute. Private detail lives in the tracked follow-ups below.
        """
        cmd = f"{GIT} {PUSH} origin main && x=$'a\\'b<<PWN'\n{GIT} {PUSH} origin evil {FORCE}\nPWN"
        r = _run(_PUSH_GUARD, cmd, cwd=str(tmp_path))
        assert _decision(r) != "block", r.stdout + r.stderr
        # SECOND severity, and the reason the continuation tests no longer carry
        # this assertion: the misleading-consent class is still LIVE here. The
        # prompt describes an ordinary push while the shell runs a force push to
        # another ref, so a human approves under the wrong description. Locked so
        # a fix flips it deliberately, exactly as the continuation lock was.
        assert _decision(r) == "ask", r.stdout + r.stderr
        assert FORCE not in r.stdout, (
            "the prompt now names the flag — re-derive this rather than loosening it"
        )

    @pytest.mark.parametrize(
        "label,cmd_tpl",
        [
            # Two DISTINCT parse signatures, which is why this enumerates rather
            # than spot-checking one form:
            #   'space + backslash'    -> segments as ['\\', None]
            #   'no space + backslash' -> segments as [None, None]  (exe is 'git\')
            # A fix keying on the " \" TOKEN shape would close the first two and
            # leave the third silently open. The durable fix — the one this
            # branch ships — is continuation JOINING before segmentation, not
            # token recognition after it, so all three close together.
            ("space_backslash", "{GIT} \\\n{PUSH} origin main {FORCE}"),
            ("space_backslash_then_space", "{GIT} \\\n {PUSH} origin main {FORCE}"),
            ("no_space_backslash", "{GIT}\\\n {PUSH} origin main {FORCE}"),
        ],
    )
    def test_continuation_resolves_exactly_like_the_joined_spelling(
        self, tmp_path, label, cmd_tpl
    ):
        """CLOSED (was DOCUMENTED RESIDUE) — the WHOLE class, not one form.

        The prior lock existed so a future fix would flip this deliberately
        rather than silently. This is that flip: the shared parser now folds an
        unquoted `\\`-newline before segmentation, which is precisely the
        durable fix the prior docstring predicted would be required.

        It was the one residue class where the mis-parse was ALSO a live gate
        bypass: bash joins the continuation before reading the command, so the
        push really ran and no gate ever saw the flag. The ANSI-C `$( )` shapes
        mis-parse too — and, measured, the shell DOES go on to run the following
        command — but the fail-closed net still routes them to a human (locked
        above), which makes them parser defects rather than open bypasses.

        Assert PARITY with the joined spelling AND the same reason. A matching
        verdict is not a matching reason: `_decision` collapses everything to
        one of three labels, so a future tightening of the net could make both
        spellings read `block` for different causes and this would pass with the
        fold reverted. The prior lock's demand was flag ATTRIBUTION, and only
        the reason string tests that.
        """
        cmd = cmd_tpl.format(GIT=GIT, PUSH=PUSH, FORCE=FORCE)
        r = _run(_PUSH_GUARD, cmd, cwd=str(tmp_path))
        r_joined = _run(_PUSH_GUARD, _JOINED_FORCE_PUSH, cwd=str(tmp_path))
        assert _decision(r) == _decision(r_joined) == "block", (
            f"{label}: continued={_decision(r)} joined={_decision(r_joined)}\n"
            + r.stdout
            + r.stderr
        )
        assert r.stderr == r_joined.stderr, (
            f"{label}: the continued spelling blocks for a DIFFERENT reason than "
            f"the joined one\ncontinued: {r.stderr!r}\njoined:    {r_joined.stderr!r}"
        )

    def test_continuation_before_the_flag_keeps_the_flag_attributed(self, tmp_path):
        """The THIRD severity in that class, also closed — and it was the worst.

        `git push \\<NL>origin main {FORCE}` was assumed harmless by two
        independent readings, on the reasoning that `push` still resolves. It
        does — but before this fix the FLAG was severed into the next segment:

            seg[0] argv=['git', 'push', '\\\\']          <- subcommand, no flag
            seg[1] argv=['origin', 'main', '--force']    <- flag, no exe

        so the guard saw an ORDINARY push and emitted `ask`, behind a prompt
        that never mentioned the force. Bash, having joined the continuation
        before reading the command, really did force-push. Consent obtained
        under a description that omits the dangerous flag is worse than a silent
        allow: a silent allow at least leaves no record of the operator having
        agreed to anything.

        The prior lock demanded that the eventual fix address flag ATTRIBUTION,
        not merely subcommand resolution. Folding before segmentation does
        exactly that — the flag now lands in the same segment as the exe — so
        the verdict is the hard block a force-push warrants.

        Scope, so this does not read as more than it is: what is closed here is
        the CONTINUATION spelling. The general "an approval prompt must not
        obtain consent under a description omitting the dangerous flag" concern
        is NOT closed — it survives on the decoy residue above, which is why
        that test carries the prompt-text assertion this one no longer needs.
        """
        cmd = f"{GIT} {PUSH} \\\norigin main {FORCE}"
        r = _run(_PUSH_GUARD, cmd, cwd=str(tmp_path))
        r_joined = _run(_PUSH_GUARD, _JOINED_FORCE_PUSH, cwd=str(tmp_path))
        assert _decision(r) == _decision(r_joined) == "block", (
            f"continued={_decision(r)} joined={_decision(r_joined)}\n"
            + r.stdout
            + r.stderr
        )
        assert r.stderr == r_joined.stderr, (
            f"blocks for a DIFFERENT reason than the joined form\n"
            f"continued: {r.stderr!r}\njoined:    {r_joined.stderr!r}"
        )

    def test_a_benign_continuation_is_not_newly_refused(self, tmp_path):
        """The allow direction, which every other case here leaves untested.

        The parser change is a JOIN, and a join under an incomplete context
        model is the direction that makes a command VANISH — so the fold must
        also leave harmless multi-line commands alone. Asserted end-to-end
        through the guard, not just at the parser level.
        """
        r = _run(_PUSH_GUARD, f"{GIT} \\\nstatus", cwd=str(tmp_path))
        r_joined = _run(_PUSH_GUARD, f"{GIT} status", cwd=str(tmp_path))
        assert _decision(r) == _decision(r_joined) == "allow", (
            f"continued={_decision(r)} joined={_decision(r_joined)}\n"
            + r.stdout
            + r.stderr
        )


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

# Contexts that MAY hide a real, executed gated op from the parser. Two of
# the four are resolved by analyze() for every op, so their cells skip —
# they are kept as locks in case a parser change makes them live, but the
# label would overstate coverage if it read as though all four hide..
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
@pytest.mark.parametrize("dispatched", [None, "1"], ids=["interactive", "dispatched"])
def test_matrix_hidden_gated_op_is_never_silently_allowed(
    op_name, op, ctx_name, wrap, dispatched, tmp_path
):
    """A gated op the PARSER CANNOT SEE must never be silently allowed.

    Run in BOTH modes. The two legs reach the same conclusion by different
    predicates — interactive asks on a broad mention, dispatched refuses only on
    a `git <verb>` adjacency — so a change to either can regress independently.
    Before this axis existed, only the interactive leg was ever exercised.
    """
    cmd = wrap(op)
    if _parser_sees_gated_op(cmd):
        pytest.skip("parser resolved the op — the ordinary gates own this verdict")
    r = _run(_guard_for(op), cmd, cwd=str(tmp_path), dispatched=dispatched)
    assert _decision(r) in ("ask", "block"), (
        f"{ctx_name}/{op_name} (dispatched={dispatched}) was silently ALLOWED\n"
        f"{r.stdout}{r.stderr}"
    )


@pytest.mark.parametrize("op_name,op", _GATED_OPS, ids=[n for n, _ in _GATED_OPS])
@pytest.mark.parametrize("ctx_name,wrap", _INERT, ids=[n for n, _ in _INERT])
@pytest.mark.parametrize("dispatched", [None, "1"], ids=["interactive", "dispatched"])
def test_matrix_inert_mention_is_never_hard_blocked(
    op_name, op, ctx_name, wrap, dispatched, tmp_path
):
    """Nothing gated EXECUTES in these cells. What that entitles them to differs
    by mode, and an earlier version of this test got that wrong.

    Two axes were missing and their absence hid a real regression. The verdict
    was asserted as `!= "block"`, which `ask` satisfies — so when 18 of these 24
    cells moved allow -> ask, the matrix stayed green while the false-positive
    cost it exists to bound tripled unseen. And dispatch mode was not a
    parameter at all, so the cell where those asks became REFUSALS did not exist
    in the grid; it took an external audit to find it.

    With both axes present the dispatched half went red, and the honest reading
    is that the invariant was wrong, not the guard. "A benign shape is never
    hard blocked" cannot hold unattended: there is no one to answer a prompt, so
    the only two options there are refuse or allow-unverified, and a security
    gate does not pick the second. Three rounds of narrowing the predicate were
    spent trying to make the old invariant true before that was accepted — the
    blind path cannot tell an executed verb from a quoted one, which is the
    premise of the net existing, so no predicate over the same degraded parse
    separates these cells from the real thing.

    So the invariant is SCOPED rather than deleted. Interactively a benign shape
    is never refused — an `ask` here is the accepted cost, one confirmation. In
    an unattended session it may be refused, but the refusal must be RECOVERABLE
    on its own: the message has to name the cause and the way out, so a session
    with no human can rewrite the command and proceed. That is what makes this a
    cost rather than a wall, and it is asserted, not assumed.

    The verdict stays `!= "block"` on the interactive side rather than
    `== "allow"` on purpose: the ask RATE is a separate measured quantity (see
    the corpus figures in git_push_guard._GATED_MENTION) and pinning it per-cell
    would freeze a cost decision inside a regression test.
    """
    r = _run(_guard_for(op), wrap(op), cwd=str(tmp_path), dispatched=dispatched)
    decision = _decision(r)
    if dispatched != "1":
        assert decision != "block", (
            f"{ctx_name}/{op_name} (interactive) was HARD BLOCKED — nothing "
            f"gated executes in this cell, and a human is present to answer an "
            f"ask.\n{r.stdout}{r.stderr}"
        )
        return
    # Unattended. A refusal is PERMITTED here — not required. Many of these
    # cells tokenize cleanly, so the net never fires and they are simply
    # allowed, which is correct: nothing gated executes in them. What this half
    # of the matrix pins is the shape of the refusal WHEN one happens. (The
    # "never a silent allow" direction is the bypass matrix's job, not this
    # one's — asserting it here too would duplicate that guarantee and make
    # this test fail for the opposite reason.)
    if decision != "block":
        return
    guidance = r.stderr.lower()
    assert "cannot be parsed" in guidance or "parse" in guidance, (
        f"{ctx_name}/{op_name} (dispatched) refused without naming the CAUSE. "
        f"An unattended session cannot ask why.\n{r.stderr}"
    )
    # APPLICABILITY, not a keyword. `"rewrite" in guidance` passes on a message
    # saying "do not rewrite", and — measured — it passed on a message whose only
    # suggestion did not apply to the command at hand: the dominant real shape
    # reaching this leg is prose being written to a FILE, where re-quoting a
    # here-doc cannot help because the apostrophe is in the prose itself. The
    # refusal has to cover BOTH cases, or an unattended session hits a wall on
    # the common one.
    assert _names_a_usable_escape(guidance), (
        f"{ctx_name}/{op_name} (dispatched) refused without a way out that "
        f"applies. A refusal a session cannot act on is a wall, not a cost.\n"
        f"{r.stderr}"
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
