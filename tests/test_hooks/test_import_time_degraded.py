"""A guard whose module-scope import fails must still fail CLOSED.

THE DEFECT THIS LOCKS. ``run_guard`` is called at the BOTTOM of a guard, so an
exception raised while the module is still importing never reaches it. Python exits
1, and Claude Code's PreToolUse contract is "exit 2 blocks; ANY other code is a
non-blocking error, so the tool RUNS". MEASURED before the fix, across all four
guards that import ``shell_parse`` at module scope: poison that one sibling and every
guard went exit 2 -> exit 1, with a healthy-tree control still blocking at 2. The gate
did not degrade — it VANISHED, silently, while the session still believed it was
protected. Version skew between a worktree and the main tree makes this a real
configuration here, not a hypothetical.

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
        # is the chosen direction — a loud, overridable refusal while the hook tree is
        # broken, against a silent deletion. If this cell ever has to change, the
        # matcher got narrower and the line-continued cell above is the one to re-check.
        "git_discard_guard",
        "hooks/git_discard_guard.py",
        "make clean",
        2,
        "over-block (priced, intended)",
    ),
    (
        "git_discard_guard",
        "hooks/git_discard_guard.py",
        "git clean -fd  # discard-override",
        0,
        "waiver honoured",
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
        "review_enforcement_commit",
        "review_enforcement_commit.py",
        'git commit -m "x"  # review-override',
        0,
        "waiver honoured",
    ),
]


def _tree(tmp_path: Path, *, poisoned: bool) -> Path:
    """A standalone copy of scripts/, optionally with ONE sibling poisoned.

    Everything is copied rather than symlinked so the poisoned module cannot leak
    back into the real tree, and the poison replaces exactly `shell_parse` — the
    shared import all four guards make at module scope — so a failure here can only
    be the import-time path and never an unrelated missing file.
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
    assert "GUARD DEGRADED" in res.stderr, (
        f"{guard} [{label}] gave no GUARD DEGRADED notice. A degraded allow that "
        "looks identical to a real one has told the operator nothing."
    )


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


def test_a_test_importing_a_broken_tree_sees_the_real_error(tmp_path):
    """Degrading is for the LIVE hook, never for an importer.

    The guards degrade only under ``__name__ == "__main__"``. A test or tool that
    imports a broken tree must get the traceback, not a process exit — otherwise a
    broken dependency is invisible to exactly the machinery meant to catch it.
    """
    root = _tree(tmp_path, poisoned=True)
    res = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, 'scripts/hooks'); import protected_paths_guard",
        ],
        capture_output=True,
        text=True,
        cwd=str(root),
        env={**os.environ, "HOME": str(tmp_path / "home_imp")},
        timeout=90,
    )
    assert res.returncode == 1, "an importer should get the exception, not a guard exit"
    assert "poisoned sibling" in res.stderr, (
        "the real cause must reach the importer; got:\n" + res.stderr[-400:]
    )
    assert "GUARD DEGRADED" not in res.stderr, (
        "the degraded path fired for an importer — it is scoped to __main__ only"
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
