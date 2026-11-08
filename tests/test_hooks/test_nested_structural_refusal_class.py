"""The accepted structural-refusal class, pinned through the real guards (#3455).

THE CLASS. The substitution scan reads a quoted here-doc's body lines as command
text, so a body line holding ``$(...)`` with an apostrophe inside is an unreadable
nested extraction, and the whole command is structurally uncertain. A command that
also mentions git is then refused even when its git part is readable. That is
accepted and named in docs/reference/shell-parse-completeness.md. These tests pin
it both ways, so a later change that widens or narrows it has to say so, and check
that the refusal names the real cause.

THE HARNESS NEVER REACHES A REAL GIT: a scratch directory outside every repository,
a FAKE ``git`` first on PATH, ``GIT_CEILING_DIRECTORIES`` set. Trigger words are
assembled from fragments so this file's own text does not carry them.
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
_PUSH_GUARD = _HOOKS_DIR / "git_push_guard.py"
_DISCARD = _HOOKS_DIR / "git_discard_guard.py"
_PY = sys.executable

GIT = "g" + "it"
ADD = " ".join([GIT, "a" + "dd", "f"])
PUSH = " ".join([GIT, "pu" + "sh", "origin", "feature-x"])
# A quoted here-doc of shell prose whose body line holds $(...) with an apostrophe.
PROSE_HEREDOC = "cat <<'EOF' > notes\n$(echo don't)\nEOF\n"
# Nested and unreadable, but naming nothing any guard gates.
NESTED_BLIND_HARMLESS = 'bash -c "echo \\"\\$(echo \'a\\"b\')\\""'

_NEUTRAL_ENV = ("TMPDIR", "LANG", "LC_ALL", "LC_CTYPE")


@pytest.fixture
def sandbox():
    with tempfile.TemporaryDirectory(prefix="structural-class-") as d:
        root = Path(d)
        for parent in (root, *root.parents):
            assert not (parent / ".git").exists(), f"{parent} is a repository"
        home, cwd, fake_bin = root / "home", root / "work", root / "bin"
        for p in (home / "genesis" / "data", cwd, fake_bin):
            p.mkdir(parents=True)
        fake = fake_bin / "git"
        fake.write_text('#!/bin/sh\necho FAKE-git "$@" >&2\nexit 97\n')
        fake.chmod(0o755)
        env = {k: os.environ[k] for k in _NEUTRAL_ENV if k in os.environ}
        env["PATH"] = f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"
        env["HOME"] = str(home)
        env["GENESIS_HOME"] = str(home / ".genesis")
        env["GIT_CEILING_DIRECTORIES"] = str(root)
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_SYSTEM"] = os.devnull
        yield cwd, env


def _run(script: Path, command: str, cwd: Path, env: dict) -> subprocess.CompletedProcess:
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}
    return subprocess.run(
        [_PY, str(script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=env,
        timeout=120,
    )


def _refused(res: subprocess.CompletedProcess) -> bool:
    try:
        out = json.loads(res.stdout or "{}")
    except json.JSONDecodeError:
        out = {}
    decision = out.get("hookSpecificOutput", {}).get("permissionDecision", "")
    return res.returncode == 2 or decision == "deny"


def test_the_discard_guard_refuses_the_class_and_names_the_real_cause(sandbox):
    cwd, env = sandbox
    res = _run(_DISCARD, ADD + " && " + PROSE_HEREDOC, cwd, env)
    assert _refused(res), (res.returncode, res.stdout, res.stderr)
    said = res.stderr + res.stdout
    assert "here-doc text" in said, said
    assert "subcommand the guard cannot read" not in said, said


def test_the_push_guard_refuses_the_class(sandbox):
    cwd, env = sandbox
    res = _run(_PUSH_GUARD, PUSH + " && " + PROSE_HEREDOC, cwd, env)
    assert _refused(res), (res.returncode, res.stdout, res.stderr)


def test_the_same_heredoc_without_git_is_not_refused(sandbox):
    """The refusal is about git appearing beside unreadable text, not the text."""
    cwd, env = sandbox
    for guard in (_PUSH_GUARD, _DISCARD):
        res = _run(guard, "ls && " + PROSE_HEREDOC, cwd, env)
        assert not _refused(res), (guard.name, res.returncode, res.stdout, res.stderr)


@pytest.mark.parametrize("guard", [_PUSH_GUARD, _DISCARD], ids=["push-guard", "discard-guard"])
def test_a_nested_blind_command_naming_nothing_gated_is_allowed(sandbox, guard):
    cwd, env = sandbox
    res = _run(guard, NESTED_BLIND_HARMLESS, cwd, env)
    assert not _refused(res), (res.returncode, res.stdout, res.stderr)


def test_a_hidden_verb_beside_the_class_gets_advice_that_covers_both(sandbox):
    """Segments are withheld, so the guard cannot tell a hidden ``git $V`` from a
    readable git command beside unreadable prose: the advice must cover both."""
    cwd, env = sandbox
    res = _run(_DISCARD, GIT + " $V && " + PROSE_HEREDOC, cwd, env)
    assert _refused(res), (res.returncode, res.stdout, res.stderr)
    said = res.stderr + res.stdout
    assert "here-doc text" in said and "written out literally" in said, said


def test_a_level_whose_own_carried_body_fails_keeps_the_heredoc_cause():
    """Only an independent child is structural: a nested level that fails to
    tokenize because of its own carried here-doc body keeps the heredoc cause."""
    sys.path.insert(0, str(_HOOKS_DIR))
    import shell_parse

    cmd = ADD + " && bash -c \"tee f <<'EOF'\nit's data\nEOF\nprobe tail\""
    _, blind = shell_parse.analyze_checked(cmd)
    assert blind is shell_parse._BLIND_HEREDOC_BODY, blind
