"""Native lifecycle validates before mutation and keeps retirement failures visible."""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.test_scripts.test_codebase_managed_config import managed

operator = managed


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
    monkeypatch.setattr(namespace["subprocess"], "run", lambda *a, **k: calls.append("enable"))
    monkeypatch.setitem(namespace, "ready", lambda config: calls.append("ready"))
    monkeypatch.setitem(namespace, "retire_managed", lambda: calls.append("rollback"))
    return namespace, calls, properties, {"binary": "/binary", "sentinel": "/sentinel"}


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
    assert "enable" not in calls and "rollback" not in calls


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
    assert calls == ["cache", "enable", "ready"]


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
def test_runtime_and_mixed_enablement_are_both_retired(operator, monkeypatch, initial):
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
def test_retirement_attempts_all_commands_and_proofs(operator, monkeypatch, failure):
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
    monkeypatch.setitem(namespace, "remove_unit_artifacts", lambda: calls.append("remove"))
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
    operator, tmp_path, monkeypatch, kind
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
