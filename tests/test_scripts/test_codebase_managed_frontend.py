"""Frontend admission is proved again in the actual capped child."""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from tests.test_scripts.test_codebase_managed_config import managed

frontend = managed
UNIT = "genesis-cbm-query-client-" + "a" * 32 + ".service"


@pytest.fixture
def boundary(frontend, tmp_path, monkeypatch):
    root = tmp_path / "cg"
    leaf = root / "user.slice" / frontend.SLICE / UNIT
    leaf.mkdir(parents=True)
    for node, memory, tasks in (
        (root, "max", "max"), (leaf.parent.parent, "max", "max"),
        (leaf.parent, str(2 * 1024**3), "512"), (leaf, str(256 * 1024**2), "32"),
    ):
        for name, value in (("memory.max", memory), ("memory.swap.max", "0"), ("pids.max", tasks)):
            (node / name).write_text(value)
    monkeypatch.setitem(frontend.verify_frontend_boundary.__globals__, "resolve_cgroup",
                        lambda *a: (leaf, root, 2))
    return root, leaf


@pytest.mark.parametrize("fault", [
    "none", "leaf-memory", "leaf-swap", "leaf-tasks", "slice-memory", "slice-swap",
    "slice-tasks", "ancestor-memory", "root-memory", "missing-ancestor", "true-root",
])
def test_actual_leaf_aggregate_and_ancestors(frontend, boundary, fault):
    root, leaf = boundary
    if fault in ("missing-ancestor", "true-root"):
        (root if fault == "true-root" else leaf.parent.parent).joinpath("memory.max").unlink()
    elif fault != "none":
        role, field = fault.split("-")
        node = {"leaf": leaf, "slice": leaf.parent, "ancestor": leaf.parent.parent, "root": root}[role]
        node.joinpath({"memory": "memory.max", "swap": "memory.swap.max", "tasks": "pids.max"}[field]).write_text("1")
    if fault in ("none", "true-root"):
        frontend.verify_frontend_boundary(UNIT)
    else:
        with pytest.raises((OSError, ValueError)):
            frontend.verify_frontend_boundary(UNIT)


@pytest.mark.parametrize("fault", ["bad-name", "wrong-leaf", "wrong-slice", "v1", "root"])
def test_wrong_native_topology_refuses(frontend, boundary, monkeypatch, fault):
    root, leaf = boundary
    if fault == "wrong-leaf":
        leaf = leaf.parent
    elif fault == "wrong-slice":
        leaf = root / UNIT
    elif fault == "root":
        leaf = root
    monkeypatch.setitem(frontend.verify_frontend_boundary.__globals__, "resolve_cgroup",
                        lambda *a: (leaf, root, 1 if fault == "v1" else 2))
    with pytest.raises(ValueError):
        frontend.verify_frontend_boundary("foreign.service" if fault == "bad-name" else UNIT)


class Executed(BaseException):
    """Capture the successful exec boundary without running a provider."""


@pytest.fixture
def child(frontend, tmp_path, monkeypatch):
    main = tmp_path / "main"
    main.mkdir()
    binary = tmp_path / "binary"
    binary.write_bytes(b"test")
    config = dict(main=str(main), binary=str(binary), cache=str(tmp_path),
                  runtime=str(tmp_path), sentinel=str(tmp_path / "disabled"))
    events = []
    namespace = frontend.client.__globals__
    monkeypatch.setitem(namespace, "units_dir", lambda: tmp_path / "units")
    monkeypatch.setitem(namespace, "runtime_config", lambda *a: events.append("fresh-config") or config)
    for name in ("verify_frontend_boundary", "verify_cache", "ready", "require_enabled", "check_backend"):
        monkeypatch.setitem(namespace, name, Mock(side_effect=lambda *a, n=name: events.append(n)))
    monkeypatch.setitem(namespace, "verified_binary", lambda *a: binary.open("rb"))
    monkeypatch.setattr(namespace["os"], "chdir", lambda *a: events.append("cwd"))
    return namespace, config, events


@pytest.mark.parametrize("fault", ["fresh-config", "boundary", "cache", "ready", "binary", "late-authority", "late-backend"])
def test_child_refuses_every_unproved_boundary(frontend, child, monkeypatch, fault):
    namespace, _, _ = child
    name = {"fresh-config": "runtime_config", "boundary": "verify_frontend_boundary",
            "cache": "verify_cache", "ready": "ready", "binary": "verified_binary",
            "late-authority": "require_enabled", "late-backend": "check_backend"}[fault]
    monkeypatch.setitem(namespace, name, Mock(side_effect=ValueError(fault)))
    native = Mock()
    monkeypatch.setattr(namespace["os"], "execve", native)
    with pytest.raises(ValueError, match=fault):
        frontend.client(Path("/settings"), UNIT)
    native.assert_not_called()


def test_child_lock_precedes_fresh_reads_and_is_cloexec(frontend, child, tmp_path, monkeypatch):
    namespace, config, events = child
    monkeypatch.setenv("CBM_TOOL_PROFILE", "all")
    monkeypatch.setenv("CBM_CACHE_DIR", "/foreign")

    def execute(path, argv, env):
        assert argv == [config["binary"], "--tool-profile=analysis"]
        assert path.startswith("/proc/self/fd/")
        assert os.get_inheritable(int(path.rsplit("/", 1)[1]))
        assert env["CBM_CACHE_DIR"] == config["cache"] and "CBM_TOOL_PROFILE" not in env
        lock_path = tmp_path / "units/.genesis-codebase-config.lock"
        with lock_path.open("a") as outsider, pytest.raises(BlockingIOError):
            fcntl.flock(outsider, fcntl.LOCK_EX | fcntl.LOCK_NB)
        matches = [int(p.name) for p in Path("/proc/self/fd").iterdir()
                   if p.is_symlink() and p.resolve() == lock_path]
        assert len(matches) == 1 and not os.get_inheritable(matches[0])
        assert events == ["fresh-config", "verify_frontend_boundary", "verify_cache", "ready",
                          "cwd", "require_enabled", "check_backend"]
        raise Executed

    monkeypatch.setattr(namespace["os"], "execve", execute)
    with pytest.raises(Executed):
        frontend.client(Path("/settings"), UNIT)


def test_busy_child_has_no_settings_read_or_exec(frontend, child):
    _, _, events = child
    with frontend.lifecycle_lock(), pytest.raises(BlockingIOError):
        frontend.client(Path("/settings"), UNIT)
    assert events == []


def test_launch_uses_primary_helper_literal_paths_and_native_dependencies(frontend, tmp_path, monkeypatch):
    main = tmp_path / 'main$%&|λ" '
    (main / ".git").mkdir(parents=True)
    interpreter = main / ".venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(sys.executable)
    monkeypatch.delenv("VENV_PATH", raising=False)
    namespace = frontend.launch.__globals__
    monkeypatch.setitem(namespace, "verify_cache", Mock())
    monkeypatch.setitem(namespace, "ready", Mock())
    settings = tmp_path / 'settings$%" '

    def execute(executable, argv):
        assert executable == "/usr/bin/systemd-run"
        for required in ("--pipe", "--collect", "--wait", "--working-directory=/",
                         "--slice=" + frontend.SLICE, "MemoryMax=256M", "MemorySwapMax=0",
                         "TasksMax=32", "OOMScoreAdjust=500", "Requisite=" + frontend.BACKEND,
                         "After=" + frontend.BACKEND, "StopPropagatedFrom=" + frontend.BACKEND):
            assert required in argv
        unit = next(a.split("=", 1)[1] for a in argv if a.startswith("--unit="))
        assert "--setenv=CBM_CLIENT_PYTHON=" + str(interpreter) in argv
        assert "--setenv=CBM_CLIENT_HELPER=" + str(main / "scripts/codebase_managed.py") in argv
        assert "--setenv=CBM_CLIENT_CONFIG=" + str(settings) in argv
        assert "--setenv=CBM_CLIENT_UNIT=" + unit in argv
        assert argv[argv.index("--") + 1:][:2] == ["/bin/sh", "-c"]
        assert not any(arg.startswith("--expand-environment") for arg in argv)
        assert "--scope" not in argv
        raise Executed

    monkeypatch.setattr(namespace["os"], "execv", execute)
    with pytest.raises(Executed):
        frontend.launch({"main": str(main)}, settings)


@pytest.mark.parametrize("stage", ["verify_cache", "ready"])
def test_failed_parent_preflight_never_creates_unit(frontend, monkeypatch, stage):
    namespace = frontend.launch.__globals__
    for name in ("verify_cache", "ready"):
        monkeypatch.setitem(namespace, name, Mock(side_effect=ValueError(stage) if name == stage else None))
    native = Mock()
    monkeypatch.setattr(namespace["os"], "execv", native)
    with pytest.raises(ValueError):
        frontend.launch({}, Path("/settings"))
    native.assert_not_called()


@pytest.mark.parametrize("command", ["enable", "disable", "remove", "uninstall", "verify-uninstall-locks", "status", "launch", "client"])
@pytest.mark.parametrize("home", [None, "", "/literal $HOME/%/λ home"])
def test_home_normalized_before_dispatch(frontend, monkeypatch, command, home):
    from types import SimpleNamespace

    namespace = frontend.main.__globals__
    if home is None:
        monkeypatch.delenv("HOME", raising=False)
    else:
        monkeypatch.setenv("HOME", home)
    passwd = Mock(return_value=SimpleNamespace(pw_dir="/passwd home"))
    monkeypatch.setattr(namespace["pwd"], "getpwuid", passwd)
    monkeypatch.setitem(namespace, "parse_arguments", lambda _: SimpleNamespace(command=command, config=None))
    expected = home or "/passwd home"

    def dispatched(*args):
        assert os.environ["HOME"] == expected
        raise Executed

    for name in ("lifecycle_main", "uninstall_main", "config_path"):
        monkeypatch.setitem(namespace, name, dispatched)
    with pytest.raises(Executed):
        frontend.main([])
    assert passwd.call_count == (0 if home else 1)


@pytest.mark.parametrize("home", ["relative", "/bad\npath", "/bad\rpath"])
def test_invalid_home_refuses_before_lifecycle(frontend, monkeypatch, capsys, home):
    monkeypatch.setenv("HOME", home)
    called = Mock()
    monkeypatch.setitem(frontend.main.__globals__, "lifecycle_main", called)
    assert frontend.main(["disable"]) == 1
    assert "invalid HOME" in capsys.readouterr().err
    called.assert_not_called()


@pytest.mark.parametrize("failure", [KeyError("uid"), OSError("passwd"), "relative", ""])
def test_passwd_failure_refuses_before_defaults(frontend, monkeypatch, failure):
    from types import SimpleNamespace

    monkeypatch.delenv("HOME", raising=False)
    lookup = Mock(side_effect=failure) if isinstance(failure, Exception) else Mock(return_value=SimpleNamespace(pw_dir=failure))
    monkeypatch.setattr(frontend.main.__globals__["pwd"], "getpwuid", lookup)
    assert frontend.main(["status"]) == 1


@pytest.mark.parametrize("kind", ["missing", "directory", "nonexecutable", "dangling", "relative", "newline", "carriage-return"])
def test_frontend_interpreter_refuses_without_fallback(frontend, tmp_path, monkeypatch, kind):
    venv = tmp_path / "venv"
    interpreter = venv / "bin/python"
    interpreter.parent.mkdir(parents=True)
    if kind == "directory":
        interpreter.mkdir()
    elif kind == "nonexecutable":
        interpreter.write_text("not executable")
    elif kind == "dangling":
        interpreter.symlink_to(tmp_path / "absent")
    raw = {"relative": "relative", "newline": str(venv) + "\n", "carriage-return": str(venv) + "\r"}.get(kind, str(venv))
    monkeypatch.setenv("VENV_PATH", raw)
    with pytest.raises(ValueError):
        frontend.frontend_command(tmp_path, tmp_path / "settings", UNIT, frontend.BACKEND, frontend.SLICE)


@pytest.mark.parametrize("literal", ["space path", "$HOME ${USER}", "%n %%", "quotes'\"", "back\\slash", "λ雪", "$(touch marker)", "`touch marker`"])
def test_fixed_shell_transport_preserves_literal_fields(frontend, tmp_path, monkeypatch, literal):
    import json

    main = tmp_path / literal
    helper = main / "scripts/codebase_managed.py"
    helper.parent.mkdir(parents=True)
    helper.write_text("import json,os,sys; print(json.dumps([sys.argv[1:],os.environ['HOME'],os.environ['VENV_PATH']]))")
    venv = tmp_path / ("custom " + literal)
    interpreter = venv / "bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(sys.executable)
    monkeypatch.setenv("VENV_PATH", str(venv))
    home = str(tmp_path / ("home " + literal)) + "/./"
    monkeypatch.setenv("HOME", home)
    settings = tmp_path / ("settings " + literal)
    argv = frontend.frontend_command(main, settings, UNIT, frontend.BACKEND, frontend.SLICE)
    env = {arg[len("--setenv="):].split("=", 1)[0]: arg.split("=", 2)[2] for arg in argv if arg.startswith("--setenv=")}
    assert env["CBM_CLIENT_PYTHON"] == str(interpreter)
    assert env["HOME"] == home
    # Native doubled-dollar decoding is independently exercised by the manager E2E.
    shell = argv[-1].replace("$$", "$")
    result = subprocess.run(["/bin/sh", "-c", shell], env=env, cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [["--config", str(settings), "client", "--unit", UNIT], env["HOME"], str(venv)]
    assert not (tmp_path / "marker").exists()
