"""``systemd-run --scope`` passes a command's arguments as written, on every systemd.

Systemd older than 254 rejects the option that asks for this, so every launcher asks
systemd-run first. The two sweeps at the end fail when a launcher does not: they see a
Python string or a shell command line naming ``--scope``, including one split across
lines, and not a launch assembled from pieces at run time.
"""

from __future__ import annotations

import ast
import logging
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from genesis.cc import invoker
from genesis.dashboard.routes import updates
from genesis.hostmetrics import run as hostmetrics_run
from genesis.util import systemd as sd

_REPO = Path(__file__).resolve().parents[2]
_FLAG = "--expand-environment=no"
_REAL_PROBE = sd._systemd_run_refuses_literal_arguments  # before the suite's autouse stub


@pytest.mark.parametrize(
    ("returncode", "expected", "warns"),
    [(0, [_FLAG], False), (1, [], False), (126, [_FLAG], True), (-9, [_FLAG], True)],
    ids=["accepted", "refused", "cannot-execute", "killed"],
)
def test_only_a_definite_refusal_drops_the_option(monkeypatch, caplog, returncode, expected, warns):
    calls = []

    def fake_run(argv, **_kw):
        calls.append(argv)
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(sd.subprocess, "run", fake_run)
    monkeypatch.setattr(sd, "_systemd_run_refuses_literal_arguments", _REAL_PROBE)
    with caplog.at_level(logging.WARNING, logger=sd.__name__):
        assert sd.literal_scope_args() == expected
    assert calls == [["systemd-run", _FLAG, "--version"]]
    assert bool(caplog.records) == warns


@pytest.mark.parametrize(
    ("error", "expected", "warns"),
    [
        (FileNotFoundError("systemd-run"), [], False),
        (subprocess.TimeoutExpired("systemd-run", 10), [_FLAG], True),
        (PermissionError("nope"), [_FLAG], True),
    ],
    ids=["missing", "hung", "not-executable"],
)
def test_a_question_that_fails_keeps_the_option_and_says_so(monkeypatch, caplog, error, expected, warns):
    """Absent systemd-run is ordinary: nothing will start a scope."""

    def fake_run(*_a, **_kw):
        raise error

    monkeypatch.setattr(sd.subprocess, "run", fake_run)
    monkeypatch.setattr(sd, "_systemd_run_refuses_literal_arguments", _REAL_PROBE)
    with caplog.at_level(logging.WARNING, logger=sd.__name__):
        assert sd.literal_scope_args() == expected
    assert bool(caplog.records) == warns


@pytest.mark.user_manager
def test_arguments_reach_a_real_scope_as_written(monkeypatch):
    """The requirement itself, through the real tool. Never skips, because the suite's
    skip ceiling has no room: with no user manager it still grades the real answer.

    The control asks for the option's opposite, so this can fail on any systemd that
    has the option, whatever its default.
    """
    if shutil.which("systemd-run") is None:
        return
    monkeypatch.setattr(sd, "_systemd_run_refuses_literal_arguments", _REAL_PROBE)
    flag = sd.literal_scope_args()
    assert flag in ([_FLAG], [])
    if not flag:
        return
    env = sd.systemctl_env()
    reachable = subprocess.run(
        ["systemd-run", "--user", "--scope", "--quiet", "--", "/bin/true"],
        env=env,
        capture_output=True,
        timeout=30,
    )
    if reachable.returncode != 0:
        return
    args = ["${HOME}", "pid $$ here", "$HOME", "x${HOME}y"]
    command = ["/usr/bin/printf", "[%s]", *args]
    written = "".join(f"[{a}]" for a in args)
    control = subprocess.run(
        ["systemd-run", "--user", "--scope", "--quiet", "--expand-environment=yes", "--", *command],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert control.stdout != written, (
        "the control changed nothing, so this test cannot see a failure"
    )
    built = {
        "invoker": invoker._scope_argv(*command),
        "hostmetrics": hostmetrics_run.scope_argv(
            hostmetrics_run.unit_name("literal-args-test", "1"), [], None, *command
        ),
    }
    for name, argv in built.items():
        proc = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30)
        assert proc.stdout == written, (name, proc.stdout, proc.stderr)


def _built(monkeypatch, flag: list[str]) -> dict[str, list[str]]:
    """What each Python launcher builds, with the helper answering ``flag``."""
    monkeypatch.setattr(invoker, "literal_scope_args", lambda: flag)
    monkeypatch.setattr(hostmetrics_run, "literal_scope_args", lambda: flag)
    return {
        "invoker": invoker._scope_argv("/bin/true"),
        "hostmetrics": hostmetrics_run.scope_argv("u", [], None, "/bin/true"),
    }


@pytest.mark.parametrize("flag", [[_FLAG], []], ids=["supported", "too-old"])
def test_the_launchers_pass_what_the_helper_answers_before_the_command(monkeypatch, flag):
    for name, argv in _built(monkeypatch, flag).items():
        split = argv.index("--")
        assert (_FLAG in argv[:split]) == bool(flag), (name, argv)
        assert argv[split + 1 :] == ["/bin/true"], (name, argv)


@pytest.mark.parametrize("flag", [[_FLAG], []], ids=["supported", "too-old"])
def test_the_update_route_passes_it_on_its_probe_and_its_launch(tmp_path, updates_sandbox, flag):
    seen: list[list[str]] = []

    def record(argv, **_kw):
        seen.append(argv)
        return SimpleNamespace(returncode=0, pid=4242)

    with (
        patch.object(updates, "literal_scope_args", return_value=flag),
        patch.object(updates, "jsonify", lambda x: x),
        patch.object(updates.subprocess, "run", record),
        patch.object(updates.subprocess, "Popen", record),
        patch.object(updates, "_HOME", tmp_path),
    ):
        assert updates_sandbox.originals["_systemd_run_scope_available"]({}) is True
        with patch.object(updates, "_systemd_run_scope_available", return_value=True):
            updates._apply_direct(tmp_path / "pid")
    launches = [argv for argv in seen if "--scope" in argv]
    assert len(launches) == 2, seen
    for argv in launches:
        assert (_FLAG in argv[: argv.index("--")]) == bool(flag), argv


def _python_files() -> list[Path]:
    files = [
        p for root in ("src/genesis", "scripts", ".claude") for p in (_REPO / root).rglob("*.py")
    ]
    # Guards read these words in commands; they launch nothing.
    return sorted(p for p in files if not {"hooks", "worktrees"} & set(p.relative_to(_REPO).parts))


def _docstrings(tree: ast.AST) -> set[int]:
    return {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }


def _python_scope_mentions() -> dict[str, bool]:
    """Each module with a non-docstring string naming ``--scope``, and whether it asks."""
    found = {}
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip = _docstrings(tree)
        nodes = list(ast.walk(tree))
        names = any(
            isinstance(n, ast.Constant)
            and isinstance(n.value, str)
            and "--scope" in n.value
            and id(n) not in skip
            for n in nodes
        )
        if names:
            found[str(path.relative_to(_REPO))] = any(
                isinstance(n, ast.Name | ast.Attribute)
                and "literal_scope_args" in (getattr(n, "id", ""), getattr(n, "attr", ""))
                for n in nodes
            )
    return found


def _python_scope_lists() -> list[tuple[str, str, bool]]:
    """Every function holding a ``--scope`` argv list, and whether it asks for each."""
    found = []
    for path in _python_files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            inner = list(ast.walk(node))
            lists = [
                n
                for n in inner
                if isinstance(n, ast.List)
                and any(isinstance(e, ast.Constant) and e.value == "--scope" for e in n.elts)
            ]
            if lists:
                asks = sum(
                    isinstance(n, ast.Call) and getattr(n.func, "id", "") == "literal_scope_args"
                    for n in inner
                )
                found.append((str(path.relative_to(_REPO)), node.name, asks >= len(lists)))
    return found


def test_no_python_launcher_builds_a_scope_without_asking():
    lists = _python_scope_lists()
    # Guard the guard: a sweep that matched nothing would pass here for ever.
    assert {
        ("src/genesis/cc/invoker.py", "_scope_argv"),
        ("src/genesis/hostmetrics/run.py", "scope_argv"),
        ("src/genesis/dashboard/routes/updates.py", "_apply_direct"),
    } <= {(f, n) for f, n, _ in lists}
    assert not [row for row in lists if not row[2]]
    mentions = _python_scope_mentions()
    assert "src/genesis/cc/invoker.py" in mentions
    assert not [path for path, asks in mentions.items() if not asks]


def _shell_scope_lines(text: str) -> list[str]:
    """Command lines naming ``--scope``, with continuation lines joined."""
    joined = re.sub(r"\s*\\\n\s*", " ", text)
    return [
        line.strip()
        for line in joined.splitlines()
        if "--scope" in line and not re.match(r"\s*(#|echo\b|printf\b)", line)
    ]


def test_the_shell_sweep_sees_a_launch_split_across_lines():
    """Guard the guard: the real launches are written this way."""
    split = '/usr/bin/systemd-run --user \\\n    --scope --quiet -- cmd\n# --scope in a comment\necho "--scope"\n'
    assert _shell_scope_lines(split) == ["/usr/bin/systemd-run --user --scope --quiet -- cmd"]


def test_no_shell_launcher_starts_a_scope_without_it():
    offenders = []
    checked = 0
    for root in (_REPO / "scripts", _REPO / ".claude" / "mcp"):
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix in (".py", ".pyc") or "hooks" in path.relative_to(_REPO).parts:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for line in _shell_scope_lines(text):
                checked += 1
                head = line.split(" -- ", 1)[0]  # the option belongs to systemd-run, not its command
                if "_SD_LITERAL" not in head:
                    offenders.append(f"{path.relative_to(_REPO)}: {line[:90]}")
    assert checked >= 2, "the sweep stopped finding the code-intel launches"
    assert not offenders, offenders
