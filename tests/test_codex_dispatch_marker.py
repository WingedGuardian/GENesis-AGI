"""Every Codex run Genesis launches itself carries the dispatched marker.

Codex hands an MCP child only a minimal environment plus the names its config
forwards, so the Genesis MCP launcher can only see who started Codex if the
marker is set on the Codex process. An unmarked Codex run is the owner's
interactive client, the same way an unmarked Claude Code session is; a run
Genesis starts unattended must say so.

The guarantee is structural: ``genesis.util.codex_cli`` is the only place under
``src/genesis`` that names the Codex binary, and every spawn of its argv must
pass its marked environment. The ban test fails on any other mention of the
word ``codex`` in a string literal, except docstrings and logging messages,
which cannot start a process. A name assembled from pieces, or read from
configuration, is not a literal and is out of its reach.

Scope is ``src/genesis``, the code that runs unattended. Scripts under
``scripts/`` are operator-run; the one Codex spawn there, the validator
launcher, is an interactive supervised run with its own environment.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.caller_context import (
    CC_SESSION_ENV,
    SESSION_ID_ENV,
    SUPERVISED_ENV,
    TRANSCRIPT_ID_ENV,
    dispatched_child_env,
)
from genesis.util import codex_cli

SRC = Path(__file__).resolve().parents[1] / "src" / "genesis"
CHOKEPOINT = "util/codex_cli.py"
# Literals that name Codex without starting it, with their exact count per file.
ALLOWED = {
    "runtime/init/process_reaper.py": 2,  # pgrep pattern and label for orphan reaping
    "contribution/review.py": 1,  # reviewer="codex" label on a ReviewResult
}
_LOG_METHODS = frozenset({"debug", "info", "warning", "error", "exception", "critical", "log"})


def test_dispatched_child_env_sets_marker_and_keeps_the_rest():
    env = dispatched_child_env({"PATH": "/bin", "HOME": "/h"})
    assert env == {"PATH": "/bin", "HOME": "/h", CC_SESSION_ENV: "1"}


def test_dispatched_child_env_overrides_a_falsy_marker():
    assert dispatched_child_env({CC_SESSION_ENV: "0"})[CC_SESSION_ENV] == "1"


def test_dispatched_child_env_drops_supervision_and_session_identity():
    env = dispatched_child_env(
        {
            SUPERVISED_ENV: "1",
            SESSION_ID_ENV: "parent-session",
            TRANSCRIPT_ID_ENV: "parent-transcript",
            "CLAUDE_SESSION_ID": "legacy",
            "GENESIS_SESSION_ORIGIN": "external_untrusted",
        }
    )
    assert env == {CC_SESSION_ENV: "1", "GENESIS_SESSION_ORIGIN": "external_untrusted"}


def test_dispatched_child_env_copies_os_environ(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GENESIS_PROBE_KEEP", "x")
    monkeypatch.delenv(CC_SESSION_ENV, raising=False)
    env = dispatched_child_env()
    assert env["GENESIS_PROBE_KEEP"] == "x"
    assert env[CC_SESSION_ENV] == "1"
    assert CC_SESSION_ENV not in os.environ  # a copy, never the live mapping


def test_codex_cli_helpers():
    assert codex_cli.codex_exec_argv("-", "--json") == ["codex", "exec", "-", "--json"]
    assert codex_cli.codex_child_env()[CC_SESSION_ENV] == "1"
    with patch("genesis.util.codex_cli.shutil.which", return_value=None):
        assert codex_cli.codex_available() is False
    with patch("genesis.util.codex_cli.shutil.which", return_value="/x/codex"):
        assert codex_cli.codex_available() is True


def _assert_marked_spawn(kwargs: dict) -> None:
    env = kwargs["env"]
    assert env[CC_SESSION_ENV] == "1"
    assert SUPERVISED_ENV not in env
    assert kwargs["start_new_session"] is True


def test_contribution_codex_spawn_is_marked(monkeypatch: pytest.MonkeyPatch):
    from genesis.contribution import review

    monkeypatch.setenv(SUPERVISED_ENV, "1")
    proc = MagicMock()
    proc.pid = 54321
    proc.returncode = 0
    proc.communicate.return_value = (
        json.dumps(
            {"type": "item.completed", "item": {"type": "agent_message", "text": "VERDICT: PASS"}}
        ),
        "",
    )
    with (
        patch("genesis.util.codex_cli.shutil.which", return_value="/fake/codex"),
        patch("genesis.contribution.review.subprocess.Popen", return_value=proc) as popen,
    ):
        review._try_codex("diff")
    assert popen.call_args.args[0][:3] == ["codex", "exec", "-"]
    assert popen.call_args.kwargs["stdin"] is subprocess.PIPE
    _assert_marked_spawn(popen.call_args.kwargs)


async def test_executor_codex_spawn_is_marked(monkeypatch: pytest.MonkeyPatch):
    from genesis.autonomy.executor.review import TaskReviewer

    monkeypatch.setenv(SUPERVISED_ENV, "1")
    proc = AsyncMock()
    proc.pid = 54321
    proc.communicate = AsyncMock(return_value=(b"", b""))
    proc.returncode = 1
    with (
        patch("genesis.util.codex_cli.shutil.which", return_value="/usr/bin/codex"),
        patch(
            "genesis.autonomy.executor.review.asyncio.create_subprocess_exec",
            return_value=proc,
        ) as spawn,
    ):
        await TaskReviewer(router=AsyncMock())._verify_via_codex("d", "r")
    assert spawn.call_args.args[:3] == ("codex", "exec", "-")
    assert spawn.call_args.kwargs["stdin"] is subprocess.PIPE
    _assert_marked_spawn(spawn.call_args.kwargs)


_NAMES_CODEX = re.compile(r"(?i)(?<![\w-])codex(?![\w-])")


def _excluded_constants(tree: ast.AST) -> set[int]:
    """Docstrings and logging messages: literals that cannot start a process."""
    excluded = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            excluded.add(id(node.value))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _LOG_METHODS
        ):
            excluded.update(id(a) for a in node.args if isinstance(a, ast.Constant))
    return excluded


def _codex_literals(tree: ast.AST) -> list[int]:
    """Line numbers of string literals that mention the Codex binary.

    Everything but docstrings and logging messages counts, whatever the
    surrounding call looks like, so the ban does not depend on recognising the
    shape of a spawn.
    """
    excluded = _excluded_constants(tree)
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in excluded
        and _NAMES_CODEX.search(node.value)
    ]


@pytest.mark.parametrize(
    "snippet",
    [
        'subprocess.Popen(["codex", "exec", "-"])',
        'b = shutil.which("codex"); subprocess.Popen([b, "exec"])',
        'cmd = ["codex", "-c", "x", "exec"]; subprocess.run(cmd)',
        'asyncio.create_subprocess_shell("codex exec -")',
        'os.execvp("codex", ["codex", "review"])',
        'subprocess.run(["/usr/local/bin/codex", "e"])',
        'CODEX = "codex"',
        'subprocess.run(base + ["codex"])',
        'subprocess.run(f"codex exec {x}", shell=True)',
        'subprocess.run(f"cd {d} && codex exec", shell=True)',
        'subprocess.run(["bash", "-c", "codex exec"])',
        'subprocess.run("timeout 5 codex exec x", shell=True)',
        'subprocess.run(["npx", "codex@latest", "exec"])',
        'NAME = "Codex"',
        'subprocess.run("node codex.js exec", shell=True)',
    ],
)
def test_ban_scanner_sees_every_spawn_shape(snippet: str):
    # Guard the guard: shapes an argv-pattern matcher missed must all be seen.
    assert _codex_literals(ast.parse(snippet))


def test_ban_scanner_ignores_docstrings_logging_and_other_words():
    src = (
        '"""Run codex exec here."""\n'
        'logger.info("review: codex exec rc=%s", rc)\n'
        'x = "codex-cli 0.166.0"\n'
        'y = "codexy"\n'
    )
    assert _codex_literals(ast.parse(src)) == []


def test_codex_is_named_only_in_the_chokepoint():
    seen_chokepoint = False
    unexpected = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        hits = _codex_literals(ast.parse(path.read_text(), str(path)))
        if rel == CHOKEPOINT:
            seen_chokepoint = bool(hits)
            continue
        if len(hits) != ALLOWED.get(rel, 0):
            unexpected.append(f"{rel}: lines {hits} (allowed {ALLOWED.get(rel, 0)})")
    # Guard the guard: the scan must see the chokepoint's own constant.
    assert seen_chokepoint, "scanner did not see the binary name in the chokepoint"
    assert not unexpected, (
        "the Codex binary is named outside genesis.util.codex_cli; start Codex "
        "through codex_exec_argv with env=codex_child_env() so the run is marked "
        "dispatched: " + "; ".join(unexpected)
    )


def _called_name(call: ast.Call) -> str | None:
    func = call.func
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)


def _spawns_missing_marked_env(tree: ast.AST) -> list[int]:
    """Calls that take ``codex_exec_argv(...)`` but not ``env=codex_child_env()``."""
    missing = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        args = [a.value if isinstance(a, ast.Starred) else a for a in node.args]
        if not any(isinstance(a, ast.Call) and _called_name(a) == "codex_exec_argv" for a in args):
            continue
        env = next((kw.value for kw in node.keywords if kw.arg == "env"), None)
        if not (isinstance(env, ast.Call) and _called_name(env) == "codex_child_env"):
            missing.append(node.lineno)
    return missing


def test_pairing_check_sees_an_unmarked_spawn():
    src = (
        "subprocess.Popen(codex_exec_argv('-'))\n"
        "asyncio.create_subprocess_exec(*codex_exec_argv('-'), env=None)\n"
        "subprocess.Popen(codex_exec_argv('-'), env=codex_child_env())\n"
    )
    assert _spawns_missing_marked_env(ast.parse(src)) == [1, 2]


def test_every_codex_spawn_passes_the_marked_env():
    spawns, missing = 0, []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(), str(path))
        rel = path.relative_to(SRC).as_posix()
        spawns += sum(
            1
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and _called_name(n) == "codex_exec_argv"
        )
        missing += [f"{rel}:{line}" for line in _spawns_missing_marked_env(tree)]
    assert spawns >= 2, "expected the executor and contribution spawn sites"
    assert not missing, "codex_exec_argv spawned without env=codex_child_env(): " + ", ".join(
        missing
    )
