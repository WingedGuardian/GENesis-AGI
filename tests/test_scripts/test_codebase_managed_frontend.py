"""Frontend admission is proved again in the actual capped child."""

from __future__ import annotations

import fcntl
import os
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
    namespace = frontend.launch.__globals__
    monkeypatch.setitem(namespace, "verify_cache", Mock())
    monkeypatch.setitem(namespace, "ready", Mock())
    settings = tmp_path / 'settings$%" '

    def execute(executable, argv):
        assert executable == "/usr/bin/systemd-run"
        for required in ("--pipe", "--collect", "--wait", "--expand-environment=no",
                         "--slice=" + frontend.SLICE, "MemoryMax=256M", "MemorySwapMax=0",
                         "TasksMax=32", "OOMScoreAdjust=500", "Requisite=" + frontend.BACKEND,
                         "After=" + frontend.BACKEND, "StopPropagatedFrom=" + frontend.BACKEND):
            assert required in argv
        unit = next(a.split("=", 1)[1] for a in argv if a.startswith("--unit="))
        assert argv[argv.index("--") + 1:] == ["/usr/bin/python3", "-I",
            str(main / "scripts/codebase_managed.py"), "--config", str(settings), "client", "--unit", unit]
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
