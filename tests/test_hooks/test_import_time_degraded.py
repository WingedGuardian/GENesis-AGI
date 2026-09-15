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

import json
import os
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
]


def _tree(tmp_path: Path, *, poisoned: bool) -> Path:
    """A standalone copy of scripts/, optionally with ONE sibling poisoned.

    Everything is copied rather than symlinked so the poisoned module cannot leak
    back into the real tree, and the poison replaces exactly `shell_parse` — the one
    sibling every wired guard imports at module scope — so a failure here can only be
    the import-time path and never an unrelated missing file.
    """
    root = tmp_path / ("poisoned" if poisoned else "healthy")
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
_NOT_WIRED = {
    # Its only usable degraded token is the pipe character, which appears in 52,220 of
    # 74,282 commands (70.30%). Wiring it would not make the broken state safe, it
    # would make it UNREPAIRABLE — and a degraded matcher that blocks the majority of
    # ordinary work is not a fail-closed option at all. What its refusal protects is
    # also the mildest in this set: a backgrounded pipeline whose stdout is swallowed,
    # i.e. a re-run. Revisit if it ever gains a distinctive token.
    "background_pipe_guard.py": "only token is `|` at 70.30% — would wedge the repair",
    # Advisory only: their sole verdict is a note, so an import-time exit 1 loses
    # advice and never a refusal. Verified by the absence of any exit-2 path, not by
    # reading their docstrings.
    "pipe_status_guard.py": "advisory only — no exit-2 path to lose",
    "tmux_kill_server_guard.py": "advisory only — no exit-2 path to lose",
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

    found: set[str] = set()
    for path in [*_HOOKS.glob("*.py"), *_SCRIPTS.glob("*.py")]:
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover — a syntax error is another test's job
            continue
        for node in tree.body:
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "shell_parse"
                or isinstance(node, ast.Import)
                and any(alias.name == "shell_parse" for alias in node.names)
            ):
                found.add(path.name)

    assert found, (
        "no bare module-scope importer found at all — the walk is looking in the wrong "
        "place, and an empty population would make this test pass forever"
    )
    unaccounted = found - set(_NOT_WIRED)
    assert not unaccounted, (
        f"{sorted(unaccounted)} import shell_parse at bare module scope. An exception "
        "during that import never reaches run_guard, so the process exits 1 — which "
        "Claude Code reads as NON-BLOCKING, and the guard vanishes instead of "
        "degrading. Either call hook_input.degraded_exit from a guarded import, or add "
        "the file to _NOT_WIRED with the MEASURED reason it does not need one."
    )
    # Both directions: a guard that gains the wiring must leave _NOT_WIRED, or the
    # exemption silently outlives its reason. This is what a bare `len(found) == N`
    # check could not catch.
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
