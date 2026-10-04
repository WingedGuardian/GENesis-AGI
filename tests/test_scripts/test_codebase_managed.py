"""Native integration boundaries: malformed settings never select raw fallback."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "codebase_managed", ROOT / "scripts/codebase_managed.py"
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


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
    monkeypatch.setattr(m, "require_enabled", lambda *a: None)
    monkeypatch.setattr(m, "verify_cache", lambda *a: None)
    monkeypatch.setattr(m, "check_backend", lambda *a: None)
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


@pytest.mark.parametrize("conflict", ["dangling-settings", "loaded-unit"])
def test_configure_preserves_foreign_artifacts_before_pin_or_state(tmp_path, monkeypatch, conflict):
    import argparse

    monkeypatch.setenv("HOME", str(tmp_path))
    main = tmp_path / "repo"
    (main / ".git").mkdir(parents=True)
    path = tmp_path / "settings.json"
    foreign = tmp_path / "foreign-target"
    if conflict == "dangling-settings":
        path.symlink_to(foreign)
    state = tmp_path / "state"
    monkeypatch.setattr(
        m, "verified_binary", lambda *a: pytest.fail("must preserve before opening binary")
    )
    monkeypatch.setattr(m, "systemctl", lambda *a: "loaded")
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


def test_activation_start_failure_rolls_back_and_stops_owned_unit(tmp_path, config, monkeypatch):
    import contextlib
    import subprocess

    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".config/systemd/user").mkdir(parents=True)
    path = tmp_path / "settings.json"
    config["enabled"] = False
    path.write_text(json.dumps(config))
    monkeypatch.setattr(m, "verify_cache", lambda *a: None)
    monkeypatch.setattr(m, "verified_binary", lambda *a: contextlib.nullcontext())
    actions = []

    def manager(*args):
        actions.append(args)
        if args[0] == "start":
            raise subprocess.CalledProcessError(1, "start")
        return ""

    monkeypatch.setattr(m, "systemctl", manager)
    with pytest.raises(subprocess.CalledProcessError):
        m.set_enabled(config, path, True)
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
