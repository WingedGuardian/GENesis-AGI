"""A guard whose module-scope import fails must still fail CLOSED.

THE DEFECT THIS LOCKS. ``run_guard`` is called at the BOTTOM of a guard, so an
exception raised while the module is still importing never reaches it. Python exits
1, and Claude Code's PreToolUse contract is "exit 2 blocks; ANY other code is a
non-blocking error, so the tool RUNS". MEASURED before the fix, across every guard
wired here: poison one shared sibling and each went exit 2 -> exit 1, with a
healthy-tree control still blocking at 2. The gate did not degrade — it VANISHED,
silently, while the session still believed it was protected. Version skew between a
worktree and the main tree makes this a real configuration here, not a hypothetical.

THE POPULATION IS ENUMERATED, NOT ASSERTED. An earlier revision of this module said
"all four guards that import ``shell_parse`` at module scope" — and an AST walk found
five more, three of them blocking. That claim now lives in
``test_every_module_scope_shell_parse_importer_is_accounted_for``, which derives the
set from the source and fails on an importer nobody has accounted for, so a new guard
added next year is a failing test rather than a silent hole.

WHY BOTH DIRECTIONS ARE TESTED, and why the benign arm is not padding: a degraded
guard that refused EVERYTHING would satisfy the blocking arm perfectly while wedging
the session. The pair is what makes either half meaningful, and the ``GUARD DEGRADED``
notice is asserted throughout because an operator who cannot tell a degraded allow
from a real one has been told nothing.

These drive the REAL guards as subprocesses against a tree with exactly one poisoned
sibling — not a mocked import — because the property is about what the interpreter
does at module load, which no in-process fake reproduces.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO_ROOT / "scripts"
_HOOKS = _SCRIPTS / "hooks"


# EVERY subprocess gets a hermetic temp HOME, and commands that name a home-relative
# path are built from it. Two reasons, both learned the hard way here:
#   * the commit gate's HEALTHY verdict depends on whether a review MARKER exists under
#     $HOME. Against the developer's real home that verdict flips with whatever the
#     session happened to mark, so a control asserting it was passing or failing on
#     ambient state rather than on this change. (MEASURED: the control failed for
#     exactly that reason before HOME was isolated.)
#   * the protected-path set is derived from the guard's own notion of home, so a
#     command naming the real home would not be protected under a temp one.
# Built at runtime rather than written literally so a recursive-remove of a protected
# path is not sitting as a string in a test file that other tooling scans.
def _protected_rm(home: Path) -> str:
    return " ".join(["rm", "-rf", str(home / "genesis" / "data")])


# (guard_name, path relative to scripts/, command | callable(home), expected_exit, label)
_CASES = [
    ("protected_paths_guard", "hooks/protected_paths_guard.py", _protected_rm, 2, "gated"),
    ("protected_paths_guard", "hooks/protected_paths_guard.py", "ls -la /tmp", 0, "benign"),
    ("git_discard_guard", "hooks/git_discard_guard.py", "git clean -fd", 2, "gated"),
    ("git_discard_guard", "hooks/git_discard_guard.py", "git status", 0, "benign"),
    (
        # The shell runs this as one command, and an ADJACENCY matcher does not see it
        # that way — the starving shape the earlier degraded pattern fell to. The exact
        # bytes ARE the fixture: do not reformat this literal onto one line.
        "git_discard_guard",
        "hooks/git_discard_guard.py",
        "git \\\n  clean -fd",
        2,
        "gated (line-continued)",
    ),
    (
        # The PRICE of matching on a single token, asserted rather than left to be
        # discovered: `make clean` names no git operation and is refused anyway. That
        # is the chosen direction — a loud refusal while the hook tree is broken,
        # against a silent deletion. If this cell ever has to change, the matcher got
        # narrower and the line-continued cell above is the one to re-check.
        "git_discard_guard",
        "hooks/git_discard_guard.py",
        "make clean",
        2,
        "over-block (priced, intended)",
    ),
    (
        # NO WAIVER IS HONOURED ON THIS PATH, and this cell is the one that changed
        # when that was decided. It read `expect 0, waiver honoured` until a review
        # showed the check was a bare substring — see the two decoys below, which is
        # what a substring waiver actually admits. The waiver is a PARSER's judgement
        # and the parser is what is missing; the way through is repairing the tree.
        "git_discard_guard",
        "hooks/git_discard_guard.py",
        "git clean -fd  # discard-override",
        2,
        "waiver NOT honoured",
    ),
    (
        # DECOY, verified by execution before the fix: the sigil is part of an
        # unrelated FILENAME in a later segment, and the substring check allowed an
        # unrecoverable `git clean` on the strength of it.
        "git_discard_guard",
        "hooks/git_discard_guard.py",
        "git clean -fd && echo notes-discard-override.md",
        2,
        "decoy: sigil inside a filename",
    ),
    (
        # DECOY, same measurement: the sigil sits in a QUOTED string in an earlier
        # segment and waived the review gate for the real commit that follows.
        "review_enforcement_commit",
        "review_enforcement_commit.py",
        "echo '# review-override' && git commit -m wip",
        2,
        "decoy: sigil quoted in another segment",
    ),
    (
        "git_push_guard",
        "hooks/git_push_guard.py",
        "git push origin main --force",
        2,
        "gated",
    ),
    ("git_push_guard", "hooks/git_push_guard.py", "git log --oneline -5", 0, "benign"),
    (
        "review_enforcement_commit",
        "review_enforcement_commit.py",
        'git commit -m "x"',
        2,
        "gated",
    ),
    (
        "review_enforcement_commit",
        "review_enforcement_commit.py",
        "git diff --stat",
        0,
        "benign",
    ),
    (
        # Was `expect 0` on the reasoning that refusing a recoverable local commit
        # would strand an operator mid-repair. It does not: repairing a broken sibling
        # module needs no commit, and the substring check that granted the waiver was
        # admitting the quoted decoy above.
        "review_enforcement_commit",
        "review_enforcement_commit.py",
        'git commit -m "x"  # review-override',
        2,
        "waiver NOT honoured",
    ),
    # The two guards an AST enumeration found AFTER this file first claimed the
    # population was complete. Both block in normal operation and both exited 1 — the
    # fail-open — on a poisoned tree.
    ("worktree_cwd_guard", "hooks/worktree_cwd_guard.py", "git worktree remove x", 2, "gated"),
    ("worktree_cwd_guard", "hooks/worktree_cwd_guard.py", "ls -la /tmp", 0, "benign"),
    ("full_suite_guard", "hooks/full_suite_guard.py", "pytest tests/", 2, "gated"),
    ("full_suite_guard", "hooks/full_suite_guard.py", "ls -la /tmp", 0, "benign"),
    # INTRA-TOKEN continuation: the shell removes the pair and runs `git clean -fd`,
    # while a word-boundary matcher over the raw bytes sees `cl` and `ean`. Dropping an
    # adjacency conjunct — the earlier fix — closes the split BETWEEN tokens and does
    # nothing for a split INSIDE one. MEASURED: 6 of 6 wired guards allowed the split
    # spelling of a command each refused whole. One cell per guard, because the matcher
    # is per-guard and a fix proven on one proves nothing about the rest.
    ("git_discard_guard", "hooks/git_discard_guard.py", "git cl\\\nean -fd", 2, "split token"),
    (
        "git_push_guard",
        "hooks/git_push_guard.py",
        "git pu\\\nsh origin main",
        2,
        "split token",
    ),
    (
        "review_enforcement_commit",
        "review_enforcement_commit.py",
        'git comm\\\nit -m "x"',
        2,
        "split token",
    ),
    ("full_suite_guard", "hooks/full_suite_guard.py", "py\\\ntest tests/", 2, "split token"),
    (
        "worktree_cwd_guard",
        "hooks/worktree_cwd_guard.py",
        "git work\\\ntree remove x",
        2,
        "split token",
    ),
]


def _tree(tmp_path: Path, *, poisoned: bool, old_helper: bool = False) -> Path:
    """A standalone scripts/ copy with an optional poisoned parser or old helper.

    Everything is copied rather than symlinked so the poisoned module cannot leak
    back into the real tree, and the poison replaces exactly `shell_parse` — the one
    sibling every wired guard imports at module scope. ``old_helper`` removes exactly
    the newly introduced API, for the REVERSE skew. Either failure therefore comes
    from the intended import boundary and never from unrelated scaffolding.
    """
    variant = "poisoned" if poisoned else "healthy"
    if old_helper:
        variant += "_old_helper"
    root = tmp_path / variant
    (root / "scripts" / "hooks").mkdir(parents=True)
    (root / "scripts" / "lib").mkdir(parents=True)
    for src, dst in (
        (_HOOKS.glob("*.py"), root / "scripts" / "hooks"),
        (_SCRIPTS.glob("*.py"), root / "scripts"),
        ((_SCRIPTS / "lib").glob("*.py"), root / "scripts" / "lib"),
    ):
        for f in src:
            shutil.copy(f, dst / f.name)
    if poisoned:
        boom = 'raise RuntimeError("poisoned sibling")\n'
        (root / "scripts" / "hooks" / "shell_parse.py").write_text(boom)
        (root / "scripts" / "shell_parse.py").write_text(boom)
    if old_helper:
        # Reproduce the pre-PR helper API without carrying a frozen second copy of
        # the whole module. The markers are adjacent to the one newly added function;
        # everything else remains today's real helper implementation.
        helper = root / "scripts" / "hooks" / "hook_input.py"
        source = helper.read_text()
        start = source.index("\ndef degraded_exit(")
        end = source.index("\n\n_BRACE_RE", start)
        helper.write_text(source[:start] + source[end:])
    return root


def _run(root: Path, rel: str, command, home: Path) -> subprocess.CompletedProcess:
    """Drive one guard as a subprocess under a hermetic HOME.

    `command` may be a string or a callable taking the home, so a case that names a
    home-relative path is built against the SAME home the guard will resolve.
    """
    home.mkdir(parents=True, exist_ok=True)
    cmd = command(home) if callable(command) else command
    return subprocess.run(
        [sys.executable, str(root / "scripts" / rel)],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}}),
        capture_output=True,
        text=True,
        cwd=str(root),
        env={**os.environ, "HOME": str(home)},
        timeout=90,
    )


@pytest.mark.parametrize(("guard", "rel", "command", "expected", "label"), _CASES)
def test_degraded_guard_keeps_its_fail_direction(tmp_path, guard, rel, command, expected, label):
    """Both directions, per guard. See the module docstring for why both."""
    res = _run(_tree(tmp_path, poisoned=True), rel, command, tmp_path / "home_bad")
    assert res.returncode == expected, (
        f"{guard} [{label}] exited {res.returncode}, expected {expected}. "
        f"Exit 1 in particular is the FAIL-OPEN this exists to prevent — CC treats "
        f"any non-2 exit as non-blocking.\nstderr: {res.stderr[:400]}"
    )
    notice = res.stderr
    if expected == 0 and res.stdout:
        payload = json.loads(res.stdout)
        hook_output = payload["hookSpecificOutput"]
        assert hook_output["hookEventName"] == "PreToolUse"
        assert "permissionDecision" not in hook_output
        notice = hook_output["additionalContext"]
    assert "GUARD DEGRADED" in notice, (
        f"{guard} [{label}] gave no GUARD DEGRADED notice. A degraded allow that "
        "looks identical to a real one has told the operator nothing."
    )
    if expected == 0:
        assert not res.stderr, "successful degradation notices belong on additionalContext"


@pytest.mark.parametrize(("guard", "rel", "command", "expected", "label"), _CASES)
def test_the_healthy_tree_is_unchanged(tmp_path, guard, rel, command, expected, label):
    """CONTROL — it proves the instrument, and it deliberately does NOT assert
    verdict equality.

    An earlier version of this test DID assert the healthy tree reaches the same exit
    as the degraded one. That was wrong, and the way it was wrong is worth keeping:
    MEASURED, the commit gate exits 0 here on `git commit`, because the scratch tree is
    not a git repository — nothing is staged, so there is no unreviewed change and
    allowing is correct. The degraded path exits 2 on the same command. They differ,
    and they are SUPPOSED to differ: the degraded path cannot parse, so it blocks on a
    mere MENTION of a gated verb, which is strictly more conservative than the real
    guard. Asserting equality was asserting a property the design does not have.

    What this control genuinely establishes, and what the poisoned test needs from it:
      1. the tree builder produces a WORKING guard — without this, a missing-file
         mistake would make every poisoned run "pass" for entirely the wrong reason;
      2. the guard reaches a real verdict (0 or 2) and never exit 1, so the fail-open
         being measured is attributable to the poison and not to the scaffolding;
      3. the degraded path does not fire when the import SUCCEEDS.
    """
    res = _run(_tree(tmp_path, poisoned=False), rel, command, tmp_path / "home_ok")
    assert res.returncode in (0, 2), (
        f"{guard} [{label}] on a HEALTHY tree exited {res.returncode} — not a verdict. "
        "Exit 1 here would mean the scaffolding is broken, and every poisoned result "
        "measured against it would be meaningless."
    )
    assert "GUARD DEGRADED" not in res.stderr, (
        f"{guard} [{label}] reported GUARD DEGRADED on a healthy tree — the degraded "
        "path is firing when the import succeeded."
    )


_GUARDS = sorted({(guard, rel) for guard, rel, _c, _e, _l in _CASES})


@pytest.mark.parametrize(("guard", "rel"), _GUARDS)
@pytest.mark.parametrize(
    ("stdin", "shape"),
    [
        ("", "empty stdin"),
        ("not json at all", "unparseable stdin"),
        ('{"tool_name": "Bash", "tool_input": {}}', "well-formed but no command"),
        ('["not", "an", "object"]', "JSON that is not an object"),
    ],
)
def test_a_payload_that_names_no_command_blocks(tmp_path, guard, rel, stdin, shape):
    """A degraded guard with nothing to look at must BLOCK, not shrug.

    THE DEFECT THIS LOCKS, and why it hid: ``read_payload`` never raises — malformed
    JSON and empty stdin both return ``{}`` — so an unusable payload never reached the
    except-clause that was meant to catch it. It arrived as an empty string, matched no
    gated pattern, and the guard exited 0. The docstring already promised the opposite
    ("we cannot prove the command is harmless, so we block"), so the prose was the spec
    and the code was the defect.

    Parametrized across every guard and every unusable SHAPE rather than one example,
    because the branch lives in shared ``degraded_exit`` — a fix proven on one caller
    proves nothing about the population that actually uses it.

    Scope, stated rather than implied: this is about the DEGRADED path only. A HEALTHY
    guard given empty stdin also exits 0 (MEASURED on the real hooks) — pre-existing,
    a different question, and deliberately not changed here.
    """
    root = _tree(tmp_path, poisoned=True)
    home = tmp_path / "home_empty"
    home.mkdir(parents=True, exist_ok=True)
    res = subprocess.run(
        [sys.executable, str(root / "scripts" / rel)],
        input=stdin,
        capture_output=True,
        text=True,
        cwd=str(root),
        env={**os.environ, "HOME": str(home)},
        timeout=90,
    )
    assert res.returncode == 2, (
        f"{guard} with {shape} exited {res.returncode}. The guard could not establish "
        "what would run and allowed it anyway — the fail-open this whole change exists "
        f"to close, relocated into its own recovery path.\nstderr: {res.stderr[:400]}"
    )
    assert "GUARD DEGRADED" in res.stderr, (
        f"{guard} with {shape} blocked without saying why it was degraded."
    )


@pytest.mark.parametrize(("guard", "rel"), _GUARDS)
def test_a_bash_payload_carrying_only_a_file_path_blocks(tmp_path, guard, rel):
    """`file_path` is not a stand-in for `command`, and treating it as one fails OPEN.

    All four callers are registered under Bash matchers alone, so a Bash payload with
    no `command` is a broken contract. An earlier draft read `file_path` as the command
    text when `command` was absent — which meant such a payload no longer reached the
    empty-payload block and instead ran the gated-mention test against a PATH, which
    names no operation, and exited 0. A field that is never legitimately present here
    must not be a fallback.
    """
    root = _tree(tmp_path, poisoned=True)
    home = tmp_path / "home_fp"
    home.mkdir(parents=True, exist_ok=True)
    res = subprocess.run(
        [sys.executable, str(root / "scripts" / rel)],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"file_path": "/etc/hosts"}}),
        capture_output=True,
        text=True,
        cwd=str(root),
        env={**os.environ, "HOME": str(home)},
        timeout=90,
    )
    assert res.returncode == 2, (
        f"{guard} allowed a Bash payload with no command by reading file_path as one "
        f"(exit {res.returncode}).\nstderr: {res.stderr[:300]}"
    )


@pytest.mark.parametrize(("guard", "rel"), _GUARDS)
def test_the_degraded_allow_reaches_the_model_not_only_stderr(tmp_path, guard, rel):
    """An allowed command under a broken tree must still SAY the tree is broken.

    Claude Code discards stderr from an exit-0 PreToolUse hook (``git_discard_guard``
    records the same constraint), so on the allow path a stderr-only notice is written
    to nobody and a degraded guard is indistinguishable from a healthy one — the exact
    invisibility this whole change exists to end. The channel that IS delivered on
    exit 0 is ``hookSpecificOutput.additionalContext`` on stdout.

    Asserted on the ALLOW path only, deliberately: on exit 2 the reason already reaches
    the model through stderr, and a second copy would be noise.
    """
    root = _tree(tmp_path, poisoned=True)
    home = tmp_path / "home_ctx"
    home.mkdir(parents=True, exist_ok=True)
    res = subprocess.run(
        [sys.executable, str(root / "scripts" / "hooks" / "git_discard_guard.py")],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "git status"}}),
        capture_output=True,
        text=True,
        cwd=str(root),
        env={**os.environ, "HOME": str(home)},
        timeout=90,
    )
    assert res.returncode == 0, f"expected the benign command to be allowed: {res.stderr[:300]}"
    payload = json.loads(res.stdout.strip().splitlines()[-1])
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert payload["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "GUARD DEGRADED" in context, (
        "the allow path emitted an envelope with no degraded notice in it — the one "
        "channel that reaches the model on exit 0 is carrying nothing"
    )


def test_the_discard_guards_allow_names_the_snapshot_it_did_not_take(tmp_path):
    """A guard is not always only a gate, and the allow notice has to say what else went.

    `git_discard_guard`'s main job is the worktree RECOVERY SNAPSHOT it takes before a
    discarding verb — not a refusal at all. MEASURED on a poisoned tree: `git checkout
    -- .` exits 0 with no snapshot written, so uncommitted work is destroyed
    unrecoverably, and the generic notice mentioned only "gates". A reader told the
    gates are off, when what actually went is their undo, has been misled rather than
    informed.

    Asserted on the word SNAPSHOT rather than the full sentence: the wording should be
    free to improve, the fact that the allow names this specific loss should not.
    """
    root = _tree(tmp_path, poisoned=True)
    home = tmp_path / "home_snap"
    home.mkdir(parents=True, exist_ok=True)
    res = subprocess.run(
        [sys.executable, str(root / "scripts" / "hooks" / "git_discard_guard.py")],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "git status"}}),
        capture_output=True,
        text=True,
        cwd=str(root),
        env={**os.environ, "HOME": str(home)},
        timeout=90,
    )
    assert res.returncode == 0, f"expected an allow: {res.stderr[:300]}"
    payload = json.loads(res.stdout.strip().splitlines()[-1])
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "SNAPSHOT" in context.upper(), (
        "the discard guard's degraded allow does not name the recovery snapshot it "
        f"failed to take — the loss a reader most needs to know about.\ngot: {context}"
    )


def test_an_exception_that_cannot_render_itself_still_blocks(tmp_path):
    """The failure message must not be able to cause the failure it reports.

    ``f"{exc}"`` runs ``__str__``, which is arbitrary code. An earlier draft rendered
    the reason BEFORE the protective blocks, so an exception whose ``__str__`` raises
    propagated straight out of ``degraded_exit`` — Python exits 1, Claude Code reads
    that as non-blocking, and the gated command runs. The fail-open reached through
    the error message of the fix for the fail-open.
    """
    root = _tree(tmp_path, poisoned=True)
    (root / "scripts" / "hooks" / "shell_parse.py").write_text(
        "class _Unprintable(Exception):\n"
        "    def __str__(self):\n"
        "        raise RuntimeError('cannot render')\n"
        "raise _Unprintable()\n"
    )
    home = tmp_path / "home_unprintable"
    home.mkdir(parents=True, exist_ok=True)
    res = subprocess.run(
        [sys.executable, str(root / "scripts" / "hooks" / "git_discard_guard.py")],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "git clean -fd"}}),
        capture_output=True,
        text=True,
        cwd=str(root),
        env={**os.environ, "HOME": str(home)},
        timeout=90,
    )
    assert res.returncode == 2, (
        f"an unrenderable import error produced exit {res.returncode} — exit 1 here is "
        f"the fail-open, reached through the message.\nstderr: {res.stderr[:300]}"
    )


def _run_degraded_helper(tmp_path: Path, body: str) -> subprocess.CompletedProcess:
    """Drive ``degraded_exit`` itself in a FRESH PROCESS.

    Not a convenience: the helper leaves by ``os._exit``, so calling it in-process
    would take the test runner with it. A subprocess is the only way to observe the
    exit code of a function whose contract is that nothing can change that code.
    """
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, 'scripts/hooks'); import hook_input; " + body,
        ],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        env={**os.environ, "HOME": str(tmp_path / "home_helper")},
        timeout=90,
    )


def test_a_matcher_that_cannot_answer_blocks(tmp_path):
    """The only thing between an unanswerable matcher and a fail-open.

    ``gated`` is a public parameter, so a caller can pass a pattern that makes the
    search raise. The except arm answers that with a BLOCK, and an audit's mutation
    sweep found the arm SURVIVED being flipped to ``hit = False``: nothing pinned the
    one branch whose whole job is to refuse when the crude check cannot even run.

    The command is `ls`, which names nothing gated — so an ALLOW here would LOOK
    correct. That is exactly why it needs pinning: the wrong answer and the right one
    are indistinguishable from the outside.
    """
    body = (
        "hook_input.read_payload=lambda: {'command': 'ls'}; "
        "hook_input.re.search=lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom')); "
        "hook_input.degraded_exit('probe', gated=r'\\bnever\\b', exc=RuntimeError('poison'))"
    )
    res = _run_degraded_helper(tmp_path, body)
    assert res.returncode == 2, (
        f"a matcher that could not answer allowed the command (exit {res.returncode})"
    )


@pytest.mark.parametrize(
    ("stream", "command", "expected"),
    [("stderr", "git push origin main", 2), ("stdout", "git status", 0)],
)
def test_broken_output_stream_cannot_change_the_verdict(tmp_path, stream, command, expected):
    """A diagnostic that cannot be written must not decide the verdict.

    Both directions and both streams: a broken stderr must not turn a BLOCK into
    something else, and a broken stdout must not turn an ALLOW into one. This is also
    what pins ``os._exit`` — under ``sys.exit`` the interpreter retries the failed
    flush during shutdown and can replace the status with 120, which is not 2, so a
    closed stream would still convert a block into a non-block after every write had
    been carefully suppressed.

    (From the concurrent session that reached this branch independently; kept because
    it pins a failure mode this side had not considered.)
    """
    body = (
        "Broken=type('Broken',(),{'write':lambda self,value: (_ for _ in ()).throw(OSError('broken')),"
        "'flush':lambda self: (_ for _ in ()).throw(OSError('broken'))}); "
        f"sys.{stream}=Broken(); "
        f"hook_input.read_payload=lambda: {{'command': {command!r}}}; "
        "hook_input.degraded_exit('test', gated=r'\\bpush\\b', exc=RuntimeError('poison'))"
    )
    res = _run_degraded_helper(tmp_path, body)
    assert res.returncode == expected, (
        f"a broken {stream} changed the verdict to {res.returncode}, expected {expected}"
    )


def test_legacy_override_keyword_is_accepted_but_cannot_waive(tmp_path):
    """Version skew must not turn a signature change into a fail-open.

    No sigil is honoured here, but the parameter is still ACCEPTED — because the
    guards resolve from the main tree while a worktree can hold a different copy, so
    an older caller can be paired with this newer helper. Removing the parameter makes
    that pairing a ``TypeError`` at import, which exits 1, which Claude Code reads as
    NON-BLOCKING: tidying the signature would have reintroduced the fail-open through
    the very version skew that makes this bug reachable.

    (From the concurrent session; this side had removed the parameter outright.)
    """
    body = (
        "hook_input.read_payload=lambda: {'command': 'git commit -m x # review-override'}; "
        "hook_input.degraded_exit('test', gated=r'\\bcommit\\b', "
        "override_sigils=('review-override',), exc=RuntimeError('poison'))"
    )
    res = _run_degraded_helper(tmp_path, body)
    assert res.returncode == 2, (
        "an old-style caller either crashed the helper or had its sigil honoured; "
        f"exit {res.returncode}"
    )


@pytest.mark.parametrize(("guard", "rel"), _GUARDS)
def test_a_test_importing_a_broken_tree_sees_the_real_error(tmp_path, guard, rel):
    """Degrading is for the LIVE hook, never for an importer.

    The guards degrade only under ``__name__ == "__main__"``. A test or tool that
    imports a broken tree must get the traceback, not a process exit — otherwise a
    broken dependency is invisible to exactly the machinery meant to catch it.

    Parametrized over every wired guard, because the carve-out is a separate copy in
    each of them and an audit's mutation sweep found that deleting one copy left the
    suite fully green. This module argues elsewhere that a fix proven on one caller
    proves nothing about the population; that argument applies to its own tests.
    """
    root = _tree(tmp_path, poisoned=True)
    module = Path(rel).stem
    res = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys; sys.path.insert(0, 'scripts/hooks'); "
            f"sys.path.insert(0, 'scripts'); import {module}",
        ],
        capture_output=True,
        text=True,
        cwd=str(root),
        env={**os.environ, "HOME": str(tmp_path / f"home_imp_{module}")},
        timeout=90,
    )
    assert res.returncode == 1, f"{guard}: an importer should get the exception, not a guard exit"
    assert "poisoned sibling" in res.stderr, (
        f"{guard}: the real cause must reach the importer; got:\n" + res.stderr[-400:]
    )
    assert "GUARD DEGRADED" not in res.stderr, (
        f"{guard}: the degraded path fired for an importer — it is scoped to __main__ only"
    )


def test_git_push_guards_check_pr_cli_does_not_degrade(tmp_path):
    """`--check-pr` is a HUMAN-run read that takes no stdin.

    Degrading there would block on a terminal read and then exit 2 at someone who only
    asked a question, so that path re-raises instead. Pinned because the carve-out is
    easy to drop in a refactor and its absence would only show up as a hang.
    """
    root = _tree(tmp_path, poisoned=True)
    res = subprocess.run(
        [sys.executable, str(root / "scripts" / "hooks" / "git_push_guard.py"), "--check-pr", "1"],
        input="",
        capture_output=True,
        text=True,
        cwd=str(root),
        env={**os.environ, "HOME": str(tmp_path / "home_cli")},
        timeout=90,
    )
    assert "GUARD DEGRADED" not in res.stderr, (
        "the CLI path degraded; it must re-raise so a human sees the real error"
    )
    assert "poisoned sibling" in res.stderr, (
        "the CLI path should surface the import error; got:\n" + res.stderr[-400:]
    )


#: Guards wired to ``degraded_exit``. Derived from _CASES rather than retyped, so the
#: two lists cannot drift apart.
_WIRED = {Path(rel).name for _g, rel, _c, _e, _l in _CASES}

#: NOT wired, each with the reason it is not — and the reason is a MEASUREMENT, not a
#: judgement, because an exemption whose ground can shift is a hole with a comment on
#: it. Rates are over 74,282 unique real Bash commands harvested from this install.
#:
#: The advisory hooks that used to sit here are gone from the table, because the scan
#: no longer reaches them: it enumerates BLOCKING guards, and a hook with no exit-2
#: path was never in the population to begin with. That is the difference between an
#: exemption and a question that was asked wrongly.
_NOT_WIRED = {
    # Its remaining bare import is `shell_parse`, and its only usable degraded token
    # would be the pipe character — 52,220 of 74,282 commands (70.30%). Wiring it would
    # not make the broken state safe, it would make it UNREPAIRABLE, and a degraded
    # matcher that refuses the majority of ordinary work is not a fail-closed option at
    # all. What its refusal protects is also the mildest in this set: a backgrounded
    # pipeline whose stdout is swallowed, i.e. a re-run. Its `hook_input` import IS
    # wrapped, because there the failed module is the one that would read the payload
    # and there is no matcher to choose — no token, no trade, no exemption.
    "background_pipe_guard.py": "only token is `|` at 70.30% — would wedge the repair",
}


def test_every_module_scope_shell_parse_importer_is_accounted_for():
    """ALLOWLIST, so the next bare importer fails by CONSTRUCTION rather than by luck.

    THIS TEST EXISTS BECAUSE ITS ABSENCE SHIPPED A FALSE CLAIM. An earlier revision of
    this change stated — in this file, in the shared helper's docstring, in a public
    changelog and in the pull request body — that the four guards it patched were the
    whole population, "established by reading their import blocks, not by spot-check".
    It was a spot-check. An AST enumeration found FIVE more, three of them blocking
    guards, every one exiting 1 on a poisoned tree. `grep` cannot separate a guarded
    import from a bare one, which is how a careful reading still missed them.

    Polarity is ALLOWLIST, the same shape as `test_hook_output_contract.py`: a guard
    added next year is unaccounted-for by default and fails here. A scan for known-bad
    patterns could not do that, because the thing it would have to know about does not
    exist yet.

    Module scope ONLY — iterating ``tree.body`` rather than ``ast.walk`` — because an
    import inside a ``try:`` lives in a ``Try`` node, and that is precisely the guarded
    form this is checking for.
    """
    import ast

    files = [*_HOOKS.glob("*.py"), *_SCRIPTS.glob("*.py")]
    siblings = {p.stem for p in files} | {p.stem for p in (_SCRIPTS / "lib").glob("*.py")}

    found: dict[str, set[str]] = {}
    for path in files:
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover — a syntax error is another test's job
            continue
        text = path.read_text()
        # A guard is BLOCKING if it can ever exit 2. Derived from the source rather than
        # from a hand-kept list, because a hand-kept list is the thing that was wrong.
        if not any(marker in text for marker in ("sys.exit(2)", "return 2", "os._exit(2)")):
            continue
        bare: set[str] = set()
        for node in tree.body:  # module scope ONLY — a guarded import is in a Try node
            if isinstance(node, ast.ImportFrom) and node.module in siblings:
                bare.add(node.module)
            elif isinstance(node, ast.Import):
                bare.update(a.name for a in node.names if a.name in siblings)
        if bare:
            found[path.name] = bare

    assert found or _NOT_WIRED, (
        "the walk found no blocking guard with a bare sibling import AND the exemption "
        "table is empty — an empty population on both sides makes this pass forever"
    )
    unaccounted = {name: sorted(deps) for name, deps in found.items() if name not in _NOT_WIRED}
    assert not unaccounted, (
        f"{unaccounted} are BLOCKING guards with a bare module-scope sibling import. An "
        "exception during that import never reaches run_guard, so the process exits 1 — "
        "which Claude Code reads as NON-BLOCKING, and the guard vanishes instead of "
        "degrading. Wrap the import (call hook_input.degraded_exit where the failed "
        "module is a parser; refuse locally where the failed module IS hook_input, since "
        "nothing it could import can recover it), or add the file to _NOT_WIRED with the "
        "MEASURED reason it does not need one."
    )
    # Both directions: a guard that gains the wiring must leave _NOT_WIRED, or the
    # exemption silently outlives its reason. A bare count could not catch that.
    stale = set(_NOT_WIRED) & _WIRED
    assert not stale, f"{sorted(stale)} are wired now and must leave _NOT_WIRED"


@pytest.mark.parametrize(
    "command",
    [
        "git commit --no-verify -m x",
        "git commit --no-verify;",
        "git commit --no-verify&",
        "git commit --no-verify|cat",
        "(git commit --no-verify)",
        "git push --force;",
    ],
)
@pytest.mark.parametrize("constant", ["_GATED_MENTION", "_DEGRADED_GATED"])
def test_a_flag_mention_is_not_starved_by_the_separator_after_it(command, constant):
    """A narrowing conjunct inside the very pattern whose comment forbids them.

    Both of the push guard's mention sets required whitespace, `=` or end-of-string
    AFTER a destructive flag. Every ordinary shell separator therefore starved them:
    MEASURED on the literal patterns, `--no-verify -m x` matched while `--no-verify;`,
    `--no-verify&`, `--no-verify|cat` and `(… --no-verify)` did not. A word boundary
    asks the one thing that was meant — that the flag is a whole token — without
    naming what may follow it.

    BOTH constants, because the defect was in both and fixing the degraded copy alone
    would have left the LIVE net starved under a comment claiming the class was
    closed. Cost of the widening, measured over 74,282 real commands: +50 (+0.07%).

    The first case is the control: it matched before the fix too, so a pattern that
    stopped matching anything at all would fail here rather than pass.
    """
    import importlib

    sys.path.insert(0, str(_HOOKS))
    guard = importlib.import_module("git_push_guard")
    pattern = getattr(guard, constant)
    rx = pattern if hasattr(pattern, "search") else __import__("re").compile(pattern)
    assert rx.search(command), (
        f"{constant} did not see the flag in {command!r} — a shell separator starved "
        "the mention set, and an unseen mention is an unverified publish"
    )


@pytest.mark.parametrize(("guard", "rel"), _GUARDS)
@pytest.mark.parametrize("command", ["git status", "git push origin main --force"])
def test_new_guard_with_old_hook_input_fails_closed(tmp_path, guard, rel, command):
    """Reverse skew cannot fail before the degraded shell-parser recovery exists."""
    root = _tree(tmp_path, poisoned=False, old_helper=True)
    res = _run(root, rel, command, tmp_path / "home_reverse_skew")
    assert res.returncode == 2, (
        f"{guard} with a pre-degraded_exit helper returned {res.returncode}; "
        "non-2 lets Claude Code run the command"
    )
    assert "shared hook_input is incompatible" in res.stderr


@pytest.mark.parametrize(
    ("rel", "message"),
    [
        ("hooks/git_push_guard.py", "native approval helper is incompatible"),
        ("review_enforcement_commit.py", "native approval helper is incompatible"),
    ],
)
def test_new_guard_with_old_tree_missing_native_approval_fails_closed(tmp_path, rel, message):
    root = _tree(tmp_path, poisoned=False)
    (root / "scripts" / "hooks" / "native_approval.py").unlink()
    res = _run(root, rel, "git status", tmp_path / f"home_{Path(rel).stem}")
    assert res.returncode == 2
    assert message in res.stderr


def test_commit_guard_with_old_tree_missing_deadline_helper_fails_closed(tmp_path):
    root = _tree(tmp_path, poisoned=False)
    (root / "scripts" / "review_deadline.py").unlink()
    res = _run(
        root,
        "review_enforcement_commit.py",
        "git status",
        tmp_path / "home_missing_deadline",
    )
    assert res.returncode == 2
    assert "deadline helper is incompatible" in res.stderr


def test_review_state_keeps_its_safe_timeout_fallback_on_old_tree(tmp_path):
    root = _tree(tmp_path, poisoned=False)
    (root / "scripts" / "review_deadline.py").unlink()
    res = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                f"sys.path.insert(0, {str(root / 'scripts')!r}); "
                "import review_state; "
                "print(review_state._deadline_timeout(0.0, 8.0))"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "0.001"


def test_check_pr_with_old_hook_input_surfaces_the_import_error(tmp_path):
    """The human read-only CLI keeps its existing no-degradation carve-out."""
    root = _tree(tmp_path, poisoned=False, old_helper=True)
    res = subprocess.run(
        [sys.executable, str(root / "scripts" / "hooks" / "git_push_guard.py"), "--check-pr", "1"],
        input="",
        capture_output=True,
        text=True,
        cwd=str(root),
        env={**os.environ, "HOME": str(tmp_path / "home_old_helper_cli")},
        timeout=90,
    )
    assert res.returncode == 1
    assert "cannot import name 'degraded_exit'" in res.stderr
    assert "GUARD DEGRADED" not in res.stderr


@pytest.mark.parametrize(
    ("raw", "joined", "why"),
    [
        ("git cl\\\nean -fd", "git clean -fd", "odd run: the shell removes the pair"),
        ("git \\\n  clean -fd", "git   clean -fd", "between tokens: spaces around it stay"),
        ("a\\\r\nb", "ab", "CRLF form"),
        (
            "echo a \\\\\n  b",
            "echo a \\\\\n  b",
            "EVEN run: the last backslash is literal and the newline really separates",
        ),
        ("git clean -fd", "git clean -fd", "control: nothing to join"),
        ("printf 'a\\tb'", "printf 'a\\tb'", "control: an escape that is not a newline"),
    ],
)
def test_the_continuation_join_matches_the_shell(raw, joined, why):
    """DELETED, not replaced with a space, and only for ODD-length runs.

    `a\\<newline>b` is one word `ab` to the shell — #1547 shipped the fold-to-space
    version and it was itself the bug, gluing the wrong things together and allowing a
    destructive command. And in an EVEN run each backslash is escaped by its
    neighbour, so the last is a literal character and the newline after it really does
    separate two commands; joining there would be a pure over-block.

    The two control rows matter as much as the rest: a function that returned its
    input unchanged would pass every joining row if the expectations were sloppy, and
    one that stripped every backslash would pass the joins and fail the controls.
    """
    sys.path.insert(0, str(_HOOKS))
    import hook_input

    assert hook_input._join_continuations(raw) == joined, why


@pytest.mark.parametrize(
    ("guard", "rel", "payload"),
    [
        (
            "pretool_check",
            "pretool_check.py",
            {"tool_name": "Write", "tool_input": {"file_path": ".claude/settings.json"}},
        ),
        (
            "destructive_command_guard",
            "hooks/destructive_command_guard.py",
            {"tool_name": "Bash", "tool_input": {"command": "rm -rf ~/genesis"}},
        ),
        (
            "repo_routing_guard",
            "hooks/repo_routing_guard.py",
            {"tool_name": "Bash", "tool_input": {"command": "git push origin main"}},
        ),
        (
            "background_pipe_guard",
            "hooks/background_pipe_guard.py",
            {"tool_name": "Bash", "tool_input": {"command": "ls | wc -l"}},
        ),
    ],
)
def test_a_blocking_guard_whose_hook_input_is_broken_still_blocks(tmp_path, guard, rel, payload):
    """The dependency that cannot be recovered from, because it IS the recovery module.

    `degraded_exit` lives in `hook_input`, so a guard whose `hook_input` import fails
    has nothing to fall back on and must refuse outright. MEASURED before this: these
    four blocking guards imported it bare and exited 1 on a poisoned copy — including
    the CRITICAL-path Write/Edit gate, which would have permitted an autonomous edit to
    `.claude/settings.json`.

    THIS IS THE POPULATION QUESTION ASKED CORRECTLY, at the third attempt. The first
    asked "who imports shell_parse bare" and answered four. The second asked the same
    question properly and answered nine. The right question is "what module-scope
    dependency does each BLOCKING guard have" — and the answer includes `hook_input`
    itself, which the previous two framings could not see.
    """
    root = _tree(tmp_path, poisoned=False)
    boom = 'raise RuntimeError("poisoned helper")\n'
    (root / "scripts" / "hooks" / "hook_input.py").write_text(boom)
    home = tmp_path / f"home_hi_{guard}"
    home.mkdir(parents=True, exist_ok=True)
    res = subprocess.run(
        [sys.executable, str(root / "scripts" / rel)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=str(root),
        env={**os.environ, "HOME": str(home), "GENESIS_CC_SESSION": "1"},
        timeout=90,
    )
    assert res.returncode == 2, (
        f"{guard} exited {res.returncode} with an unimportable hook_input. Exit 1 is "
        f"the fail-open: Claude Code reads any non-2 code as non-blocking.\n"
        f"stderr: {res.stderr[:300]}"
    )


def test_hook_input_stays_stdlib_only(tmp_path):
    """`degraded_exit` lives in the one module 19 hooks import BARE.

    A non-stdlib import here is a fatal dependency for every one of them — the exact
    failure this whole change exists to recover from, relocated one layer down.
    """
    import ast

    tree = ast.parse((_HOOKS / "hook_input.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    outside = imported - set(sys.stdlib_module_names)
    assert not outside, f"hook_input.py must import only stdlib; found {sorted(outside)}"


# --- Leg B: the degraded refusal must not cost the OPERATOR the repair path -----
#
# `pretool_check` blocks Write/Edit to CRITICAL paths in AUTONOMOUS sessions only —
# an interactive session is allowed through by design ("the user is present and
# sovereign"). Its healthy session test is `os.environ.get("GENESIS_CC_SESSION")`,
# which needs nothing from `hook_input`. Degraded, it used to exit 2 BEFORE reaching
# that test, so it blocked the interactive owner from editing ANYTHING — a category
# it was built never to block, and the one that repairs the tree. With every Bash guard
# that is not declared advisory ALSO refusing, the session could neither run a command
# nor edit a file: on a headless box, a brick. The size of that refusal is deliberately
# not written here — see `test_every_bash_hook_declares_its_degrade_direction` below,
# which derives it from the settings on every run. A figure in a comment is a figure
# that goes stale the next time anyone wires a hook, which is exactly what happened to
# the three that preceded this sentence.
#
# FIVE cases follow. The first four are one 2x2 (dispatched|interactive) x
# (healthy|degraded); the healthy row is the control that makes the claim "zero security
# delta" checkable rather than asserted, since whatever the guard permits when healthy it
# must still permit. The fifth is outside that grid and locks predicate PARITY: a non-"1"
# stamp must read as interactive in BOTH paths, which no cell of the 2x2 can see because
# every cell passes one of the two canonical values.

_CRITICAL_WRITE = {"tool_name": "Write", "tool_input": {"file_path": ".claude/settings.json"}}


def _pretool(root: Path, home: Path, *, dispatched: bool) -> subprocess.CompletedProcess:
    """Drive the real pretool_check as a subprocess, with the session stamp controlled.

    GENESIS_CC_SESSION is STRIPPED rather than left to the ambient environment for the
    interactive arm: a dispatched test runner carries the stamp, which would silently
    turn the interactive case into a second copy of the dispatched one and make the
    whole pair vacuous.
    """
    env = {k: v for k, v in os.environ.items() if k != "GENESIS_CC_SESSION"}
    env["HOME"] = str(home)
    if dispatched:
        env["GENESIS_CC_SESSION"] = "1"
    return subprocess.run(
        [sys.executable, str(root / "scripts" / "pretool_check.py")],
        input=json.dumps(_CRITICAL_WRITE),
        capture_output=True,
        text=True,
        cwd=str(root),
        env=env,
        timeout=90,
    )


def _broken_hook_input(tmp_path: Path, name: str) -> tuple[Path, Path]:
    """A scripts/ copy whose hook_input raises at import — Leg B's exact condition."""
    root = _tree(tmp_path, poisoned=False)
    (root / "scripts" / "hooks" / "hook_input.py").write_text(
        'raise RuntimeError("poisoned helper")\n'
    )
    home = tmp_path / name
    home.mkdir(parents=True, exist_ok=True)
    return root, home


def test_degraded_pretool_check_still_refuses_a_dispatched_write(tmp_path):
    """The protection this guard exists for survives the degrade. Unattended, keep refusing.

    This is the arm that must NOT change, and it is why the fix reads the environment
    instead of simply allowing: a dispatched session has nobody to approve anything.
    """
    root, home = _broken_hook_input(tmp_path, "home_legb_dispatched")
    res = _pretool(root, home, dispatched=True)
    assert res.returncode == 2, (
        "a dispatched session must still be refused when hook_input is unimportable; "
        f"got {res.returncode}. stderr: {res.stderr[:300]}"
    )
    assert "GUARD DEGRADED" in res.stderr, "a degraded refusal must say so"


def test_degraded_pretool_check_leaves_the_interactive_repair_path_open(tmp_path):
    """THE REGRESSION THIS LOCKS: the degraded guard must not block the owner's edits.

    Exit 0 here is not a relaxation. A healthy interactive session is ALREADY allowed
    through this guard unconditionally (see the healthy control below), so refusing one
    while degraded is strictly MORE than the guard was ever scoped to do — and what it
    costs is the ability to repair the very file that is broken.
    """
    root, home = _broken_hook_input(tmp_path, "home_legb_interactive")
    res = _pretool(root, home, dispatched=False)
    assert res.returncode == 0, (
        "an interactive session must keep its repair path when hook_input is "
        f"unimportable; got {res.returncode}, which blocks the edit that fixes it. "
        f"stderr: {res.stderr[:300]}"
    )
    # STDOUT, not stderr: Claude Code discards stderr from an exit-0 hook, so a
    # notice written there would be delivered to nobody and the degraded allow would
    # be indistinguishable from a healthy one. additionalContext is the exit-0 channel.
    assert "GUARD DEGRADED" in res.stdout, (
        "a degraded ALLOW is the one that most needs saying so, and on exit 0 only "
        f"additionalContext on stdout reaches the model. stdout: {res.stdout[:200]!r}"
    )
    assert res.stderr.strip() == "", (
        "the allow path must not ALSO write stderr: on exit 0 that copy goes nowhere "
        f"while appearing delivered. stderr: {res.stderr[:200]!r}"
    )
    json.loads(res.stdout)  # the hand-rolled literal must be valid JSON


def test_healthy_pretool_check_still_refuses_a_dispatched_critical_write(tmp_path):
    """Control, healthy tree: the guard's actual job, unchanged by this fix."""
    root = _tree(tmp_path, poisoned=False)
    home = tmp_path / "home_legb_healthy_dispatched"
    home.mkdir(parents=True, exist_ok=True)
    res = _pretool(root, home, dispatched=True)
    assert res.returncode == 2, (
        f"healthy dispatched CRITICAL-path write must be refused; got {res.returncode}"
    )


def test_healthy_pretool_check_allows_the_interactive_critical_write(tmp_path):
    """Control, and the evidence for the zero-security-delta claim.

    Healthy, this guard ALREADY allows an interactive CRITICAL-path write. The degraded
    branch returning the same verdict therefore surrenders no protection that existed —
    it stops withholding one that never did. Without this row, "zero delta" would be a
    sentence in a PR body rather than a measurement.
    """
    root = _tree(tmp_path, poisoned=False)
    home = tmp_path / "home_legb_healthy_interactive"
    home.mkdir(parents=True, exist_ok=True)
    res = _pretool(root, home, dispatched=False)
    assert res.returncode == 0, (
        "healthy interactive CRITICAL-path write is allowed by design (the user is "
        f"present and sovereign); got {res.returncode}"
    )
    # rc==0 ALONE would also be returned on an unread payload (main() returns 0 when
    # file_path is empty), so this row — the one cited as the zero-delta measurement —
    # would pass without the guard ever reaching its scope check. The degraded path is
    # the only one that writes to stdout, so silence proves the HEALTHY route ran.
    assert res.stdout.strip() == "", (
        f"a healthy allow must be silent; stdout means the degraded branch ran: "
        f"{res.stdout[:200]!r}"
    )


def test_degraded_pretool_check_reads_the_stamp_exactly_as_the_healthy_path_does(tmp_path):
    """Predicate parity: a non-`"1"` stamp is INTERACTIVE in both paths, or neither.

    The degraded branch re-asks the guard's own scope question, and the whole
    zero-delta argument rests on it asking the SAME question. `_is_dispatched` tests
    `== "1"` exactly, so `"true"` is interactive there; if the degraded branch used a
    looser test (truthiness, or `is not None`) it would REFUSE where the healthy guard
    ALLOWS — a divergence invisible to every other row here, because they only ever
    pass the two canonical values.
    """
    root, home = _broken_hook_input(tmp_path, "home_legb_parity")
    env = {k: v for k, v in os.environ.items() if k != "GENESIS_CC_SESSION"}
    env["HOME"] = str(home)
    env["GENESIS_CC_SESSION"] = "true"  # set, but not the literal the stamper writes
    res = subprocess.run(
        [sys.executable, str(root / "scripts" / "pretool_check.py")],
        input=json.dumps(_CRITICAL_WRITE),
        capture_output=True,
        text=True,
        cwd=str(root),
        env=env,
        timeout=90,
    )
    assert res.returncode == 0, (
        'GENESIS_CC_SESSION="true" is NOT the dispatched stamp (invoker writes the '
        f"literal \"1\"), so the healthy guard treats it as interactive and the "
        f"degraded branch must agree; got {res.returncode}"
    )


# ── Every hook that can fire on Bash must DECLARE its degrade direction ────────
#
# WHY THIS EXISTS, and why it is an allowlist rather than a count.
#
# The doctrine used to carry a hand-written figure ("six guards", then "9 of 13")
# for how much of the Bash surface refuses when `hook_input` is unimportable. That
# number was wrong three times running, and then went stale a fourth time WITHOUT
# anyone being wrong: another PR wired one more hook on matcher `Bash`, and a
# denominator written in prose cannot notice that. A number maintained by hand in
# four places is not a measurement, it is four chances to be out of date.
#
# So the number is no longer written down. It is DERIVED here, on every run, from
# `.claude/settings.json` — parsed, never grepped — and what is asserted is the
# RULE the number was only ever evidence for.
#
# THE RULE, derived by execution rather than assumed. The first hypothesis, "a hook
# that imports hook_input blocks", was REFUTED: every hook that can fire on Bash
# imports it at module scope, including every non-blocker. The real split is whether
# the module carries a degraded HANDLER:
#
#   A hook that can fire on Bash either
#     (a) carries a degraded handler, emits `GUARD DEGRADED` and exits 2, or
#     (b) is NAMED below - in _ADVISORY_BY_DESIGN with the words from its own
#         docstring, or in _NOT_PYTHON_ON_BASH because it never imports the module.
#
# Category (b) is a SILENT FAIL-OPEN: Claude Code treats a non-2 exit as a
# non-blocking error WHEN THE HOOK EMITS NO `permissionDecision`, so a guard that
# dies on its import traceback exits 1 and the tool call proceeds. That is correct
# for a hook whose contract is advisory, and a latent hole for anything else.
#
# THE POPULATION FILTER IS WHERE THE FAIL-OPEN LIVES, so it gets the care the
# assertion gets. An earlier version of this gate compared `matcher != "Bash"` —
# an exact-string test against a field Claude Code treats as a REGEX. Measured
# against mutated copies of the real settings, that version could not see a hook
# wired `"Bash|Edit"` or `".*"`, and silently DROPPED an existing blocker respelled
# as a bare `python3 .../guard.py`, leaving the suite green with one fewer guard
# under test. `procedure_advisor.py` is wired on `".*"` in this very repo, so that
# was not a hypothetical future gap — it was a live one.
#
# Two consequences, both deliberate: the matcher is evaluated as a regex, and a
# command that cannot be resolved to a script is a FAILING row rather than a
# skipped one. An unparseable hook is exactly what an allowlist must not wave
# through (the same rule, and the same resolver shape, as
# tests/test_scripts/test_hook_output_contract.py::_resolve).
#
# Deliberately NOT keyed on a docstring keyword scan: that was tried and it
# over-matches — "advisory" also appears in git_discard_guard, git_push_guard and
# repo_routing_guard, all of which DO refuse. An automatic oracle that misclassifies
# three of nine is worse than an explicit list someone had to think about. The
# quotes below are verified VERBATIM against each docstring by a test, so the
# justification cannot drift into invention.

#: Hooks that can fire on Bash but are not Python-via-launcher, so they never
#: import `hook_input` and cannot degrade with it. EXEMPT BY NAME, never invisible.
#:
#: Keyed on the EXACT normalised command, not a substring. A substring key is an
#: exemption with a wildcard on the end: a newly wired
#: `python3 scripts/cc-deploy-timeout-guard.py` would inherit the shell hook's
#: waiver and never be executed, which is precisely the "a new guard fails by
#: construction" guarantee this gate exists to make.
_NOT_PYTHON_ON_BASH = {
    "bash ${CLAUDE_PROJECT_DIR}/.claude/hooks/cc-deploy-timeout-guard":
        "shell script; never imports hook_input",
}

#: The inline `bash -c` guard is exempted by its own exact text, which is long and
#: contains quoting, so it is matched after whitespace normalisation rather than
#: pasted here in full. The PREFIX is still an exact command opening, not a
#: substring that could appear anywhere in someone else's command.
_INLINE_SHELL_PREFIX = "bash -c 'IN=$(cat);"

#: Hooks that legitimately do NOT block when degraded.
#:
#: THIS IS A HUMAN CLAIM, like every other exemption list in this repo
#: (`_NOT_WIRED` above, `_STRUCTURALLY_BOUNDED` and `hook-output-exempt:` in
#: test_hook_output_contract.py, `UNMAPPED_BY_DESIGN` in the sentinel). The quote
#: is EVIDENCE OF INTENT — it must appear verbatim in the hook's own docstring,
#: which is checked, and which caught three invented justifications — but it is
#: not a proof of behaviour and is not presented as one.
#:
#: An earlier version tried to VERIFY the claim by scanning for blocking syntax.
#: It could not converge: a docstring keyword matched three hooks that DO refuse;
#: a verbatim quote passes on historical prose; an AST scan for `exit(2)` missed
#: `return 2`, the house spelling; a scan for `return 2` misses `BLOCK = 2`, a
#: computed status, or a delegated helper. A contract cannot be inferred from
#: syntax, and every refactor invents a spelling the scan does not know. The
#: right shape is an explicit declaration the hook entrypoint ENFORCES, which is
#: a change to the hooks rather than to this test — see the issue linked from the
#: PR. Until then this list is what it has always been: a claim someone had to
#: write down, next to the words that justify it.
_ADVISORY_BY_DESIGN = {
    "hooks/capped_read_advisory.py": "ADVISORY ONLY.",
    "hooks/credential_surface_hook.py": "Exit 0 always — advisory, never blocks.",
    "hooks/pipe_status_guard.py": "ADVISORY, never blocking:",
    "hooks/pre_push_privacy_review.py": "NON-BLOCKING.",
    "hooks/tmux_kill_server_guard.py": "which is exactly why this is ADVISORY, never a block",
    "procedure_advisor.py": "surfaces relevant procedures as advisory context.",
}

#: The one supported launcher invocation. Compared literally: a path that merely
#: ENDS in `genesis-hook`, or is named that but points somewhere else, is a command
#: production cannot run, and resolving it would have this test exercise a guard the
#: real configuration never reaches.
_LAUNCHER = "${CLAUDE_PROJECT_DIR}/.claude/hooks/genesis-hook"

#: A tripwire, not a count. If the enumeration collapses — a renamed key, a
#: reshaped settings file — pytest reports an EMPTY parametrize list as SKIPPED,
#: not failed, and this gate would silently cover nothing.
_MIN_EXPECTED_BASH_HOOKS = 12


def _fires_on_bash(matcher: str | None) -> bool:
    """Does this entry's matcher fire on a Bash tool call?

    THREE spellings, and missing any one of them silently shrinks the population:

    * REGEX, not a literal — the repo wires `"Read|Grep|Glob"`, `"Write|Edit"`
      and `".*"`.
    * COMMA-SEPARATED alternatives. `docs/reference/cc-compatibility.md` records
      "comma-separated matchers never firing" as a FIX landed at 2.1.191, and the
      pinned version is well past that, so `"Bash,Edit"` fires on Bash in
      production while `re.search("Bash,Edit", "Bash")` finds nothing. None is
      wired today, which is exactly what makes it the case an allowlist is for.
    * ABSENT/EMPTY — every tool.

    An INVALID regex is treated as FIRING: a malformed entry gets examined rather
    than skipped, which is the conservative direction here.
    """
    if not matcher:
        return True
    for part in (p.strip() for p in matcher.split(",")):
        if not part:
            return True  # a trailing/empty alternative means "unconstrained"
        try:
            if re.search(part, "Bash"):
                return True
        except re.error:
            return True
    return False


def _resolve_bash_hook(command: str) -> tuple[str, str | None, list[str]]:
    """(display, script-relative-to-scripts/, extra argv) for one wired command.

    Returns ``(display, None, [])`` when the command cannot be resolved, so the
    caller FAILS on it instead of dropping it. Extra argv is carried because a
    hook run without its flags can produce a verdict production never produces
    (``worktree_cwd_guard`` is already wired three ways, two of them flagged).
    """
    tokens = command.split()
    # The launcher must be the command being EXECUTED — token 0 — not a path that
    # merely appears somewhere in the line. Accepting a suffix at any position
    # resolves `echo .../genesis-hook hooks/full_suite_guard.py` as if it ran the
    # guard: the test would execute the guard directly, see exit 2, and stay green,
    # while Claude Code only echoes and the guard never runs at all. A typo like
    # `/missing-genesis-hook …` has the same shape and the same consequence.
    # The SUPPORTED invocation, exactly. Three progressively tighter versions of
    # this check were each defeated in review, so the reasoning is recorded rather
    # than the conclusion alone:
    #   `endswith("genesis-hook")` anywhere  -> `echo .../genesis-hook guard.py`
    #                                           resolved, and the test ran the guard
    #                                           directly while production only echoed.
    #   token 0 + `endswith`                 -> `./fake-genesis-hook guard.py` resolved.
    #   token 0 + basename equality          -> `/missing/genesis-hook guard.py`
    #                                           resolved: a config TYPO that production
    #                                           cannot execute at all, while the test
    #                                           ran the guard and passed.
    # Every one of those failure modes is the same shape: the test exercised a guard
    # the configured command would never reach. So the launcher must be the path the
    # settings actually support; anything else is unresolvable and FAILS.
    if len(tokens) > 1 and tokens[0] == _LAUNCHER:
        rel = tokens[1]
        return rel, rel, tokens[2:]
    return command, None, []


def _bash_hook_entries() -> list[tuple[str, str | None, list[str]]]:
    """Every PreToolUse hook whose matcher can fire on Bash, from the settings.

    Scoped to the REPO settings. A user-level `~/.claude/settings.json` can wire
    more hooks on the same matcher and is invisible here by construction — stated
    rather than silently assumed away. MEASURED on one install: it wires a shell
    hook on `Grep|Glob|Bash`, which this enumeration cannot see.
    """
    cfg = json.loads((_REPO_ROOT / ".claude" / "settings.json").read_text())
    out: list[tuple[str, str | None, list[str]]] = []
    for entry in cfg.get("hooks", {}).get("PreToolUse", []):
        if not _fires_on_bash(entry.get("matcher")):
            continue
        for hook in entry.get("hooks", []):
            out.append(_resolve_bash_hook(hook.get("command", "")))
    return sorted(out, key=lambda row: row[0])


def _ids(rows):
    return [row[0] for row in rows]


@pytest.mark.parametrize("display,rel,argv", _bash_hook_entries(), ids=_ids(_bash_hook_entries()))
def test_every_bash_hook_declares_its_degrade_direction(tmp_path, display, rel, argv):
    """A hook that can fire on Bash either refuses when `hook_input` dies, or is NAMED.

    The failure this prevents is invisible in production: a guard whose shared
    import breaks exits 1 on the traceback, Claude Code reads a bare non-2 exit as
    a non-blocking error, and the command runs. Nothing reports it, and the guard
    looks identical to one that examined the command and approved it.
    """
    if rel is None:
        norm = " ".join(display.split())
        exempt = norm in _NOT_PYTHON_ON_BASH or norm.startswith(_INLINE_SHELL_PREFIX)
        assert exempt, (
            f"cannot resolve the wired command {display!r} to a script, and it is not "
            f"named in _NOT_PYTHON_ON_BASH. An unresolvable hook is exactly what an "
            f"allowlist must not wave through — a guard could be respelled out of this "
            f"gate's sight and the suite would stay green. Either wire it through "
            f"genesis-hook, or name it with the reason it cannot degrade."
        )
        return

    root, home = _broken_hook_input(tmp_path, f"home_contract_{abs(hash(display)) % 10**8}")
    res = subprocess.run(
        [sys.executable, str(root / "scripts" / rel), *argv],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hello"}}),
        capture_output=True,
        text=True,
        cwd=str(root),
        env={**os.environ, "HOME": str(home)},
        timeout=90,
    )

    if rel in _ADVISORY_BY_DESIGN:
        assert res.returncode != 2, (
            f"{rel} is listed as advisory-by-design but REFUSED (exit 2) while "
            f"degraded. If it became a blocking guard, remove it from "
            f"_ADVISORY_BY_DESIGN; the list is not a waiver."
        )
        return

    assert res.returncode == 2, (
        f"{rel} can fire on Bash and did NOT refuse when hook_input was unimportable "
        f"(exit {res.returncode}). Claude Code treats a bare non-2 exit as NON-BLOCKING, "
        f"so this guard silently stops guarding whenever a shared module breaks — "
        f"indistinguishable from having approved the command.\n\n"
        f"Either give it a degraded handler that exits 2 (see full_suite_guard.py), or, "
        f"if it is advisory by contract, add it to _ADVISORY_BY_DESIGN with the words "
        f"from its own docstring that say so.\n\nstderr: {res.stderr[:300]}"
    )
    assert "GUARD DEGRADED" in res.stderr, (
        f"{rel} refused while degraded but did not SAY it was degraded. A silent "
        f"refusal is indistinguishable from a real finding, which is the whole reason "
        f"the degraded path speaks."
    )


def test_the_bash_hook_enumeration_is_not_vacuous():
    """pytest reports an EMPTY parametrize list as SKIPPED, not failed.

    So without this, a collapsed enumeration — a renamed settings key, a reshaped
    file — would report success while covering nothing. The floor is a tripwire,
    not a count: it must never be edited to match a shrinking population.
    """
    rows = _bash_hook_entries()
    assert len(rows) >= _MIN_EXPECTED_BASH_HOOKS, (
        f"only {len(rows)} hooks resolved as able to fire on Bash — the enumeration "
        f"has collapsed, and an empty parametrize list is reported as SKIPPED rather "
        f"than failed. Fix the enumeration; do not lower the floor."
    )
    resolved = {rel for _d, rel, _a in rows if rel}
    for anchor in ("hooks/full_suite_guard.py", "review_enforcement_commit.py"):
        assert anchor in resolved, (
            f"{anchor} is a known blocking guard wired on Bash and did not appear in "
            f"the enumeration — a silent DROP, which is the failure mode this gate "
            f"exists to make impossible."
        )


def test_the_advisory_allowlist_quotes_are_real():
    """An exemption justified by an invented quote is prose, not evidence.

    Three of the first draft's five quotes were paraphrases that appeared nowhere
    in the hook they excused. Nothing checked them, so the allowlist's only
    substance was a sentence someone could have made up.
    """
    for rel, quote in _ADVISORY_BY_DESIGN.items():
        path = _REPO_ROOT / "scripts" / rel
        doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or ""
        flat = " ".join(doc.split())
        assert " ".join(quote.split()) in flat, (
            f"_ADVISORY_BY_DESIGN quotes {quote!r} for {rel}, which does not appear "
            f"in its docstring. The justification must be the hook's own words, or "
            f"the exemption is unverifiable."
        )


def test_the_advisory_allowlist_has_no_stale_entries():
    """Both directions: a listed hook that is no longer wired must leave the list.

    Without this, a hook could be removed from the Bash surface and its entry would
    sit here forever, quietly widening the exemption for a file nobody runs.
    """
    wired = {rel for _d, rel, _a in _bash_hook_entries() if rel}
    stale = sorted(set(_ADVISORY_BY_DESIGN) - wired)
    assert not stale, (
        f"{stale} are in _ADVISORY_BY_DESIGN but no longer fire on Bash. Remove them — "
        f"an exemption for a hook nobody runs is debt, not safety."
    )
