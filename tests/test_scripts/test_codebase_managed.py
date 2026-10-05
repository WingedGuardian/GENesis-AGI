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


# Bound at import: tests patch m.subprocess.run, which is this same module.
_RUN = subprocess.run


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
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("version", 2),
        ("enabled", "true"),
        ("build", "wrong"),
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




def test_batch_other_repository_refused_before_provider(config, tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setattr(m, "verify_cache", lambda *a: pytest.fail("must not open cache"))
    with pytest.raises(ValueError, match="main checkout"):
        m.batch_values(config, str(other))




def test_dollar_paths_reach_both_launch_paths_literally(tmp_path, config, monkeypatch):
    """Codex 4178812470: systemd-run expands ${VAR} unless told not to.

    The unit's Exec lines need no "$$": their ":" prefix disables substitution
    (v255 systemd.service), so a "$$" there would reach the program doubled.
    """
    config["main"] = str(tmp_path / "${HOME}repo")
    for attr in ("require_enabled", "verify_cache", "check_backend"):
        monkeypatch.setattr(m, attr, lambda *a, **k: None)
    captured = []
    monkeypatch.setattr(m.os, "execv", lambda _, args: captured.extend(args))
    m.launch(config, tmp_path / "$x.json")
    assert "--expand-environment=no" in captured[: captured.index("--")]
    assert str(tmp_path / "$x.json") in captured[captured.index("--") :]


@pytest.mark.parametrize("conflict", ["dangling-settings", "dangling-state"])
def test_configure_preserves_foreign_artifacts_before_pin_or_state(tmp_path, monkeypatch, conflict):
    monkeypatch.setenv("HOME", str(tmp_path))
    main = tmp_path / "repo"
    (main / ".git").mkdir(parents=True)
    monkeypatch.setattr(m, "SCRIPT", main.resolve() / "scripts/codebase_managed.py", raising=False)
    path = tmp_path / ".genesis/config/codebase-managed.json"
    path.parent.mkdir(parents=True)
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
    monkeypatch.setattr(m, "verified_binary", lambda *a: contextlib.nullcontext())
    monkeypatch.setattr(m, "verify_client_slice", lambda: None)
    actions = []

    def manager(*args):
        actions.append(args)
        if args[0] == "enable":
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
    assert actions[:3] == [
        ("enable", "--now", "genesis-cbm-query.service"),
        ("disable", "--now", "genesis-cbm-query.service"),
        ("stop", "genesis-cbm-query.service"),
    ]
    # A failed stop consults the manager read-only before keeping the error.
    assert all(a[0] == "show" for a in actions[3:])


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
    unit = m.NAME + "-client-" + "a" * 32 + ".service"
    parent = tmp_path / (m.SLICE)
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


















@pytest.mark.parametrize("fail_at", ["config-set", "verify-cache", "settings"])
def test_failed_configure_retains_staging_without_unsafe_rollback(tmp_path, monkeypatch, fail_at):
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
    )
    with pytest.raises(subprocess.CalledProcessError):
        m.configure(args, path)
    assert state.is_dir()
    assert not path.exists() and not path.is_symlink()
    before = {p.name: p.read_bytes() for p in unit_dir.glob("genesis-cbm-*")}
    failing["active"] = False
    with pytest.raises(ValueError, match="preserved"):
        m.configure(args, path)  # retained staging is never silently reclaimed
    assert {p.name: p.read_bytes() for p in unit_dir.glob("genesis-cbm-*")} == before


def test_configure_rollback_preserves_foreign_directory_at_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    main = (tmp_path / "repo").resolve()
    (main / ".git").mkdir(parents=True)
    monkeypatch.setattr(m, "SCRIPT", main / "scripts/codebase_managed.py", raising=False)
    provider = tmp_path / "provider"
    provider.write_bytes(b"accepted build")
    path = tmp_path / ".genesis/config/codebase-managed.json"
    monkeypatch.setattr(m, "verified_binary", lambda p: open(p, "rb"))  # noqa: SIM115
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
        sentinel=str(tmp_path / "disabled"),
    )
    with pytest.raises(IsADirectoryError):
        m.configure(args, path)
    assert (path / "theirs").read_text() == "keep"
    assert (tmp_path / "state").is_dir()


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




def test_empty_override_means_default_settings(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEBASE_MEMORY_MCP_MANAGED_CONFIG", "")
    monkeypatch.setattr(m.sys, "argv", ["codebase_managed.py", "status"])
    assert m.main() == 1
    assert (
        str(tmp_path.resolve() / ".genesis/config/codebase-managed.json") in capsys.readouterr().err
    )




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
        "WorkingDirectory=/\n" in (ROOT / "scripts/systemd/genesis-cbm-query.service.template").read_text()
    )






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
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0))
    monkeypatch.setattr(m, "verify_cache", lambda config: None)

    def race(config):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("theirs")
    monkeypatch.setattr(m, "verify_cache", race)
    args = argparse.Namespace(
        main=str(main), binary=str(provider), state=str(tmp_path / "state"),
        sentinel=str(tmp_path / "disabled"),
    )
    with pytest.raises(OSError):
        m.configure(args, path)
    assert path.read_text() == "theirs"
    assert (tmp_path / "state").is_dir()


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
    monkeypatch.setattr(m, "verified_binary", lambda *a: contextlib.nullcontext())
    monkeypatch.setattr(m, "verify_client_slice", lambda: None)
    real_write = m.write_settings
    writes = []

    def write_settings(target, value, **kw):
        writes.append(value["enabled"])
        if value["enabled"] is False and fault != "start-oserror":
            raise OSError("settings filesystem is read-only")
        return real_write(target, value, **kw)

    monkeypatch.setattr(m, "write_settings", write_settings)
    actions = []

    def manager(*args):
        actions.append(args)
        if args[0] == "enable":
            if fault == "start-oserror":
                raise PermissionError("systemctl became inaccessible")
            raise subprocess.CalledProcessError(1, "start")
        if args[0] == "disable" and fault == "start-error-write-and-stop-fail":
            raise subprocess.CalledProcessError(2, "stop")
        return ""

    monkeypatch.setattr(m, "systemctl", manager)
    expected = PermissionError if fault == "start-oserror" else subprocess.CalledProcessError
    with pytest.raises(expected) as error:
        m.set_enabled(config, path, True)
    if expected is subprocess.CalledProcessError:
        assert error.value.cmd == "start"
    assert writes == [True, False]
    assert actions[:3] == [
        ("enable", "--now", "genesis-cbm-query.service"),
        ("disable", "--now", "genesis-cbm-query.service"),
        ("stop", "genesis-cbm-query.service"),
    ]
    assert all(a[0] == "show" for a in actions[3:])
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
    unit = m.NAME + "-client-" + "a" * 32 + ".service"
    parent = tmp_path / "user.slice" / (m.SLICE)
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






@pytest.mark.parametrize("command", ["disable", "status"])
def test_stale_build_still_reaches_disable_and_status(tmp_path, config, monkeypatch, command):
    """Codex 4178812457: an upgrade changes BUILD; the stop lever and status still work."""
    monkeypatch.setenv("HOME", str(tmp_path))
    config["build"] = "0" * 64
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(config))
    called = []
    monkeypatch.setattr(m, "disable", lambda p: called.append(False))
    monkeypatch.setattr(m, "systemctl", lambda *a: "inactive")
    monkeypatch.setattr(m.sys, "argv", ["codebase_managed.py", "--config", str(path), command])
    assert m.main() == 0
    assert called == ([False] if command == "disable" else [])


@pytest.mark.parametrize("command", ["launch", "enable", "batch"])
def test_stale_build_still_refuses_running_commands(tmp_path, config, monkeypatch, command, capsys):
    config["build"] = "0" * 64
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(config))
    for attr in ("launch", "set_enabled", "batch_values"):
        monkeypatch.setattr(m, attr, lambda *a, **k: pytest.fail("ran with a stale build"))
    extra = ["--repo", str(tmp_path)] if command == "batch" else []
    argv = ["codebase_managed.py", "--config", str(path), command, *extra]
    monkeypatch.setattr(m.sys, "argv", argv)
    assert m.main() == 1
    assert "unsupported managed build" in capsys.readouterr().err


def test_uninstall_retains_managed_fragments_and_application_roots(tmp_path):
    source = (ROOT / "scripts/uninstall.sh").read_text()
    helpers = source[source.index("safe_remove() {"):source.index("# Stop and disable a systemd")]
    start = source.index("        # Remove systemd unit files\n        SYSTEMD_DIR")
    end = source.index("\n", source.index("daemon-reload", start))
    directory = tmp_path / ".config/systemd/user"
    directory.mkdir(parents=True)
    for name in ("genesis-cbm-query.service", "genesis-cbm-custom.service", "genesis-cbm-custom-clients.slice"):
        (directory / name).write_text("owned-or-foreign-preserve")
    (tmp_path / ".genesis").mkdir()
    (tmp_path / ".genesis/keep").write_text("managed cache")
    fake = tmp_path / "bin/systemctl"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    block = 'DRY_RUN=false; MANAGED_STATE_RETAIN=true; REMOVED=(); KEPT=(); info() { :; }; ok() { :; }; skip() { :; };\n'
    block += helpers + source[start:end] + '\nsafe_remove "$HOME/.genesis" state\n'
    _RUN(["bash", "-c", block], env=dict(os.environ, HOME=str(tmp_path), PATH=f"{fake.parent}:{os.defpath}"), check=True)
    assert len(list(directory.glob("genesis-cbm-*"))) == 3
    assert (tmp_path / ".genesis/keep").read_text() == "managed cache"


def test_host_uninstall_shares_only_managed_lifecycle_helpers():
    source = (ROOT / "scripts/uninstall.sh").read_text()
    assert "$(declare -f info managed_codebase_retention managed_codebase_preflight)" in source
    assert "$(declare -f info ok skip safe_remove managed_codebase_retention)" in source
    assert 'if [ "$strict" = true ]; then' in source
    assert 'managed_codebase_preflight\n            " true ||' in source


def test_host_uninstall_retains_managed_fragments_and_roots_using_real_commands(tmp_path):
    source = (ROOT / "scripts/uninstall.sh").read_text()
    helpers = source[source.index("safe_remove() {"):source.index("# Stop and disable a systemd")]
    host = source.index("# Stop all services (timers first")
    start = source.index('            # Remove systemd unit files', host)
    end = source.index('            ok "Removed systemd unit files"', start)
    unit_commands = source[start:end]
    start = source.index('            # Remove Genesis directories and data', host)
    end = source.index('            ok "Genesis directory cleanup completed', start)
    root_commands = source[start:end]
    directory = tmp_path / ".config/systemd/user"
    directory.mkdir(parents=True)
    for name in ("genesis-cbm-query.service", "genesis-cbm-custom.service", "genesis-cbm-custom-clients.slice"):
        (directory / name).write_text("preserve")
    ordinary = directory / "genesis-server.service"
    ordinary.write_text("remove")
    for name in ("genesis", ".genesis", "data", ".qdrant"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "keep").write_text("managed cache")
    fake = tmp_path / "bin/systemctl"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    block = 'DRY_RUN=false; REMOVED=(); SKIPPED=(); KEPT=(); info() { :; }; ok() { :; }; skip() { :; }; container_exec() { bash -e -c "$1"; };\n'
    block += helpers + unit_commands + root_commands
    _RUN(["bash", "-e", "-c", block], env=dict(os.environ, HOME=str(tmp_path), PATH=f"{fake.parent}:{os.defpath}"), check=True)
    assert not ordinary.exists()
    assert len(list(directory.glob("genesis-cbm-*"))) == 3
    assert all((tmp_path / name / "keep").read_text() == "managed cache" for name in ("genesis", ".genesis", "data", ".qdrant"))
