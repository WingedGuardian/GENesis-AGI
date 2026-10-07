"""Native lifecycle validates before mutation and keeps retirement failures visible."""

from __future__ import annotations

import argparse
import copy
import json
import os
import sqlite3
import subprocess
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.test_scripts.test_codebase_managed_config import managed

operator = managed


@pytest.fixture
def loaded(operator, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("VENV_PATH", raising=False)
    config = {"main": str(tmp_path / 'repo $%λ" space ')}
    argv = ["/bin/sh", "-c", 'exec "$@"', "--", config["main"] + "/.venv/bin/python",
            "-I", config["main"] + "/scripts/codebase_managed.py", "--config",
            str(tmp_path / ".genesis/config/codebase-managed.json")]
    values = {"Type": "exec", "WorkingDirectory": "/", "MemoryMax": 2 * 1024**3,
              "MemorySwapMax": 0, "OOMScoreAdjust": 500, "KillMode": "control-group",
              "Restart": "no", "TasksMax": 128, "CPUQuotaPerSecUSec": 2000000,
              "TimeoutStartUSec": 120000000, "TimeoutStopUSec": 30000000}
    properties = {key: {"type": "s" if isinstance(value, str) else
                        "i" if key == "OOMScoreAdjust" else "t", "data": value}
                  for key, value in values.items()}
    for name, role in (("ExecStartEx", "serve"), ("ExecStartPostEx", "ready"),
                       ("ExecConditionEx", None), ("ExecStartPreEx", None),
                       ("ExecReloadEx", None), ("ExecStopEx", None), ("ExecStopPostEx", None)):
        properties[name] = {"type": "a(sasasttttuii)", "data":
                            [["/bin/sh", argv + [role], ["no-env-expand"], *([0] * 7)]]
                            if role else []}
    unit = {key: {"type": "s", "data": value}
            for key, value in {"LoadState": "loaded", "Id": operator.BACKEND}.items()}
    namespace = operator.validate_loaded_backend.__globals__["validate_backend"].__globals__
    monkeypatch.setitem(namespace, "loaded_properties",
                        lambda name, interface: unit if interface == "Unit" else properties)
    return config, properties, unit


@pytest.mark.parametrize("custom", [False, True])
def test_loaded_commands_preserve_literal_installed_paths(operator, loaded, monkeypatch, custom):
    config, properties, _ = loaded
    if custom:
        venv = '/custom $%λ" \\ venv//'
        monkeypatch.setenv("VENV_PATH", venv)
        for name in ("ExecStartEx", "ExecStartPostEx"):
            properties[name]["data"][0][1][4] = venv + "/bin/python"
    operator.validate_loaded_backend(config)


@pytest.mark.parametrize("name", ["Type", "WorkingDirectory", "MemoryMax", "MemorySwapMax",
                                  "OOMScoreAdjust", "KillMode", "Restart", "TasksMax",
                                  "CPUQuotaPerSecUSec", "TimeoutStartUSec", "TimeoutStopUSec"])
@pytest.mark.parametrize("fault", ["missing", "type", "value"])
def test_every_loaded_property_refuses_invalid_contract(operator, loaded, name, fault):
    config, properties, _ = loaded
    if fault == "missing":
        del properties[name]
    elif fault == "type":
        properties[name]["type"] = "b"
        properties[name]["data"] = True
    else:
        properties[name]["data"] = "wrong" if properties[name]["type"] == "s" else 2**64 - 1
    with pytest.raises(ValueError):
        operator.validate_loaded_backend(config)


@pytest.mark.parametrize("name", ["ExecStartEx", "ExecStartPostEx"])
@pytest.mark.parametrize("fault", ["empty", "multiple", "path", "argv", "flag", "extra-flag",
                                   "record", "metadata", *range(10)])
def test_each_required_command_rejects_boundary_or_flag_change(operator, loaded, name, fault):
    config, properties, _ = loaded
    commands = properties[name]["data"]
    record = commands[0]
    if fault == "empty":
        commands.clear()
    elif fault == "multiple":
        commands.append(copy.deepcopy(record))
    elif fault == "path":
        record[0] = "/bin/false"
    elif fault == "argv":
        record[1].append("extra")
    elif fault == "flag":
        record[2].clear()
    elif fault == "extra-flag":
        record[2].append("ignore-failure")
    elif fault == "record":
        record.pop()
    elif fault == "metadata":
        record[3] = True
    else:
        record[1][fault] += "changed"
    with pytest.raises(ValueError):
        operator.validate_loaded_backend(config)


@pytest.mark.parametrize("name", ["ExecConditionEx", "ExecStartPreEx", "ExecReloadEx",
                                  "ExecStopEx", "ExecStopPostEx"])
def test_auxiliary_loaded_commands_refuse_execution(operator, loaded, name):
    config, properties, _ = loaded
    properties[name]["data"] = copy.deepcopy(properties["ExecStartEx"]["data"])
    with pytest.raises(ValueError):
        operator.validate_loaded_backend(config)


@pytest.mark.parametrize("value", [True, -1, 2**64, "128", None])
def test_uint64_properties_reject_untyped_values(operator, loaded, value):
    config, properties, _ = loaded
    properties["TasksMax"]["data"] = value
    with pytest.raises(ValueError):
        operator.validate_loaded_backend(config)


@pytest.mark.parametrize("fault", ["json", "envelope", "path", "dictionary"])
def test_typed_manager_decoder_refuses_malformed_results(operator, monkeypatch, fault):
    path = {"type": "o", "data": ["/org/freedesktop/systemd1/unit/owned"]}
    values = [path, {"type": "a{sv}", "data": [{}]}]
    if fault == "json":
        responses = ["not json"]
    else:
        if fault == "envelope":
            values[0]["extra"] = "invalid"
        elif fault == "path":
            values[0]["data"] = ["/foreign"]
        else:
            values[1]["data"] = [[], {}]
        responses = [json.dumps(value) for value in values]
    namespace = operator.loaded_properties.__globals__
    monkeypatch.setattr(namespace["subprocess"], "run",
                        Mock(side_effect=[SimpleNamespace(stdout=value) for value in responses]))
    with pytest.raises(ValueError):
        operator.loaded_properties(operator.BACKEND, "Service")


@pytest.fixture
def startup(operator, monkeypatch):
    calls = []
    namespace = operator.enable.__globals__
    monkeypatch.setitem(namespace, "verify_cache", lambda config: calls.append("cache"))
    monkeypatch.setitem(namespace, "verified_binary", lambda path: nullcontext())
    monkeypatch.setitem(namespace, "sentinel_armed", lambda path: False)
    properties = dict(
        LoadState="loaded", MemoryMax=str(2 * 1024**3), MemorySwapMax="0", TasksMax="512"
    )
    monkeypatch.setitem(namespace, "show", lambda *args: properties)
    monkeypatch.setitem(namespace, "validate_loaded_backend", lambda config: calls.append("loaded"))
    config = {"binary": "/binary", "sentinel": "/sentinel", "main": "/main"}
    monkeypatch.setitem(namespace, "runtime_config", lambda path: config)
    monkeypatch.setitem(namespace, "inspect_sources", lambda *a, **k: "canonical sources")
    monkeypatch.setitem(namespace, "refresh_sources", lambda *a, **k: calls.append("refresh"))
    monkeypatch.setitem(namespace, "native_install", lambda action, unit: calls.append(action))
    monkeypatch.setattr(namespace["subprocess"], "run", lambda argv, **k: calls.append(argv[2]))
    monkeypatch.setitem(namespace, "ready", lambda config: calls.append("ready"))
    monkeypatch.setitem(namespace, "require_quiescent", lambda unit: calls.append("quiescent"))
    monkeypatch.setitem(namespace, "retire_managed", lambda: calls.append("rollback"))
    return namespace, calls, properties, config


@pytest.mark.parametrize(
    "fault", ["cache", "binary", "sentinel", "LoadState", "MemoryMax", "MemorySwapMax", "TasksMax"]
)
def test_invalid_setup_refuses_before_enable(operator, startup, monkeypatch, fault):
    namespace, calls, properties, config = startup
    if fault in ("cache", "binary"):
        function = "verify_cache" if fault == "cache" else "verified_binary"
        monkeypatch.setitem(namespace, function, Mock(side_effect=ValueError(fault)))
    elif fault == "sentinel":
        monkeypatch.setitem(namespace, "sentinel_armed", lambda path: True)
    else:
        properties[fault] = "wrong"
    with pytest.raises(ValueError):
        operator.enable(config)
    assert "enable" not in calls
    assert ("rollback" not in calls) == (fault in ("cache", "binary", "sentinel"))


@pytest.mark.parametrize("failure", ["start", "ready", "rollback"])
def test_failed_enable_retains_startup_and_rollback_errors(operator, startup, monkeypatch, failure):
    namespace, calls, _, config = startup
    if failure == "start":
        monkeypatch.setattr(
            namespace["subprocess"],
            "run",
            Mock(side_effect=subprocess.TimeoutExpired("enable", 150)),
        )
    else:
        monkeypatch.setitem(namespace, "ready", Mock(side_effect=ValueError("wrong native PID")))
    if failure == "rollback":
        monkeypatch.setitem(
            namespace, "retire_managed", Mock(side_effect=ValueError("stop refused"))
        )
    with pytest.raises(ValueError, match="native enable failed") as error:
        operator.enable(config)
    if failure == "rollback":
        assert "wrong native PID" in str(error.value) and "stop refused" in str(error.value)
    else:
        assert "rollback" in calls


def test_success_requires_actual_readiness(operator, startup):
    _, calls, _, config = startup
    operator.enable(config)
    assert calls == ["cache", "quiescent", "refresh", "cache", "loaded", "enable",
                     "cache", "quiescent", "refresh", "cache", "loaded", "start", "ready"]


def test_active_existing_backend_refuses_before_reload_or_retirement(operator, startup, monkeypatch):
    namespace, calls, _, config = startup
    monkeypatch.setitem(namespace, "require_quiescent", Mock(side_effect=ValueError("active backend")))
    with pytest.raises(ValueError, match="active backend"):
        operator.enable(config)
    assert not any(action in calls for action in ("enable", "start", "rollback"))


@pytest.mark.parametrize("fault", ["loaded", "cache", "database", "settings", "sentinel"])
def test_post_reload_changes_never_start_and_are_retired(operator, startup, monkeypatch, fault):
    namespace, calls, _, config = startup
    if fault == "settings":
        monkeypatch.setitem(namespace, "runtime_config", lambda path: {"changed": True})
    elif fault == "sentinel":
        monkeypatch.setitem(namespace, "sentinel_armed", Mock(side_effect=[False, False, True]))
    else:
        name = "validate_loaded_backend" if fault == "loaded" else "verify_cache"
        error = sqlite3.DatabaseError("late corruption") if fault == "database" else ValueError("late change")
        monkeypatch.setitem(namespace, name, Mock(side_effect=[None, error] if fault == "loaded"
                                                        else [None, None, error]))
    with pytest.raises(ValueError, match="native enable failed"):
        operator.enable(config)
    assert "enable" in calls and "start" not in calls and "rollback" in calls


def test_partial_parent_resolution_failure_still_reloads(operator, fixed_artifact_authority, tmp_path, monkeypatch):
    paths = artifact_population(operator, tmp_path, monkeypatch)
    resolve = operator.artifact_parent
    namespace = operator.remove_unit_artifacts.__globals__
    def changed(path):
        if not paths[0].exists():
            raise RuntimeError("symlink loop after first unlink")
        return resolve(path)
    monkeypatch.setitem(namespace, "artifact_parent", changed)
    reload = Mock()
    monkeypatch.setattr(namespace["subprocess"], "run", reload)
    with pytest.raises(ValueError, match="partial=True") as error:
        operator.remove_unit_artifacts()
    assert "symlink loop" in str(error.value)
    reload.assert_called_once()
    assert paths[1].exists()


@pytest.fixture
def fixed_artifact_authority(operator, monkeypatch):
    """Artifact tests isolate inode/unlink behavior; source gates have their own suite."""
    namespace = operator.remove_unit_artifacts.__globals__
    sources = ((), {operator.BACKEND: (None, None), operator.SLICE: (None, None)})
    monkeypatch.setitem(namespace, "inspect_sources", lambda *a, **k: sources)
    monkeypatch.setitem(namespace, "require_quiescent", lambda unit: None)
    monkeypatch.setitem(namespace, "refresh_sources", lambda *a, **k:
                        namespace["subprocess"].run(["/usr/bin/systemctl", "--user", "daemon-reload"],
                                                    check=True, timeout=30))
    # Direct artifact tests also enter the retirement precondition. Retirement
    # behavior itself remains exercised with explicit supported-state fixtures.
    monkeypatch.setitem(namespace, "retire_managed", lambda: sources)


@pytest.fixture
def supported_retirement(operator, monkeypatch):
    namespace = operator.retire_managed.__globals__
    sources = ((), {operator.BACKEND: ("source", None), operator.SLICE: ("source", None)})
    monkeypatch.setitem(namespace, "inspect_sources", lambda *a, **k: sources)
    monkeypatch.setitem(namespace, "refresh_sources", lambda *a, **k: None)
    def install(action, unit, *, runtime=False):
        result = namespace["subprocess"].run(
            ["/usr/bin/systemctl", "--user", action, *(("--runtime",) if runtime else ()), unit])
        if result.returncode:
            raise subprocess.CalledProcessError(result.returncode, action)
    monkeypatch.setitem(namespace, "native_install", install)


def artifact_population(operator, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    paths = [root / location / unit for root in
             (operator.units_dir(), tmp_path / "runtime/systemd/user")
             for location in (Path("."), Path("default.target.wants"))
             for unit in (operator.BACKEND, operator.SLICE)]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("owned")
    return paths


@pytest.mark.parametrize("slot", range(8))
@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_invalid_artifact_anywhere_preserves_complete_population(
    operator, fixed_artifact_authority, tmp_path, monkeypatch, slot, kind
):
    paths = artifact_population(operator, tmp_path, monkeypatch)
    paths[slot].unlink()
    paths[slot].mkdir() if kind == "directory" else os.mkfifo(paths[slot])
    before = [path.lstat().st_ino for path in paths]
    reload = Mock()
    monkeypatch.setattr(operator.remove_unit_artifacts.__globals__["subprocess"], "run", reload)
    with pytest.raises(ValueError, match="non-file"):
        operator.remove_unit_artifacts()
    assert [path.lstat().st_ino for path in paths] == before
    reload.assert_not_called()


@pytest.mark.parametrize("membership", [False, True])
def test_selected_directory_aliases_preserve_unrelated_names_and_targets(
    operator, fixed_artifact_authority, tmp_path, monkeypatch, membership
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    selected = operator.units_dir() / "default.target.wants" if membership else operator.units_dir()
    selected.parent.mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    selected.symlink_to(external, target_is_directory=True)
    target = tmp_path / "foreign"
    target.write_text("preserved")
    (external / operator.BACKEND).symlink_to(target)
    (external / "unrelated.service").write_text("preserved")
    monkeypatch.setattr(operator.remove_unit_artifacts.__globals__["subprocess"], "run", Mock())
    operator.remove_unit_artifacts()
    assert selected.is_symlink() and target.read_text() == "preserved"
    assert (external / "unrelated.service").read_text() == "preserved"
    assert not (external / operator.BACKEND).is_symlink()


def test_partial_unlink_failure_reloads_and_reports_failure(operator, fixed_artifact_authority, tmp_path, monkeypatch):
    paths = artifact_population(operator, tmp_path, monkeypatch)
    unlink = Path.unlink
    def fail(path, *args, **kwargs):
        if path == paths[-1]:
            raise PermissionError("injected unlink refusal")
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", fail)
    reload = Mock()
    monkeypatch.setattr(operator.remove_unit_artifacts.__globals__["subprocess"], "run", reload)
    with pytest.raises(ValueError, match="partial=True"):
        operator.remove_unit_artifacts()
    reload.assert_called_once()
    assert paths[-1].read_text() == "owned"


@pytest.mark.parametrize("fault", ["appeared", "replaced", "vanished", "lookup"])
def test_population_recheck_preserves_observed_changes(operator, fixed_artifact_authority, tmp_path, monkeypatch, fault):
    paths = artifact_population(operator, tmp_path, monkeypatch)
    if fault == "appeared":
        paths[-1].unlink()
    snapshot = operator.artifact_snapshot
    namespace = operator.remove_unit_artifacts.__globals__
    count = 0
    def observe(path):
        nonlocal count
        count += 1
        if count == 9:
            if fault == "lookup":
                raise PermissionError("injected lookup refusal")
            if fault in ("replaced", "vanished"):
                paths[-1].rename(paths[-1].with_suffix(".old"))
            if fault in ("appeared", "replaced"):
                paths[-1].write_text("replacement")
        return snapshot(path)
    monkeypatch.setitem(namespace, "artifact_snapshot", observe)
    reload = Mock()
    monkeypatch.setattr(namespace["subprocess"], "run", reload)
    if fault == "vanished":
        operator.remove_unit_artifacts()
        reload.assert_called_once()
    else:
        with pytest.raises((ValueError, PermissionError)):
            operator.remove_unit_artifacts()
        assert paths[0].read_text() == "owned"
        reload.assert_not_called()
        if fault != "lookup":
            assert paths[-1].read_text() == "replacement"


@pytest.mark.parametrize("kind", ["dangling", "cyclic", "file", "fifo"])
def test_invalid_membership_parent_refuses_every_unlink(operator, fixed_artifact_authority, tmp_path, monkeypatch, kind):
    paths = artifact_population(operator, tmp_path, monkeypatch)
    parent = paths[2].parent
    for path in paths[2:4]:
        path.unlink()
    parent.rmdir()
    if kind == "dangling":
        parent.symlink_to(tmp_path / "missing")
    elif kind == "cyclic":
        parent.symlink_to(parent)
    elif kind == "file":
        parent.write_text("not a directory")
    else:
        os.mkfifo(parent)
    reload = Mock()
    monkeypatch.setattr(operator.remove_unit_artifacts.__globals__["subprocess"], "run", reload)
    with pytest.raises((OSError, RuntimeError, ValueError)):
        operator.remove_unit_artifacts()
    assert paths[0].read_text() == "owned"
    reload.assert_not_called()


@pytest.mark.parametrize("partial", [False, True])
def test_reload_failure_never_reports_success(operator, fixed_artifact_authority, tmp_path, monkeypatch, partial):
    paths = artifact_population(operator, tmp_path, monkeypatch)
    if partial:
        unlink = Path.unlink
        def fail(path, *args, **kwargs):
            if path == paths[-1]:
                raise PermissionError("unlink refused")
            return unlink(path, *args, **kwargs)
        monkeypatch.setattr(Path, "unlink", fail)
    monkeypatch.setattr(operator.remove_unit_artifacts.__globals__["subprocess"], "run",
                        Mock(side_effect=subprocess.CalledProcessError(1, "reload")))
    with pytest.raises(ValueError, match="reload failed") as error:
        operator.remove_unit_artifacts()
    if partial:
        assert "unlink refused" in str(error.value) and paths[-1].exists()


def test_corrupt_cache_reports_refusal_before_mutation(
    operator, startup, tmp_path, monkeypatch, capsys
):
    namespace, calls, _, config = startup
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setitem(namespace, "runtime_config", lambda path: config)
    monkeypatch.setitem(
        namespace, "verify_cache", Mock(side_effect=sqlite3.DatabaseError("damaged cache"))
    )
    assert operator.lifecycle_main(argparse.Namespace(command="enable", config=None)) == 1
    assert "managed lifecycle refused: damaged cache" in capsys.readouterr().err
    assert "enable" not in calls and "rollback" not in calls


@pytest.mark.parametrize("initial", [{"runtime"}, {"persistent", "runtime"}])
def test_runtime_and_mixed_enablement_are_both_retired(operator, monkeypatch, initial, supported_retirement):
    links = set(initial)
    namespace = operator.retire_managed.__globals__

    def run(argv, **kwargs):
        if argv[2] == "disable":
            links.discard("runtime" if "--runtime" in argv else "persistent")
        return SimpleNamespace(returncode=0, stderr="")

    def show(*args):
        state = "enabled" if "persistent" in links else "enabled-runtime" if links else "disabled"
        return dict(UnitFileState=state, LoadState="loaded")

    monkeypatch.setattr(namespace["subprocess"], "run", run)
    monkeypatch.setitem(namespace, "show", show)
    monkeypatch.setitem(namespace, "require_quiescent", lambda unit: None)
    operator.retire_managed()
    assert not links


@pytest.mark.parametrize(
    "failure",
    [
        "disable-error",
        "backend-error",
        "slice-error",
        "backend-proof",
        "slice-proof",
        "state-error",
        "still-enabled",
    ],
)
def test_retirement_attempts_all_commands_and_proofs(operator, monkeypatch, failure, supported_retirement):
    namespace = operator.retire_managed.__globals__
    calls, proofs = [], []
    selected = {
        "disable-error": "disable",
        "backend-error": operator.BACKEND,
        "slice-error": operator.SLICE,
    }.get(failure)

    def run(argv, **kwargs):
        action, unit = argv[2], argv[-1]
        calls.append((action, "--runtime" in argv, unit))
        if selected == action or (action == "stop" and selected == unit):
            raise subprocess.TimeoutExpired(action, 60)
        return SimpleNamespace(returncode=0, stderr="")

    def proof(unit):
        proofs.append(unit)
        if failure == ("backend-proof" if unit == operator.BACKEND else "slice-proof"):
            raise ValueError("populated")

    def show(*args):
        if failure == "state-error":
            raise OSError("unreachable manager")
        return dict(
            UnitFileState="enabled" if failure == "still-enabled" else "disabled",
            LoadState="loaded",
        )

    monkeypatch.setattr(namespace["subprocess"], "run", run)
    monkeypatch.setitem(namespace, "require_quiescent", proof)
    monkeypatch.setitem(namespace, "show", show)
    with pytest.raises(ValueError, match="retirement failed"):
        operator.retire_managed()
    assert calls == [
        ("disable", False, operator.BACKEND),
        ("disable", True, operator.BACKEND),
        ("stop", False, operator.BACKEND),
        ("stop", False, operator.SLICE),
    ]
    assert proofs == [operator.BACKEND, operator.SLICE]


@pytest.mark.parametrize("command", ["disable", "remove"])
def test_retirement_does_not_read_settings(operator, tmp_path, monkeypatch, command):
    monkeypatch.setenv("HOME", str(tmp_path))
    namespace = operator.lifecycle_main.__globals__
    monkeypatch.setitem(
        namespace, "read_settings", Mock(side_effect=AssertionError("must not read"))
    )
    calls = []
    monkeypatch.setitem(namespace, "retire_managed", lambda: calls.append("retire"))
    monkeypatch.setitem(namespace, "remove_unit_artifacts", lambda retired: calls.append("remove"))
    assert operator.lifecycle_main(argparse.Namespace(command=command, config="/bad/settings")) == 0
    assert calls == (["retire"] if command == "disable" else ["retire", "remove"])


@pytest.mark.parametrize("command", ["enable", "disable", "remove"])
def test_exclusive_lifecycle_lock_refuses_busy_admission(operator, tmp_path, monkeypatch, command):
    monkeypatch.setenv("HOME", str(tmp_path))
    namespace = operator.lifecycle_main.__globals__
    callback = Mock(side_effect=AssertionError("busy lock must precede mutation"))
    monkeypatch.setitem(namespace, "enable", callback)
    monkeypatch.setitem(namespace, "retire_managed", callback)
    with operator.lifecycle_lock(shared=True):
        assert operator.lifecycle_main(argparse.Namespace(command=command, config=None)) == 1
    callback.assert_not_called()


@pytest.mark.parametrize("kind", ["regular", "symlink", "dangling", "directory", "fifo"])
def test_fixed_fragment_removal_preserves_state_and_foreign_targets(
    operator, fixed_artifact_authority, tmp_path, monkeypatch, kind
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    namespace = operator.remove_unit_artifacts.__globals__
    reload = Mock()
    monkeypatch.setattr(namespace["subprocess"], "run", reload)
    root = operator.units_dir()
    root.mkdir(parents=True)
    artifact = root / operator.BACKEND
    foreign = tmp_path / "foreign"
    foreign.write_text("preserved")
    if kind == "regular":
        artifact.write_text("owned")
    elif kind in ("symlink", "dangling"):
        artifact.symlink_to(foreign if kind == "symlink" else tmp_path / "missing")
    elif kind == "directory":
        artifact.mkdir()
    else:
        os.mkfifo(artifact)
    unrelated = root / "unrelated.slice"
    unrelated.write_text("preserved")
    if kind in ("directory", "fifo"):
        with pytest.raises(ValueError, match="non-file"):
            operator.remove_unit_artifacts()
        reload.assert_not_called()
    else:
        operator.remove_unit_artifacts()
        assert not artifact.exists() and not artifact.is_symlink()
        reload.assert_called_once()
    assert foreign.read_text() == "preserved" and unrelated.read_text() == "preserved"
