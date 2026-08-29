"""The compound-atomicity guard: refuse a write chained ahead of a pre-blocking step.

A PreToolUse block discards the WHOLE Bash call. A file write chained ahead of a
step that a guard refuses is therefore lost SILENTLY — the error names only the
second step, so the write reads as having happened. This guard refuses that shape
up front, turning an invisible loss into a one-step correction.

The tests below pin BOTH directions, because a guard that refuses everything
passes every "must block" case while being worthless:

  * the shape it exists for is refused, including the exact command that
    motivated it;
  * the far larger set of ordinary compounds is NOT refused, and the scoping
    decisions that keep it that way are pinned individually so that widening one
    of them fails here rather than in a session's ergonomics.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parent.parent.parent
_GUARD = _WORKTREE / "scripts" / "hooks" / "compound_atomicity_guard.py"

_spec = importlib.util.spec_from_file_location("compound_atomicity_guard", _GUARD)
_mod = importlib.util.module_from_spec(_spec)
sys.path.insert(0, str(_WORKTREE / "scripts" / "hooks"))
_spec.loader.exec_module(_mod)


def _run(command: str) -> int:
    """Exit status of the guard for ``command`` — 2 blocks, 0 allows.

    Runs the hook as a SUBPROCESS, the way Claude Code invokes it, rather than
    calling its predicates: the payload path is part of what can break, and an
    in-process test would not have caught it. ``CLAUDE_TOOL_INPUT`` is stripped
    because ``hook_input`` prefers that legacy env var over stdin — with it set
    (as it is inside a live session) every case would silently exercise the
    ambient command instead of the one under test.
    """
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_TOOL_INPUT"}
    payload = json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
        }
    )
    return subprocess.run(
        [sys.executable, str(_GUARD)], input=payload, capture_output=True, text=True, env=env
    ).returncode


# ── the shape it exists for ───────────────────────────────────────────────


def test_the_command_that_motivated_this_guard_is_refused():
    """The acceptance bar: replay the REAL defect, not a stylised version of it.

    An inline script that edits a file, chained with a step that blocks before
    running. This exact shape lost an edit; the loss was found only by re-reading
    the file afterwards.
    """
    assert _run("python3 - <<'PY'\nopen('f','w').write('x')\nPY\n && git commit -m x") == 2


@pytest.mark.parametrize(
    "command",
    [
        "python3 -c \"open('f','w').write('x')\" && git push origin main",
        "bash -s <<'SH'\necho hi\nSH\n && gh pr merge 12 --squash --admin",
        "sed -i s/a/b/ f.py && git commit -m x",
        "cp a b && git checkout main",
        "echo x | tee f.txt && git push origin main",
        "python3 - <<'PY'\nx=1\nPY\n && pytest tests/",
    ],
    ids=["python -c", "bash -s heredoc", "sed -i", "cp", "tee", "full-suite pytest"],
)
def test_a_write_ahead_of_a_pre_blocking_step_is_refused(command):
    assert _run(command) == 2


# ── the far larger set that must pass ─────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "cd /tmp && pytest tests/x.py",
        "mkdir -p out && pytest tests/x.py",
        "git add -A && git commit -m x",
        "cat f | grep x",
        "cmd > /dev/null && git commit -m x",
        "cp a b && ls",
        "cp a b",
        "sed s/a/b/ f.py && git commit -m x",
        "touch f && git commit -m x",
        "python3 scripts/run.py && git commit -m x",
        "python3 - <<'PY'\nprint(1)\nPY",
        "grep -rn foo src/ && git commit -m x",
        "git status --short && git commit -m x",
    ],
    ids=[
        "cd",
        "mkdir",
        "git add",
        "read-only pipe",
        "null sink",
        "nothing blockable",
        "single segment",
        "sed without -i",
        "touch",
        "script FILE not inline",
        "inline script, no chain",
        "grep",
        "git status",
    ],
)
def test_ordinary_compounds_are_not_refused(command):
    assert _run(command) == 0


# ── the scoping decisions, pinned one by one ──────────────────────────────


def test_a_TARGETED_pytest_run_is_not_treated_as_pre_blocking():
    """The single decision that took the false-positive rate from 3.2% to 0.26%.

    A targeted pytest run is serialised by an in-process lock (#1530), so it fails
    AFTER the chained write has already landed — there is nothing to lose and
    nothing to refuse. Counting it flagged 51 commands in one real session, almost
    all of them the author's ordinary idiom.
    """
    assert _run("python3 - <<'PY'\nx=1\nPY\n && pytest tests/a/test_b.py") == 0


def test_a_write_AFTER_the_blockable_step_is_not_refused():
    """Order matters. A write after the refused step is discarded too, but nobody
    believes it ran, so there is no silent loss to prevent."""
    assert _run("git commit -m x && cp a b") == 0


def test_rm_is_not_treated_as_pre_blocking():
    """`rm` is refused only for genuinely destructive shapes, not in general.
    Counting every `rm` re-flagged the ordinary backup-then-clean-up idiom."""
    assert _run("cp a b && rm -f /home/ubuntu/tmp/scratch") == 0


def test_an_unparseable_command_fails_OPEN():
    """A convenience guard that prevents a self-inflicted loss, not a security
    boundary. The worst case of a miss is the status quo; the worst case of a
    crash is that no Bash command runs at all."""
    assert _run("cmd 'unterminated && git commit -m x") == 0


def test_the_refusal_names_both_halves_so_the_fix_is_obvious():
    """A block that does not say WHAT to split is a block that gets worked around."""
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_TOOL_INPUT"}
    payload = json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "cp a b && git push origin main"},
        }
    )
    result = subprocess.run(
        [sys.executable, str(_GUARD)], input=payload, capture_output=True, text=True, env=env
    )
    assert result.returncode == 2
    assert "cp a b" in result.stderr
    assert "git push" in result.stderr
    assert "Split them" in result.stderr
