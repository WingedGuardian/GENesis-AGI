"""A here-doc body that hides the commands after its terminator, driven through the
real guards (#3433).

THE DEFECT. ``parse_segments`` has no here-doc state, so a body that leaves the
segmenter open (here an unmatched backtick in what reads as a redirect target) merges
the terminator and every command after it into one segment. A guard searching the
segments for its operation found none, and the shell ran it. The fix reports a
bounds-type blind spot (``shell_parse._BLIND_HEREDOC_BODY``), and each guard's
existing bounds branch refuses when the command names its operation.

THE HARNESS NEVER REACHES A REAL GIT. Each case runs in a fresh directory that is no
repository, with a FAKE ``git`` executable first on PATH and
``GIT_CEILING_DIRECTORIES`` set: an earlier prototype ran the real git with the
force-push arguments these cases carry. Trigger literals are assembled from fragments
so this file's own text does not carry them.
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
_COMMIT_GATE = _WORKTREE / "scripts" / "review_enforcement_commit.py"
_PY = sys.executable

GIT = "g" + "it"
FORCE_PUSH = " ".join([GIT, "pu" + "sh", "--" + "for" + "ce", "origin", "main"])
CLEAN_ALL = " ".join([GIT, "cl" + "ean", "-" + "fdx"])
COMMIT = " ".join([GIT, "com" + "mit", "-m", "x"])

# The body's unmatched backtick reads as a redirect target running to the end.
HIDING = "tee out.txt <<'EOF'\nx<`\nEOF\n"
CLEAN_BODY = "tee out.txt <<'EOF'\nhello\nEOF\n"

_NEUTRAL_ENV = ("TMPDIR", "LANG", "LC_ALL", "LC_CTYPE")


@pytest.fixture
def sandbox():
    """(home, cwd, env): a scratch root outside every repository, a fake git."""
    with tempfile.TemporaryDirectory(prefix="heredoc-suffix-") as d:
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
    """Exit 2, or a JSON deny. An ``ask`` is not a refusal."""
    try:
        out = json.loads(res.stdout or "{}")
    except json.JSONDecodeError:
        out = {}
    decision = out.get("hookSpecificOutput", {}).get("permissionDecision", "")
    return res.returncode == 2 or decision == "deny"


def _engaged(res: subprocess.CompletedProcess) -> bool:
    """Refused, or asked: the guard saw the operation and did not simply allow it."""
    return _refused(res) or '"ask"' in (res.stdout or "")


def _says_heredoc(res: subprocess.CompletedProcess) -> bool:
    return "here-doc body" in (res.stderr + res.stdout)


# (id, guard, gated command). The hidden form must be refused, naming the here-doc.
# The same command after a clean body is the control: the guard engages on this
# operation in this sandbox at all (the commit gate asks there, because no review
# history is readable without a repository), so the case measures the here-doc and
# not the sandbox.
CASES = [
    pytest.param(_PUSH_GUARD, FORCE_PUSH, id="push-guard-force-push"),
    pytest.param(_DISCARD, CLEAN_ALL, id="discard-guard-clean"),
    pytest.param(_COMMIT_GATE, COMMIT, id="commit-gate-commit"),
]


@pytest.mark.parametrize(("guard", "gated"), CASES)
def test_a_command_hidden_after_a_heredoc_is_refused(sandbox, guard, gated):
    cwd, env = sandbox
    res = _run(guard, HIDING + gated, cwd, env)
    assert _refused(res), (res.returncode, res.stdout, res.stderr)
    assert _says_heredoc(res), res.stderr + res.stdout


@pytest.mark.parametrize(("guard", "gated"), CASES)
def test_the_same_command_after_a_clean_body_engages_the_guard(sandbox, guard, gated):
    cwd, env = sandbox
    res = _run(guard, CLEAN_BODY + gated, cwd, env)
    assert _engaged(res), (res.returncode, res.stdout, res.stderr)
    assert not _says_heredoc(res)


@pytest.mark.parametrize("guard", [_PUSH_GUARD, _DISCARD, _COMMIT_GATE])
def test_a_hiding_body_before_a_harmless_command_is_not_refused(sandbox, guard):
    cwd, env = sandbox
    res = _run(guard, HIDING + "ls -la", cwd, env)
    assert not _refused(res), (res.returncode, res.stdout, res.stderr)
