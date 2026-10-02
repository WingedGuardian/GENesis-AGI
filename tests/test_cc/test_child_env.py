"""The dispatched-session env pins, and the lock that keeps every spawn site on them.

Every module that marks a child as Genesis-dispatched (``GENESIS_CC_SESSION``
set to ``"1"``) builds that child's env by hand from ``os.environ``. A new spawn
site that forgets the shared pins would silently let a server-side rollout turn
function hooks on in background sessions, so the lock enumerates the sites
from the source rather than naming the ones known today.
"""

from __future__ import annotations

import ast
from pathlib import Path

from genesis.cc.child_env import FUNCTION_HOOKS_ENV, pin_dispatched_env

SRC = Path(__file__).resolve().parents[2] / "src" / "genesis"


def test_pin_sets_function_hooks_off_over_an_inherited_opt_in():
    env = {FUNCTION_HOOKS_ENV: "1", "HOME": "/h"}
    assert pin_dispatched_env(env) is env
    assert env == {FUNCTION_HOOKS_ENV: "0", "HOME": "/h"}


def test_pin_writes_off_when_nothing_was_inherited():
    assert pin_dispatched_env({})[FUNCTION_HOOKS_ENV] == "0"


def _names_claude(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value == "claude" or node.value.endswith("/claude")
    if isinstance(node, ast.Name):
        return "claude" in node.id.lower()
    if isinstance(node, ast.Attribute):
        return "claude" in node.attr.lower()
    return False


def _spawns_a_session(tree: ast.AST) -> bool:
    """True when the module builds a `claude -p` argv: a list or tuple whose first
    element names claude and which carries "-p". A `claude --version` probe is not
    a session and does not match."""
    return any(
        isinstance(node, (ast.List, ast.Tuple))
        and node.elts
        and _names_claude(node.elts[0])
        and any(isinstance(e, ast.Constant) and e.value == "-p" for e in node.elts[1:])
        for node in ast.walk(tree)
    )


_CC_FLAGS = {"--output-format", "--dangerously-skip-permissions", "--max-turns", "--model"}


def _text(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value for v in node.values if isinstance(v, ast.Constant))
    return None


def _carries_cc_flags(tree: ast.AST) -> bool:
    """True when the module builds a print-mode argv or command string by
    Claude Code's own flags, whatever the binary is called (`cc_path`, a remote
    path): a list with "-p" plus one of the flags, or an f-string holding both
    (review: two sites were invisible to the name-based match)."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)):
            vals = {_text(e) for e in node.elts}
            if "-p" in vals and vals & _CC_FLAGS:
                return True
        if isinstance(node, ast.JoinedStr):
            text = _text(node) or ""
            if " -p" in text and any(f in text for f in _CC_FLAGS):
                return True
    return False


def _marks_dispatched(tree: ast.AST) -> bool:
    """True when the module assigns "1" to a GENESIS_CC_SESSION subscript."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.slice, ast.Constant)
                and target.slice.value == "GENESIS_CC_SESSION"
                and isinstance(node.value, ast.Constant)
                and node.value.value == "1"
            ):
                return True
    return False


def _calls_pin(tree: ast.AST) -> bool:
    """A pin_dispatched_env call, or a use of FUNCTION_HOOKS_ENV (the remote
    command sets it as a shell assignment)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if name == "pin_dispatched_env":
                return True
        if isinstance(node, ast.Name) and node.id == "FUNCTION_HOOKS_ENV":
            return True
    return False


def _spawn_sites() -> list[Path]:
    sites = []
    for p in SRC.rglob("*.py"):
        tree = ast.parse(p.read_text(), str(p))
        if _spawns_a_session(tree) or _carries_cc_flags(tree) or _marks_dispatched(tree):
            sites.append(p)
    return sorted(sites)


def test_the_enumeration_finds_the_known_spawn_sites():
    """Guard the guard: if the AST match stopped matching, the lock below would
    pass on an empty population. The population is found three ways (a `claude -p`
    argv, Claude Code's own flags in an argv or command string, or the
    dispatched marker), because no single one sees every site.
    None sees a command whose binary and flags all arrive in variables at run
    time, so this is a floor on the population, not a proof of it."""
    names = {p.relative_to(SRC).as_posix() for p in _spawn_sites()}
    assert {
        "cc/invoker.py",
        "session_awareness/headless.py",
        "experimentation/cc_router.py",
        "dashboard/routes/updates.py",
        "guardian/diagnosis.py",
        "modules/external/ipc.py",
    } <= names, names


def test_every_dispatched_spawn_site_applies_the_pins():
    missing = [
        p.relative_to(SRC).as_posix()
        for p in _spawn_sites()
        if not _calls_pin(ast.parse(p.read_text(), str(p)))
    ]
    assert not missing, (
        "these modules start a claude session but do not call "
        f"genesis.cc.child_env.pin_dispatched_env on its env: {missing}"
    )


def test_the_update_orchestrator_script_pins_its_sessions():
    """The orchestrator is a generated script that runs outside genesis, so it
    cannot import the helper and carries its own copy of the pin. Rendered and
    compiled here so the copy cannot drift out of its spawn call unnoticed."""
    import string

    from genesis.dashboard.routes import updates

    fields = {f for _, f, _, _ in string.Formatter().parse(updates._ORCHESTRATOR_TEMPLATE) if f}
    rendered = updates._ORCHESTRATOR_TEMPLATE.format(**{f: "x" for f in fields})
    tree = ast.parse(rendered)
    [spawn] = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "spawn_cc"]
    src = ast.unparse(spawn)
    assert "'CLAUDE_CODE_ENABLE_FUNCTION_HOOKS': '0'" in src, src
    assert "env=env" in src, src


def test_the_detached_update_session_is_pinned(monkeypatch, tmp_path):
    from genesis.dashboard.routes import updates

    captured = {}

    class _Proc:
        pid = 1

    def fake_popen(cmd, **kw):
        captured.update(kw)
        return _Proc()

    monkeypatch.setattr(updates, "_HOME", tmp_path)
    monkeypatch.setattr(updates.subprocess, "Popen", fake_popen)
    monkeypatch.setenv(FUNCTION_HOOKS_ENV, "1")
    updates._spawn_detached_cc("p", "haiku")
    assert captured["env"][FUNCTION_HOOKS_ENV] == "0"
