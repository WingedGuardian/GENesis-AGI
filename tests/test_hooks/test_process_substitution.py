"""Process substitution bodies are commands the guards must see.

``parse_segments`` consumes the ``<`` or ``>`` of ``<(…)`` / ``>(…)`` and leaves
``(body)`` in the segment as a word, and no scan extracted the body, so a push or an
``rm`` bash runs there reached no guard and no blind spot was reported (#2481).

The oracle runs each command under bash with recording stand-ins for git, gh and rm,
in a throwaway directory, so nothing real is pushed or deleted.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[2] / "scripts" / "hooks"
sys.path.insert(0, str(HOOKS))
import shell_parse as sp  # noqa: E402

PUSH = "git " + "push --force origin main"
PUSH_ARGV = ("git", "push", "--force", "origin", "main")
RM = "rm -rf " + "~/.genesis"

_SHIM = (
    '#!/bin/sh\n{ printf "%s\\037" "${0##*/}" "$@"; printf "\\036"; } >> "$ORACLE_LOG"\nexit 0\n'
)


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if shutil.which("bash") is None:
        pytest.skip("the oracle needs bash")
    root = tmp_path_factory.mktemp("process-substitution")
    (root / "bin").mkdir()
    for name in ("git", "gh", "rm"):
        shim = root / "bin" / name
        shim.write_text(_SHIM, encoding="utf-8")
        shim.chmod(0o755)
    (root / "work").mkdir()
    subprocess.run(["git", "init", "-q", str(root / "repo")], check=True)
    return root


def _ran(command: str, sandbox: Path) -> list[tuple[str, ...]]:
    """The argv of every stand-in bash actually executed for ``command``."""
    log = sandbox / "oracle.log"
    log.write_text("", encoding="utf-8")
    # Two independent layers keep the oracle harmless: HOME is the sandbox, so `~`
    # never names a real directory, and the stand-ins must be what bash resolves.
    path = f"{sandbox / 'bin'}:/usr/bin:/bin"
    for name in ("git", "gh", "rm"):
        assert shutil.which(name, path=path) == str(sandbox / "bin" / name)
    subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", command],
        cwd=sandbox / "work",
        env={
            "PATH": path,
            "HOME": str(sandbox / "work"),
            "ORACLE_LOG": str(log),
            "LC_ALL": "C",
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        check=False,
    )
    data = log.read_text(encoding="utf-8")
    return [tuple(rec.split("\x1f")[:-1]) for rec in data.split("\x1e") if rec]


def _guard_exit(guard: str, command: str, sandbox: Path) -> int:
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "t",
        "cwd": str(sandbox / "repo"),
    }
    done = subprocess.run(
        [sys.executable, str(HOOKS / f"{guard}.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=120,
        cwd=sandbox / "repo",
    )
    return done.returncode


def _shows_or_refuses(command: str, wanted: tuple[str, ...]) -> None:
    """The operation is visible in both checked views, or nothing is and a bound says why."""
    segments, blind = sp.analyze_checked(command)
    policy, policy_blind = sp.analyze_policy(command)
    if blind is not None and blind.bounds_induced:
        assert segments == [] and policy == [] and policy_blind is not None
        return
    assert any(tuple(s.argv[: len(wanted)]) == wanted for s in segments), (
        f"bash runs {wanted} and the parse neither shows it nor refuses: {blind}"
    )
    assert any(tuple(p.argv[: len(wanted)]) == wanted for p in policy)


HIDDEN_PUSH = {
    "input process substitution": "cat <(" + PUSH + ")",
    "output process substitution": "echo x > >(" + PUSH + ")",
    "second of two": "diff <(true) <(" + PUSH + ")",
    "after a redirect": "cat < <(" + PUSH + ")",
    "inside a command substitution": "x=$(cat <(" + PUSH + "))",
    "after a dq apostrophe": 'cat <(echo "it\'s") <(' + PUSH + ")",
    "tee target": "tee >(" + PUSH + ") </dev/null",
    # Inside a command substitution the `<` is consumed while the parent's segment is
    # built, so the body must be read again in the parent's own text.
    "nested in a command substitution": "echo $(cat <(" + PUSH + "))",
    "nested in backticks": "echo `cat <(" + PUSH + ")`",
    "output form nested": "echo $(echo x > >(" + PUSH + "))",
    "two levels deep": "echo $(echo $(cat <(" + PUSH + ")))",
    "nested in another process substitution": "cat <(cat <(" + PUSH + "))",
    "inside a bash -c script": "bash -c 'cat <(" + PUSH + ")'",
    "after a separator in the body": "cat <(true; " + PUSH + ")",
    "after a separator, nested": "echo $(cat <(true; " + PUSH + "))",
    "after arithmetic": "echo $((1 + 1)) <(" + PUSH + ")",
}


@pytest.mark.parametrize("name", sorted(HIDDEN_PUSH))
def test_a_push_in_a_process_substitution_is_seen(name: str, sandbox: Path) -> None:
    command = HIDDEN_PUSH[name]
    assert any(a[: len(PUSH_ARGV)] == PUSH_ARGV for a in _ran(command, sandbox)), (
        "oracle: bash must run the push"
    )
    _shows_or_refuses(command, ("git", "push"))
    assert _guard_exit("git_push_guard", command, sandbox) == 2


@pytest.mark.parametrize("guard", ["destructive_command_guard", "protected_paths_guard"])
@pytest.mark.parametrize("shape", ["cat <({})", "echo $(cat <({}))"])
def test_an_rm_in_a_process_substitution_is_refused(guard: str, shape: str, sandbox: Path) -> None:
    command = shape.format(RM)
    assert any(a[:2] == ("rm", "-rf") for a in _ran(command, sandbox)), (
        "oracle: bash must run the rm"
    )
    assert _guard_exit(guard, command, sandbox) == 2


NOT_RUN = {
    "double-quoted": 'echo "<(' + PUSH + ')"',
    "single-quoted": "echo '<(" + PUSH + ")'",
    "escaped": "echo \\<\\(" + PUSH + "\\)",
}


@pytest.mark.parametrize("name", sorted(NOT_RUN))
def test_a_quoted_process_substitution_is_not_a_body(name: str, sandbox: Path) -> None:
    command = NOT_RUN[name]
    assert not any(a[:2] == ("git", "push") for a in _ran(command, sandbox)), (
        "oracle: bash must not run the push"
    )
    assert sp._process_substitution_spans(command) == []


def test_bodies_are_cut_at_their_close() -> None:
    text = "diff <(a b) >(c) $(<(d))"
    assert [text[x:y] for x, y in sp._process_substitution_spans(text)] == ["a b", "c"]


def test_process_bodies_are_charged_as_added_work() -> None:
    """The legacy scan never read these bodies, so they are bounded like any other
    added work."""
    work = [0, 10**9]
    sp._analyze_bounded("cat <(abc)", _added_work=work)
    assert work[0] == 3
