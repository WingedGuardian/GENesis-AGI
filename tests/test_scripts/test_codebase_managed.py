"""Native integration boundaries: malformed settings never select raw fallback."""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SELECTION_LIB = ROOT / "scripts/lib/codebase_managed_selection.sh"
spec = importlib.util.spec_from_file_location(
    "codebase_managed", ROOT / "scripts/codebase_managed.py"
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def _manager(values: dict, actions: list):
    """Fake systemctl for both `show -p X --value` and batched `show -p A -p B`."""

    def systemctl(*args):
        if args[0] != "show":
            actions.append(args)
            return ""
        unit = args[1]
        names = [args[i + 1] for i, arg in enumerate(args) if arg == "-p"]
        lookup = {name: values(unit, name) if callable(values) else values[name] for name in names}
        if "--value" in args:
            return "\n".join(lookup.values())
        return "\n".join(f"{k}={v}" for k, v in lookup.items())

    return systemctl


# Bound at import: tests patch m.subprocess.run, which is this same module.
_RUN = subprocess.run


def _selected(config: Path, home: Path) -> int:
    env = {k: v for k, v in os.environ.items() if k != "CODEBASE_MEMORY_MCP_MANAGED_CONFIG"}
    return _RUN(
        [
            "bash",
            "-c",
            '. "$0" && codebase_managed_selected "$1" "$2"',
            SELECTION_LIB,
            str(config),
            str(home),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    ).returncode


@pytest.fixture(autouse=True)
def primary_checkout_script(tmp_path, monkeypatch):
    # The config fixture's main is tmp_path; act as its primary-checkout copy.
    monkeypatch.setattr(m, "SCRIPT", tmp_path / "scripts/codebase_managed.py", raising=False)


@pytest.fixture
def config(tmp_path):
    return dict(
        version=1,
        enabled=True,
        main=str(tmp_path),
        binary=str(tmp_path / "binary"),
        cache=str(tmp_path / "cache"),
        runtime=str(tmp_path / "runtime"),
        sentinel=str(tmp_path / "disabled"),
        build=m.BUILD,
        name="genesis-cbm-query",
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("version", 2),
        ("enabled", "true"),
        ("build", "wrong"),
        ("name", "other.service"),
        ("main", "relative"),
        ("cache", None),
        ("runtime", "/tmp/a\nb"),
        ("binary", "/tmp/a\x00b"),
        ("sentinel", "~/disabled"),
    ],
)
def test_malformed_settings_refused(tmp_path, config, key, value):
    config[key] = value
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError):
        m.read_settings(path)


@pytest.mark.parametrize("disabled", ["settings", "sentinel"])
def test_disabled_refuses_before_native_or_manager(tmp_path, config, monkeypatch, disabled):
    if disabled == "settings":
        config["enabled"] = False
    else:
        Path(config["sentinel"]).write_text("incident")
    monkeypatch.setattr(m, "systemctl", lambda *a: pytest.fail("must not contact manager"))
    monkeypatch.setattr(m, "verify_cache", lambda *a: pytest.fail("must not open cache"))
    with pytest.raises(ValueError, match="disabled"):
        m.launch(config, tmp_path / "settings")


def test_unpinned_executable_refuses(tmp_path):
    executable = tmp_path / "provider"
    executable.write_bytes(b"#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    with pytest.raises(ValueError, match="unsupported"):
        m.verified_binary(executable)


def test_environment_removes_inherited_provider_overrides(config, monkeypatch):
    monkeypatch.setenv("CBM_TOOL_PROFILE", "all")
    monkeypatch.setenv("CBM_CACHE_DIR", "/foreign")
    monkeypatch.setenv("CBM_RUNTIME_DIR", "/foreign")
    monkeypatch.setenv("CBM_ALLOWED_ROOT", "/")
    env = m.native_env(config)
    assert set(k for k in env if k.startswith("CBM_")) == {
        "CBM_CACHE_DIR",
        "CBM_RUNTIME_DIR",
        "CBM_ALLOWED_ROOT",
    }
    assert env["CBM_ALLOWED_ROOT"] == config["main"]
    assert env["CBM_CACHE_DIR"] == config["cache"]


def test_frontend_does_not_pull_backend_start(config, tmp_path, monkeypatch):
    # Launched from a linked worktree's launcher copy, which may later be reaped.
    monkeypatch.setattr(m, "SCRIPT", tmp_path / ".worktrees/b/scripts/codebase_managed.py")
    monkeypatch.setattr(m, "require_enabled", lambda *a: None)
    monkeypatch.setattr(m, "verify_cache", lambda *a: None)
    monkeypatch.setattr(m, "check_backend", lambda *a: None)
    monkeypatch.setattr(m, "verify_units", lambda *a, **k: None)
    captured = []
    monkeypatch.setattr(m.os, "execv", lambda _, args: captured.extend(args))
    m.launch(config, tmp_path / "settings")
    assert "Requisite=genesis-cbm-query.service" in captured
    assert "After=genesis-cbm-query.service" in captured
    assert "StopPropagatedFrom=genesis-cbm-query.service" in captured
    assert not any("BindsTo" in a or "Requires=" in a for a in captured)
    assert "MemoryMax=256M" in captured and "MemorySwapMax=0" in captured
    assert "--slice=genesis-cbm-query-clients.slice" in captured
    assert "--tool-profile=all" not in captured
    # The client runs the configured checkout's script, isolated, like the backend.
    script = str(Path(config["main"]) / "scripts/codebase_managed.py")
    assert captured[captured.index("--") + 1 : captured.index("--") + 4] == [
        "/usr/bin/python3",
        "-I",
        script,
    ]


def test_render_never_enables_or_starts_backend(config, tmp_path):
    units = m.render_units(config, tmp_path / "settings")
    assert set(units) == {"genesis-cbm-query.service", "genesis-cbm-query-clients.slice"}
    assert "MemoryMax=2G" in units["genesis-cbm-query-clients.slice"]
    assert "MemorySwapMax=0" in units["genesis-cbm-query.service"]
    assert "Restart=no" in units["genesis-cbm-query.service"]
    assert " serve" not in units["genesis-cbm-query.service"]  # quoted argv
    assert '"serve"' in units["genesis-cbm-query.service"]


def test_batch_other_repository_refused_before_provider(config, tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setattr(m, "verify_cache", lambda *a: pytest.fail("must not open cache"))
    with pytest.raises(ValueError, match="main checkout"):
        m.batch_values(config, str(other))


def test_explicit_managed_paths_reject_unit_injection():
    with pytest.raises(ValueError):
        m.quote_unit("/tmp/a\nExecStart=/bin/false")
    assert m.quote_unit('/tmp/a%"$x') == '"/tmp/a%%\\"$x"'


@pytest.mark.parametrize("conflict", ["dangling-settings", "dangling-state", "loaded-unit"])
def test_configure_preserves_foreign_artifacts_before_pin_or_state(tmp_path, monkeypatch, conflict):
    monkeypatch.setenv("HOME", str(tmp_path))
    main = tmp_path / "repo"
    (main / ".git").mkdir(parents=True)
    monkeypatch.setattr(m, "SCRIPT", main.resolve() / "scripts/codebase_managed.py", raising=False)
    path = tmp_path / "settings.json"
    foreign = tmp_path / "foreign-target"
    if conflict == "dangling-settings":
        path.symlink_to(foreign)
    state = tmp_path / "state"
    if conflict == "dangling-state":  # e.g. a state link to a volume not yet mounted
        state.symlink_to(foreign)
    monkeypatch.setattr(
        m, "verified_binary", lambda *a: pytest.fail("must preserve before opening binary")
    )
    # Only the loaded-unit case may refuse on the manager's evidence; the
    # filesystem cases must refuse on their own.
    free = {"ActiveState": "inactive", "ControlGroup": "", "LoadState": "not-found"}
    monkeypatch.setattr(
        m, "systemctl", lambda *a: "loaded" if conflict == "loaded-unit" else free[a[3]]
    )
    args = argparse.Namespace(
        main=str(main),
        binary=str(tmp_path / "binary"),
        state=str(state),
        sentinel=str(tmp_path / "disabled"),
        name="genesis-cbm-test",
    )
    with pytest.raises(ValueError, match="preserved"):
        m.configure(args, path)
    assert not foreign.exists() and not state.exists()
    assert state.is_symlink() is (conflict == "dangling-state")


@pytest.mark.parametrize("stop_fails", [False, True])
def test_activation_start_failure_rolls_back_and_stops_owned_unit(
    tmp_path, config, monkeypatch, capsys, stop_fails
):
    import subprocess

    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".config/systemd/user").mkdir(parents=True)
    path = tmp_path / "settings.json"
    config["enabled"] = False
    path.write_text(json.dumps(config))
    monkeypatch.setattr(m, "verify_cache", lambda *a: None)
    monkeypatch.setattr(m, "verify_units", lambda *a: None)
    monkeypatch.setattr(m, "verified_binary", lambda *a: contextlib.nullcontext())
    actions = []

    def manager(*args):
        actions.append(args)
        if args[0] == "start":
            raise subprocess.CalledProcessError(1, "start")
        if stop_fails:
            raise subprocess.CalledProcessError(2, "stop")
        return ""

    monkeypatch.setattr(m, "systemctl", manager)
    with pytest.raises(subprocess.CalledProcessError) as error:
        m.set_enabled(config, path, True)
    assert error.value.cmd == "start"
    assert ("rollback stop failed" in capsys.readouterr().err) is stop_fails
    assert m.read_settings(path)["enabled"] is False
    assert actions == [
        ("start", "genesis-cbm-query.service"),
        ("stop", "genesis-cbm-query.service"),
    ]


def test_activation_preserves_armed_sentinel(tmp_path, config, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".config/systemd/user").mkdir(parents=True)
    path = tmp_path / "settings.json"
    config["enabled"] = False
    path.write_text(json.dumps(config))
    sentinel = Path(config["sentinel"])
    sentinel.write_text("incident")
    monkeypatch.setattr(m, "verify_units", lambda *a: None)
    monkeypatch.setattr(m, "systemctl", lambda *a: pytest.fail("must not start disabled backend"))
    with pytest.raises(ValueError, match="sentinel"):
        m.set_enabled(config, path, True)
    assert sentinel.read_text() == "incident" and m.read_settings(path)["enabled"] is False


@pytest.mark.parametrize(
    "fault", ["none", "version", "leaf-cap", "leaf-swap", "parent-cap", "parent-swap", "scope"]
)
def test_running_frontend_boundary_accepts_only_complete_proof(
    tmp_path, config, monkeypatch, fault
):
    unit = config["name"] + "-client-" + "a" * 32 + ".service"
    parent = tmp_path / (config["name"] + "-clients.slice")
    leaf = parent / unit
    leaf.mkdir(parents=True)
    for directory, cap in ((tmp_path, "max"), (parent, str(2 * m.GIB)), (leaf, str(m.GIB // 4))):
        (directory / "memory.max").write_text(cap)
        (directory / "memory.swap.max").write_text("0")
    target = {
        "leaf-cap": (leaf / "memory.max", "1"),
        "leaf-swap": (leaf / "memory.swap.max", "max"),
        "parent-cap": (parent / "memory.max", "max"),
        "parent-swap": (parent / "memory.swap.max", "max"),
    }.get(fault)
    if target:
        target[0].write_text(target[1])
    monkeypatch.setattr(
        m, "resolve_cgroup", lambda *a: (leaf, tmp_path, 1 if fault == "version" else 2)
    )
    if fault == "none":
        m.verify_boundary(config, "client", unit)
    else:
        with pytest.raises(ValueError):
            m.verify_boundary(config, "client", "foreign.service" if fault == "scope" else unit)


@pytest.mark.parametrize("reported_pid,accepted", [("42", True), ("43", False)])
def test_ready_requires_native_rpc_matching_managed_pid(
    tmp_path, config, monkeypatch, reported_pid, accepted
):
    import subprocess

    provider = tmp_path / "provider"
    provider.write_bytes(b"test")
    monkeypatch.setattr(m, "verified_binary", lambda *a: provider.open("rb"))
    monkeypatch.setattr(m, "check_backend", lambda *a, **k: None)
    monkeypatch.setattr(m, "systemctl", lambda *a: "42")
    response = subprocess.CompletedProcess(
        [], 0, "daemon: active (permanent)\n  pid: " + reported_pid + "\n", ""
    )
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: response)
    ticks = iter((0, 0, 61))
    monkeypatch.setattr(m.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(m.time, "sleep", lambda *a: None)
    if accepted:
        m.ready(config)
    else:
        with pytest.raises(ValueError, match="ready"):
            m.ready(config)


def _write_units(config: dict, path: Path, home: Path) -> Path:
    directory = home / ".config/systemd/user"
    directory.mkdir(parents=True, exist_ok=True)
    for name, body in m.render_units(config, path).items():
        (directory / name).write_text(body)
    return directory


def _stale(text: str) -> str:
    # An older template keeps the ownership header and changes the body.
    assert "TasksMax=128\n" in text or "TasksMax=512\n" in text
    return text.replace("TasksMax=128\n", "TasksMax=64\n").replace(
        "TasksMax=512\n", "TasksMax=256\n"
    )


@pytest.mark.parametrize(
    "fault", ["none", "modified", "symlink", "foreign", "drop-in", "reload", "old-template"]
)
@pytest.mark.parametrize("enabled", [False, True])
def test_lifecycle_verifies_units_before_manager_mutation(
    tmp_path, config, monkeypatch, fault, enabled
):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(config))
    directory = _write_units(config, path, tmp_path)
    fragment = directory / m.backend(config)
    if fault == "modified":
        fragment.write_text(m.MARKER + "[Service]\nExecStart=/bin/true\n")
    if fault == "symlink":
        target = tmp_path / "foreign"
        target.write_text(fragment.read_text())
        fragment.unlink()
        fragment.symlink_to(target)
    if fault == "old-template":
        fragment.write_text(_stale(fragment.read_text()))
    actions = []
    values = {
        "FragmentPath": lambda unit: (
            "/foreign/unit" if fault == "foreign" else str(directory / unit)
        ),
        "DropInPaths": lambda unit: "/foreign/drop.conf" if fault == "drop-in" else "",
        "NeedDaemonReload": lambda unit: "yes" if fault == "reload" else "no",
    }
    monkeypatch.setattr(m, "systemctl", _manager(lambda u, n: values[n](u), actions))
    monkeypatch.setattr(m, "verify_cache", lambda *a: None)
    monkeypatch.setattr(m, "verified_binary", lambda *a: contextlib.nullcontext())
    # Ownership is enough to stop; starting requires the current template.
    proceeds = fault == "none" or (fault == "old-template" and not enabled)
    if proceeds:
        m.set_enabled(config, path, enabled)
        assert actions == [("start" if enabled else "stop", m.backend(config))]
        assert m.read_settings(path)["enabled"] is enabled
    else:
        with pytest.raises(ValueError) as error:
            m.set_enabled(config, path, enabled)
        assert not actions
        if enabled:
            assert m.read_settings(path) == config
            if fault == "old-template":
                assert "repair-units" in str(error.value)
        else:
            assert "NOT stopped" in str(error.value)
            assert m.read_settings(path)["enabled"] is False


def test_disable_with_unverifiable_fragment_still_persists_disabled(tmp_path, config, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(config))
    directory = _write_units(config, path, tmp_path)
    (directory / m.backend(config)).write_text("[Service]\nExecStart=/usr/bin/vendor\n")
    actions = []
    monkeypatch.setattr(m, "systemctl", _manager({}, actions))
    with pytest.raises(ValueError):
        m.set_enabled(config, path, False)
    assert m.read_settings(path)["enabled"] is False
    assert not actions  # the unproven unit is never stopped by name


def test_verify_units_compares_loaded_fragment_by_identity(tmp_path, config, monkeypatch):
    real = tmp_path / "real-home"
    real.mkdir()
    alias = tmp_path / "alias-home"
    alias.symlink_to(real)
    monkeypatch.setenv("HOME", str(real))
    path = tmp_path / "settings.json"
    _write_units(config, path, real)
    values = {
        "FragmentPath": lambda unit: str(alias / ".config/systemd/user" / unit),
        "DropInPaths": lambda unit: "",
        "NeedDaemonReload": lambda unit: "no",
    }
    actions = []
    monkeypatch.setattr(m, "systemctl", _manager(lambda u, n: values[n](u), actions))
    m.verify_units(config, path)
    assert actions == []


def test_verify_units_queries_each_unit_once(tmp_path, config, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / "settings.json"
    directory = _write_units(config, path, tmp_path)
    calls = []

    def systemctl(*args):
        calls.append(args)
        return f"NeedDaemonReload=no\nFragmentPath={directory / args[1]}\nDropInPaths="

    monkeypatch.setattr(m, "systemctl", systemctl)
    m.verify_units(config, path)
    assert [c[1] for c in calls] == list(m.render_units(config, path))
    assert all(c.count("-p") == 3 and "--value" not in c for c in calls)


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "failed-idle",
        "active",
        "failed-busy",
        "foreign",
        "other-config",
        "symlink",
        "loaded-elsewhere",
        "non-primary-checkout",
    ],
)
def test_repair_units_rewrites_only_owned_inactive_fragments(tmp_path, config, monkeypatch, fault):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(config))
    directory = _write_units(config, path, tmp_path)
    current = m.render_units(config, path)
    for name, body in current.items():
        (directory / name).write_text(_stale(body))
    slice_unit = config["name"] + "-clients.slice"
    if fault == "foreign":
        (directory / slice_unit).write_text("[Slice]\nMemoryMax=8G\n")
    if fault == "other-config":
        (directory / slice_unit).write_text(
            _stale(m.render_units(config, tmp_path / "other.json")[slice_unit])
        )
    if fault == "symlink":
        target = tmp_path / "linked.slice"
        target.write_text((directory / slice_unit).read_text())
        (directory / slice_unit).unlink()
        (directory / slice_unit).symlink_to(target)
    before = {name: (directory / name).read_text() for name in current}
    values = {
        "ActiveState": lambda unit: (
            "active" if fault == "active" else "failed" if fault.startswith("failed") else "inactive"
        ),
        "ControlGroup": lambda unit: "/user.slice/x" if fault in ("active", "failed-busy") else "",
        "FragmentPath": lambda unit: (
            "/etc/systemd/user/" + unit if fault == "loaded-elsewhere" else str(directory / unit)
        ),
    }
    actions = []
    monkeypatch.setattr(m, "systemctl", _manager(lambda u, n: values[n](u), actions))
    if fault == "non-primary-checkout":
        monkeypatch.setattr(m, "SCRIPT", tmp_path / ".worktrees/b/scripts/codebase_managed.py")
    if fault in ("none", "failed-idle"):
        m.repair_units(path)
        assert {name: (directory / name).read_text() for name in current} == current
        assert actions == [("daemon-reload",)]
    else:
        with pytest.raises(ValueError):
            m.repair_units(path)
        assert {name: (directory / name).read_text() for name in current} == before
        assert actions == []


def test_repair_units_command_is_wired(tmp_path, config, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / "settings.json"
    seen = []
    monkeypatch.setattr(m, "repair_units", lambda p: seen.append(p))
    monkeypatch.setattr(
        m.sys, "argv", ["codebase_managed.py", "--config", str(path), "repair-units"]
    )
    assert m.main() == 0
    assert seen == [m.config_path(str(path))]


@pytest.mark.parametrize("fail_at", ["config-set", "verify-cache", "daemon-reload", "settings"])
def test_failed_configure_leaves_nothing_that_selects_or_blocks(tmp_path, monkeypatch, fail_at):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEBASE_MEMORY_MCP_MANAGED_CONFIG", raising=False)
    main = (tmp_path / "repo").resolve()
    (main / ".git").mkdir(parents=True)
    monkeypatch.setattr(m, "SCRIPT", main / "scripts/codebase_managed.py", raising=False)
    provider = tmp_path / "provider"
    provider.write_bytes(b"accepted build")
    path = tmp_path / ".genesis/config/codebase-managed.json"
    state = tmp_path / "state"
    unit_dir = tmp_path / ".config/systemd/user"
    failing = {"active": True}

    def inject(stage):
        if failing["active"] and stage == fail_at:
            raise subprocess.CalledProcessError(1, stage)

    monkeypatch.setattr(m, "verified_binary", lambda p: open(p, "rb"))  # noqa: SIM115 - caller closes
    monkeypatch.setattr(m, "unit_available", lambda unit: True)
    monkeypatch.setattr(
        m.subprocess,
        "run",
        lambda *a, **k: inject("config-set") or subprocess.CompletedProcess(a, 0),
    )
    monkeypatch.setattr(m, "verify_cache", lambda config: inject("verify-cache"))
    real_write = m.write_settings

    def write_settings(target, value):
        if target == path:
            inject("settings")
        real_write(target, value)

    monkeypatch.setattr(m, "write_settings", write_settings)
    monkeypatch.setattr(m, "systemctl", lambda *a: inject(a[0]) or "")
    args = argparse.Namespace(
        main=str(main),
        binary=str(provider),
        state=str(state),
        sentinel=str(tmp_path / "disabled"),
        name="genesis-cbm-test",
    )
    with pytest.raises(subprocess.CalledProcessError):
        m.configure(args, path)
    assert not state.exists()
    assert not path.exists() and not path.is_symlink()
    assert sorted(p.name for p in unit_dir.iterdir()) == [".genesis-codebase-config.lock"]
    assert _selected(path, tmp_path) == 1  # the launcher would run raw, not refuse
    failing["active"] = False
    m.configure(args, path)  # the identical retry is not blocked
    assert m.read_settings(path)["enabled"] is False
    assert _selected(path, tmp_path) == 0


def test_configure_rollback_preserves_foreign_directory_at_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    main = (tmp_path / "repo").resolve()
    (main / ".git").mkdir(parents=True)
    monkeypatch.setattr(m, "SCRIPT", main / "scripts/codebase_managed.py", raising=False)
    provider = tmp_path / "provider"
    provider.write_bytes(b"accepted build")
    path = tmp_path / ".genesis/config/codebase-managed.json"
    monkeypatch.setattr(m, "verified_binary", lambda p: open(p, "rb"))  # noqa: SIM115
    monkeypatch.setattr(m, "unit_available", lambda unit: True)
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0))
    monkeypatch.setattr(m, "verify_cache", lambda config: None)
    monkeypatch.setattr(m, "systemctl", lambda *a: "")

    def racing_writer(target, value):
        target.mkdir()  # another actor wins the race for the settings path
        (target / "theirs").write_text("keep")
        raise IsADirectoryError(str(target))

    monkeypatch.setattr(m, "write_settings", racing_writer)
    args = argparse.Namespace(
        main=str(main), binary=str(provider), state=str(tmp_path / "state"),
        sentinel=str(tmp_path / "disabled"), name="genesis-cbm-test",
    )
    with pytest.raises(IsADirectoryError):
        m.configure(args, path)
    assert (path / "theirs").read_text() == "keep"
    assert not (tmp_path / "state").exists()


def test_configure_refuses_script_outside_primary_checkout(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    main = (tmp_path / "repo").resolve()
    (main / ".git").mkdir(parents=True)
    worktree_script = main / ".worktrees/branch/scripts/codebase_managed.py"
    monkeypatch.setattr(m, "SCRIPT", worktree_script, raising=False)
    monkeypatch.setattr(m, "verified_binary", lambda *a: pytest.fail("must refuse before pinning"))
    monkeypatch.setattr(m, "systemctl", lambda *a: pytest.fail("must refuse before manager"))
    args = argparse.Namespace(
        main=str(main),
        binary=str(tmp_path / "provider"),
        state=str(tmp_path / "state"),
        sentinel=str(tmp_path / "disabled"),
        name="genesis-cbm-test",
    )
    with pytest.raises(ValueError, match="primary checkout"):
        m.configure(args, tmp_path / "settings.json")
    assert not (tmp_path / "state").exists()
    assert not (tmp_path / ".config").exists()


@pytest.mark.parametrize("fault", ["none", "version", "memory", "swap"])
def test_check_backend_requires_exact_memory_and_zero_swap(tmp_path, config, monkeypatch, fault):
    leaf = tmp_path / "leaf"
    leaf.mkdir()
    (leaf / "memory.max").write_text(str(m.GIB) if fault == "memory" else str(2 * m.GIB))
    (leaf / "memory.swap.max").write_text("max" if fault == "swap" else "0")
    monkeypatch.setattr(
        m, "systemctl", lambda *a: "active" if a[3] == "ActiveState" else str(os.getpid())
    )
    monkeypatch.setattr(m.os.path, "samefile", lambda *a: True)
    monkeypatch.setattr(
        m, "resolve_cgroup", lambda *a: (leaf, tmp_path, 1 if fault == "version" else 2)
    )
    # This test process is not in the managed service, so a passing cap check
    # reaches (and fails) the following membership check instead.
    expected = "escaped service" if fault == "none" else "memory/zero-swap cap"
    with pytest.raises(ValueError, match=expected):
        m.check_backend(config)


def test_config_path_canonicalises_directory_not_final_link(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "alias").symlink_to(real)
    (real / "settings.json").symlink_to(tmp_path / "elsewhere.json")
    resolved = m.config_path(str(tmp_path / "alias" / "settings.json"))
    assert resolved == real.resolve() / "settings.json"
    assert resolved.is_symlink()  # still refused by write_settings, never followed
    with pytest.raises(ValueError):
        m.config_path("relative.json")


def test_config_path_symlink_loop_is_a_refusal_not_a_crash(tmp_path):
    (tmp_path / "loop").symlink_to(tmp_path / "loop")
    with pytest.raises(ValueError, match="unresolvable"):
        m.config_path(str(tmp_path / "loop" / "settings.json"))


def test_units_pin_configured_checkout_independent_of_caller(tmp_path, config, monkeypatch):
    path = tmp_path / "settings.json"
    expected = m.render_units(config, path)
    monkeypatch.setattr(m, "SCRIPT", tmp_path / ".worktrees/b/scripts/codebase_managed.py")
    service = m.render_units(config, path)[m.backend(config)]
    assert m.render_units(config, path) == expected
    script = str(Path(config["main"]) / "scripts/codebase_managed.py")
    assert f'ExecStart=:"/usr/bin/python3" "-I" "{script}" ' in service
    assert ".worktrees" not in service


def test_empty_override_means_default_settings(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEBASE_MEMORY_MCP_MANAGED_CONFIG", "")
    monkeypatch.setattr(m.sys, "argv", ["codebase_managed.py", "status"])
    assert m.main() == 1
    assert (
        str(tmp_path.resolve() / ".genesis/config/codebase-managed.json") in capsys.readouterr().err
    )


@pytest.mark.parametrize(
    "evidence,expected",
    [
        ("none", 1),
        ("override", 0),
        ("settings", 0),
        ("dangling-settings", 0),
        ("owned-unit", 0),
        ("owned-slice", 0),
        ("unit-via-alias", 0),
        ("foreign-unit", 1),
        ("other-config-unit", 3),
        ("unreadable-unit", 2),
        ("unit-dir-unlistable", 2),
        ("unit-dir-closed", 2),
        ("config-dir-closed", 2),
        ("settings-dir-closed", 2),
        ("settings-dir-dangling-link", 2),
        ("crlf-unit", 1),
    ],
)
def test_shell_selection_derivation(tmp_path, config, evidence, expected):
    home = tmp_path / "home"
    home.mkdir()
    path = m.config_path(str(home / ".genesis/config/codebase-managed.json"))
    directory = home / ".config/systemd/user"
    directory.mkdir(parents=True)
    units = m.render_units(config, path)
    service, slice_unit = m.backend(config), config["name"] + "-clients.slice"
    env = {k: v for k, v in os.environ.items() if k != "CODEBASE_MEMORY_MCP_MANAGED_CONFIG"}
    queried = path
    if evidence == "override":
        env["CODEBASE_MEMORY_MCP_MANAGED_CONFIG"] = str(path)
    if evidence in ("settings", "dangling-settings"):
        path.parent.mkdir(parents=True)
        if evidence == "settings":
            path.write_text("{}")
        else:
            path.symlink_to(tmp_path / "missing")
    if evidence == "owned-unit":
        (directory / service).write_text(units[service])
    if evidence == "owned-slice":
        (directory / slice_unit).write_text(units[slice_unit])
    if evidence == "unit-via-alias":
        (directory / service).write_text(units[service])
        (tmp_path / "alias").symlink_to(home)
        queried = tmp_path / "alias/.genesis/config/codebase-managed.json"
    if evidence == "foreign-unit":
        (directory / "genesis-cbm-vendor.service").write_text("[Service]\nExecStart=/bin/true\n")
    if evidence == "other-config-unit":
        (directory / service).write_text(m.render_units(config, tmp_path / "other.json")[service])
    if evidence == "unreadable-unit":
        (directory / service).write_text(units[service])
        (directory / service).chmod(0)
        if os.access(directory / service, os.R_OK):
            pytest.skip("running with privileges that ignore file modes")
    if evidence == "crlf-unit":  # not a byte-exact generated fragment, for either reader
        (directory / service).write_bytes(units[service].replace("\n", "\r\n").encode())
        assert not m.owned_fragment(directory / service, path)
    closed = {
        "unit-dir-unlistable": (directory, 0o300),
        "unit-dir-closed": (directory, 0o000),
        "config-dir-closed": (home / ".config", 0o000),
        "settings-dir-closed": (path.parent, 0o000),
    }.get(evidence)
    if evidence == "settings-dir-closed":  # settings present but unseeable
        path.parent.mkdir(parents=True)
        path.write_text("{}")
    if evidence == "settings-dir-dangling-link":  # e.g. a lost mount target
        (home / ".genesis").mkdir()
        (home / ".genesis/config").symlink_to(tmp_path / "unmounted/config")
    if closed:
        if evidence != "settings-dir-closed":
            (directory / service).write_text(units[service])  # owned evidence it cannot see
        closed[0].chmod(closed[1])
    try:
        if closed and os.access(closed[0], os.R_OK | os.X_OK):
            pytest.skip("running with privileges that ignore directory modes")
        result = _RUN(
            [
                "bash",
                "-c",
                '. "$0" && codebase_managed_selected "$1" "$2"',
                SELECTION_LIB,
                str(queried),
                str(home),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        if closed:
            closed[0].chmod(0o700)
    assert result.returncode == expected, result.stderr
    assert result.stdout == ""  # the launcher's stdout is the MCP transport


@pytest.mark.parametrize("suffix", [" ", "\t", "\\", '"', "%", "\u00a0"])
def test_native_start_preserves_exact_checkout_cwd(tmp_path, config, monkeypatch, suffix):
    import os

    directory = tmp_path / ("repo" + suffix)
    directory.mkdir()
    config["main"] = str(directory)
    provider = tmp_path / "provider"
    provider.write_bytes(b"test")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "verify_cache", lambda *a: None)
    monkeypatch.setattr(m, "verify_boundary", lambda *a: None)
    monkeypatch.setattr(m, "check_backend", lambda *a: None)
    monkeypatch.setattr(m, "verified_binary", lambda *a: provider.open("rb"))
    captured = []
    monkeypatch.setattr(m.os, "execve", lambda *a: captured.append(os.getcwd()))
    m.execute_native(config, "client", "unused")
    assert captured == [str(directory)]
    assert (
        "WorkingDirectory=/\n" in m.render_units(config, tmp_path / "settings")[m.backend(config)]
    )


@pytest.mark.parametrize(
    "property_name",
    [
        "FragmentPath",
        "DropInPaths",
        "Transient",
        "ActiveState",
        "ControlGroup",
        "MemoryMax",
        "MemorySwapMax",
        "TasksMax",
        "CPUQuotaPerSecUSec",
        "none",
    ],
)
def test_only_unmodified_inactive_implicit_slice_is_available(monkeypatch, property_name):
    values = {
        "LoadState": "loaded",
        "FragmentPath": "",
        "DropInPaths": "",
        "Transient": "no",
        "ActiveState": "inactive",
        "ControlGroup": "",
        "MemoryMax": "infinity",
        "MemorySwapMax": "infinity",
        "TasksMax": "infinity",
        "CPUQuotaPerSecUSec": "infinity",
    }
    if property_name != "none":
        values[property_name] = "modified"
    monkeypatch.setattr(m, "systemctl", lambda *a: values[a[3]])
    assert m.unit_available("genesis-cbm-test-clients.slice") is (property_name == "none")
    assert m.unit_available("genesis-cbm-test.service") is False


@pytest.mark.parametrize(
    "active,cgroup,available",
    [("inactive", "", True), ("active", "/owned", False), ("inactive", "/retained", False)],
)
def test_notfound_unit_with_live_ownership_is_preserved(monkeypatch, active, cgroup, available):
    values = {"LoadState": "not-found", "ActiveState": active, "ControlGroup": cgroup}
    monkeypatch.setattr(m, "systemctl", lambda *a: values[a[3]])
    assert m.unit_available("genesis-cbm-test.service") is available


# ── round 4: fail-closed handling of filesystem, systemd and cgroup state ──


def _sentinel_state(tmp_path: Path, state: str) -> Path:
    base = tmp_path / "sentinel-home"
    base.mkdir()
    sentinel = base / "disabled"
    if state == "file":
        sentinel.write_text("incident")
    elif state == "dangling-link":
        sentinel.symlink_to(tmp_path / "missing-target")
    elif state == "unsearchable-parent":
        base.chmod(0o600)
    elif state == "dangling-ancestor":  # e.g. a lost mount target
        sentinel = base / "mount/disabled"
        (base / "mount").symlink_to(tmp_path / "unmounted")
    elif state == "file-ancestor":
        (base / "plain").write_text("not a directory")
        sentinel = base / "plain/disabled"
    elif state == "missing-ancestor":  # still a definite ENOENT
        sentinel = base / "absent/disabled"
    return sentinel


_SENTINEL_STATES = {
    "absent": False,
    "missing-ancestor": False,
    "file": True,
    "dangling-link": True,
    "unsearchable-parent": True,
    "dangling-ancestor": True,
    "file-ancestor": True,
}


@pytest.mark.parametrize("state", sorted(_SENTINEL_STATES))
def test_only_a_definitely_absent_sentinel_permits_execution(tmp_path, config, monkeypatch, state):
    """Codex P1 4176698887: an unreadable or dangling sentinel is armed, never absent."""
    sentinel = _sentinel_state(tmp_path, state)
    config["sentinel"] = str(sentinel)
    config["main"] = str(tmp_path.resolve())
    try:
        if state == "unsearchable-parent" and os.access(sentinel.parent, os.X_OK):
            pytest.skip("running with privileges that ignore directory modes")
        monkeypatch.setattr(m, "verify_cache", lambda *a: None)
        if _SENTINEL_STATES[state]:
            with pytest.raises(ValueError, match="disabled"):
                m.batch_values(config, config["main"])
        else:
            monkeypatch.setattr(m, "verified_binary", lambda *a: contextlib.nullcontext())
            assert m.batch_values(config, config["main"])[0] == config["binary"]
    finally:
        (tmp_path / "sentinel-home").chmod(0o700)


@pytest.mark.parametrize("state", sorted(_SENTINEL_STATES))
def test_shell_sentinel_check_matches_python(tmp_path, state):
    """One rule in both languages (parity lock, not a verify-RED)."""
    sentinel = _sentinel_state(tmp_path, state)
    try:
        if state == "unsearchable-parent" and os.access(sentinel.parent, os.X_OK):
            pytest.skip("running with privileges that ignore directory modes")
        result = _RUN(
            ["bash", "-c", '. "$0" && codebase_managed_sentinel_armed "$1"', SELECTION_LIB,
             str(sentinel)],
            capture_output=True, text=True, timeout=30,
        )
        python = m.sentinel_armed(str(sentinel))
    finally:
        (tmp_path / "sentinel-home").chmod(0o700)
    assert result.returncode in (0, 1), result.stderr
    assert (result.returncode == 0) is python is _SENTINEL_STATES[state]


def test_settings_symlink_refused_when_read(tmp_path, config):
    """Codex 4176698871: a link to a valid copy is refused on read, as on write."""
    real = tmp_path / "real.json"
    real.write_text(json.dumps(config))
    link = tmp_path / "settings.json"
    link.symlink_to(real)
    with pytest.raises(ValueError, match="symlink"):
        m.read_settings(link)


def test_settings_write_never_clobbers_a_concurrently_created_file(tmp_path, config):
    """Codex 4176698875: the initial publish is no-clobber."""
    path = tmp_path / "settings.json"
    path.write_text("foreign")
    with pytest.raises(OSError):
        m.write_settings(path, config)
    assert path.read_text() == "foreign"


def test_configure_preserves_settings_created_during_configure(tmp_path, monkeypatch):
    """The rollback must not delete a settings file another process created."""
    monkeypatch.setenv("HOME", str(tmp_path))
    main = (tmp_path / "repo").resolve()
    (main / ".git").mkdir(parents=True)
    monkeypatch.setattr(m, "SCRIPT", main / "scripts/codebase_managed.py", raising=False)
    provider = tmp_path / "provider"
    provider.write_bytes(b"accepted build")
    path = tmp_path / ".genesis/config/codebase-managed.json"
    monkeypatch.setattr(m, "verified_binary", lambda p: open(p, "rb"))  # noqa: SIM115
    monkeypatch.setattr(m, "unit_available", lambda unit: True)
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0))
    monkeypatch.setattr(m, "verify_cache", lambda config: None)

    def manager(*args):
        if args[0] == "daemon-reload" and not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("theirs")  # another actor wins after the absence check
        return ""

    monkeypatch.setattr(m, "systemctl", manager)
    args = argparse.Namespace(
        main=str(main), binary=str(provider), state=str(tmp_path / "state"),
        sentinel=str(tmp_path / "disabled"), name="genesis-cbm-test",
    )
    with pytest.raises(OSError):
        m.configure(args, path)
    assert path.read_text() == "theirs"
    assert not (tmp_path / "state").exists()


def test_repair_refuses_fragment_replaced_after_verification(tmp_path, config, monkeypatch):
    """Codex 4176698875: repair replaces only the inode it verified."""
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(config))
    directory = _write_units(config, path, tmp_path)
    for name, body in m.render_units(config, path).items():
        (directory / name).write_text(_stale(body))
    fragment = directory / m.backend(config)
    replacement = m.MARKER + m.CONFIG_LINE + str(path) + "\n[Service]\nExecStart=/bin/theirs\n"
    slice_unit = config["name"] + "-clients.slice"

    def fragment_path(unit):
        if unit == slice_unit:  # the last verification: swap the backend after its check
            staged = directory / "swap.tmp"
            staged.write_text(replacement)
            os.replace(staged, fragment)
        return str(directory / unit)

    values = {
        "ActiveState": lambda unit: "inactive",
        "ControlGroup": lambda unit: "",
        "FragmentPath": fragment_path,
    }
    actions = []
    monkeypatch.setattr(m, "systemctl", _manager(lambda u, n: values[n](u), actions))
    with pytest.raises(ValueError, match="changed"):
        m.repair_units(path)
    assert fragment.read_text() == replacement
    assert actions == []


@pytest.mark.parametrize(
    "fault", ["start-oserror", "start-error-write-fails", "start-error-write-and-stop-fail"]
)
def test_activation_rollback_attempts_each_step_and_keeps_start_error(
    tmp_path, config, monkeypatch, capsys, fault
):
    """Codex 4176698876: reset and stop are independent; the start error surfaces."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".config/systemd/user").mkdir(parents=True)
    path = tmp_path / "settings.json"
    config["enabled"] = False
    path.write_text(json.dumps(config))
    monkeypatch.setattr(m, "verify_cache", lambda *a: None)
    monkeypatch.setattr(m, "verify_units", lambda *a, **k: None)
    monkeypatch.setattr(m, "verified_binary", lambda *a: contextlib.nullcontext())
    real_write = m.write_settings
    writes = []

    def write_settings(target, value, *rest):
        writes.append(value["enabled"])
        if value["enabled"] is False and fault != "start-oserror":
            raise OSError("settings filesystem is read-only")
        return real_write(target, value, *rest)

    monkeypatch.setattr(m, "write_settings", write_settings)
    actions = []

    def manager(*args):
        actions.append(args)
        if args[0] == "start":
            if fault == "start-oserror":
                raise PermissionError("systemctl became inaccessible")
            raise subprocess.CalledProcessError(1, "start")
        if args[0] == "stop" and fault == "start-error-write-and-stop-fail":
            raise subprocess.CalledProcessError(2, "stop")
        return ""

    monkeypatch.setattr(m, "systemctl", manager)
    expected = PermissionError if fault == "start-oserror" else subprocess.CalledProcessError
    with pytest.raises(expected) as error:
        m.set_enabled(config, path, True)
    if expected is subprocess.CalledProcessError:
        assert error.value.cmd == "start"
    assert writes == [True, False]
    assert actions == [
        ("start", "genesis-cbm-query.service"),
        ("stop", "genesis-cbm-query.service"),
    ]
    if fault == "start-oserror":
        assert m.read_settings(path)["enabled"] is False
    else:
        assert "read-only" in capsys.readouterr().err


def test_client_ancestors_must_admit_the_aggregate_budget(tmp_path, config, monkeypatch):
    """Codex 4176698894: an ancestor below 2 GiB caps the whole client slice.

    cgroup v2 limits may be over-committed: "the sum of the limits of children
    can exceed the amount of resource available to the parent" (kernel
    admin-guide cgroup-v2, Resource Distribution Models / Limits).
    """
    unit = config["name"] + "-client-" + "a" * 32 + ".service"
    parent = tmp_path / "user.slice" / (config["name"] + "-clients.slice")
    leaf = parent / unit
    leaf.mkdir(parents=True)
    for directory, cap in (
        (tmp_path, "max"),
        (tmp_path / "user.slice", str(m.GIB)),  # admits one client, not the aggregate
        (parent, str(2 * m.GIB)),
        (leaf, str(m.GIB // 4)),
    ):
        (directory / "memory.max").write_text(cap)
        (directory / "memory.swap.max").write_text("0")
    monkeypatch.setattr(m, "resolve_cgroup", lambda *a: (leaf, tmp_path, 2))
    with pytest.raises(ValueError, match="ancestor"):
        m.verify_boundary(config, "client", unit)


@pytest.mark.parametrize("fault", ["old-template", "drop-in", "foreign"])
def test_launch_tolerates_template_drift_but_not_foreign_units(tmp_path, config, monkeypatch, fault):
    """A1: a template change must not take the MCP down; ownership still holds."""
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / "settings.json"
    directory = _write_units(config, path, tmp_path)
    fragment = directory / m.backend(config)
    fragment.write_text(_stale(fragment.read_text()))
    values = {
        "FragmentPath": lambda unit: "/foreign/unit" if fault == "foreign" else str(directory / unit),
        "DropInPaths": lambda unit: "/foreign/drop.conf" if fault == "drop-in" else "",
        "NeedDaemonReload": lambda unit: "no",
    }
    monkeypatch.setattr(m, "systemctl", _manager(lambda u, n: values[n](u), []))
    monkeypatch.setattr(m, "verify_cache", lambda *a: None)
    monkeypatch.setattr(m, "check_backend", lambda *a: None)
    launched = []
    monkeypatch.setattr(m.os, "execv", lambda _, args: launched.append(args))
    if fault == "old-template":
        m.launch(config, path)
        assert launched
    else:
        with pytest.raises(ValueError):
            m.launch(config, path)
        assert not launched


def test_status_reports_template_drift(tmp_path, config, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(config))
    directory = _write_units(config, path, tmp_path)
    fragment = directory / m.backend(config)
    fragment.write_text(_stale(fragment.read_text()))
    monkeypatch.setattr(m, "systemctl", lambda *a: "inactive")
    monkeypatch.setattr(m.sys, "argv", ["codebase_managed.py", "--config", str(path), "status"])
    assert m.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["units"][m.backend(config)].startswith("drift")
    assert report["units"][config["name"] + "-clients.slice"] == "current"


def _remove(path: Path) -> int:
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(m.sys, "argv", ["codebase_managed.py", "--config", str(path), "remove"])
        return m.main()


@pytest.mark.parametrize(
    "fault",
    ["none", "stale-build", "half-removed", "foreign", "settings-only", "units-still-loaded"],
)
def test_remove_retires_only_owned_units_and_the_settings(tmp_path, config, monkeypatch, fault):
    """A2: after remove, selection derivation reports a never-configured install."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEBASE_MEMORY_MCP_MANAGED_CONFIG", raising=False)
    path = m.config_path(str(tmp_path / ".genesis/config/codebase-managed.json"))
    path.parent.mkdir(parents=True)
    directory = _write_units(config, path, tmp_path)
    if fault == "stale-build":  # settings from an older accepted build can still retire
        config["build"] = "0" * 64
    path.write_text(json.dumps(config))
    slice_unit = config["name"] + "-clients.slice"
    if fault == "foreign":
        (directory / slice_unit).write_text("[Slice]\nMemoryMax=8G\n")
    if fault == "half-removed":  # an earlier remove stopped before its second unlink
        (directory / m.backend(config)).unlink()
    if fault in ("settings-only", "units-still-loaded"):
        for unit in m.render_units(config, path):
            (directory / unit).unlink()
    loaded = {
        "FragmentPath": lambda unit: str(directory / unit),
        "DropInPaths": lambda unit: "",
        "NeedDaemonReload": lambda unit: "no",
        "LoadState": lambda unit: "not-found",
        "ActiveState": lambda unit: "active" if fault == "units-still-loaded" else "inactive",
        "ControlGroup": lambda unit: "/owned" if fault == "units-still-loaded" else "",
    }
    actions = []
    monkeypatch.setattr(m, "systemctl", _manager(lambda u, n: loaded[n](u), actions))
    before = {p.name: p.read_text() for p in directory.glob("genesis-cbm-*")}
    result = _remove(path)
    if fault in ("none", "stale-build"):
        assert result == 0
        assert actions == [
            ("disable", m.backend(config)),
            ("stop", m.backend(config), slice_unit),
            ("daemon-reload",),
        ]
    elif fault == "half-removed":
        assert result == 0
        assert actions == [("daemon-reload",), ("stop", slice_unit), ("daemon-reload",)]
    elif fault == "settings-only":
        assert result == 0 and actions == [("daemon-reload",)]
    else:
        assert result == 1
        assert actions == ([("daemon-reload",)] if fault == "units-still-loaded" else [])
        assert {p.name: p.read_text() for p in directory.glob("genesis-cbm-*")} == before
        assert m.read_settings(path)["enabled"] is False  # the stop lever still landed
        return
    assert not any(directory.glob("genesis-cbm-*"))
    assert not path.exists()
    assert _selected(path, tmp_path) == 1


def _uninstall_block(path: str) -> str:
    source = (ROOT / "scripts/uninstall.sh").read_text()
    if path == "direct":
        helper = source[source.index("safe_disable_service() {") : source.index("# Run a command inside")]
        start = source.index("        PRESSURE_UNIT=genesis-disk-hygiene-pressure")
        return helper + source[start : source.index("        # Persistent= timers", start)]
    helper = source[source.index("remove_serena_enablement() {") : source.index("# Run a command inside")]
    helper += 'container_exec() { bash -c "$1"; }\n'
    start = source.index('            container_exec "', source.index("# Stop all services (timers first"))
    return helper + source[start : source.index('            ok "Stopped Genesis services"', start)]


@pytest.mark.parametrize("path", ["direct", "host"])
def test_uninstall_stops_and_disables_managed_codebase_units(tmp_path, path):
    calls = tmp_path / "calls"
    fake = tmp_path / "bin/systemctl"
    fake.parent.mkdir()
    fake.write_text(f'#!/bin/sh\necho "$*" >> "{calls}"\n')
    fake.chmod(0o755)
    _RUN(
        ["bash", "-c", "DRY_RUN=false; ok() { :; }; skip() { :; };\n" + _uninstall_block(path)],
        env=dict(os.environ, HOME=str(tmp_path), XDG_RUNTIME_DIR=str(tmp_path / "run"),
                 PATH=f"{fake.parent}:{os.defpath}"),
        check=True, capture_output=True, text=True, timeout=60,
    )
    recorded = [line.split() for line in calls.read_text().splitlines()]
    for unit in ("genesis-cbm-query.service", "genesis-cbm-query-clients.slice"):
        for operation in ("stop", "disable"):
            assert any(args[:2] == ["--user", operation] and unit in args[2:] for args in recorded)


@pytest.mark.parametrize("path", ["direct", "host"])
def test_uninstall_deletes_managed_codebase_fragments(tmp_path, path):
    """Default and custom unit names: no owned fragment survives to re-select."""
    source = (ROOT / "scripts/uninstall.sh").read_text()
    if path == "direct":
        start = source.index("        # Remove systemd unit files\n        SYSTEMD_DIR")
        end = source.index("\n", source.index("daemon-reload", start))
        block = source[source.index("safe_remove() {") : source.index("# Stop and disable a systemd")]
        block += source[start:end]
    else:
        start = source.index("            # Remove systemd unit files\n")
        block = 'container_exec() { bash -c "$1"; }\n'
        block += source[start : source.index('            ok "Removed systemd unit files"', start)]
    fake = tmp_path / "bin/systemctl"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    directory = tmp_path / ".config/systemd/user"
    directory.mkdir(parents=True)
    names = [
        "genesis-cbm-query.service", "genesis-cbm-query-clients.slice",
        "genesis-cbm-other.service", "genesis-cbm-other-clients.slice",
    ]
    for name in names:
        (directory / name).write_text(m.MARKER)
    _RUN(
        ["bash", "-c", "DRY_RUN=false; REMOVED=(); ok() { :; }; skip() { :; };\n" + block],
        env=dict(os.environ, HOME=str(tmp_path), PATH=f"{fake.parent}:{os.defpath}"),
        check=True, capture_output=True, text=True, timeout=60,
    )
    assert not [name for name in names if (directory / name).exists()]
