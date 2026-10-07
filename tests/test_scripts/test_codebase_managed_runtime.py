"""Managed native query startup refuses an unproved execution boundary."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.test_scripts.test_codebase_managed_config import managed

runtime = managed


@pytest.mark.parametrize(
    "state", ["disabled", "enabled-runtime", "static", "indirect", "linked", "masked", ""]
)
def test_only_persistent_enablement_authorizes_runtime(runtime, monkeypatch, state):
    monkeypatch.setitem(
        runtime.require_enabled.__globals__, "show", lambda *a, **kw: {"UnitFileState": state}
    )
    with pytest.raises(ValueError, match="persistently enabled"):
        runtime.require_enabled({"sentinel": "/unused"})


def test_persistent_enablement_still_refuses_sentinel(runtime, tmp_path, monkeypatch):
    monkeypatch.setitem(
        runtime.require_enabled.__globals__, "show", lambda *a, **kw: {"UnitFileState": "enabled"}
    )
    sentinel = tmp_path / "disabled"
    sentinel.symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="sentinel"):
        runtime.require_enabled({"sentinel": str(sentinel)})
    sentinel.unlink()
    runtime.require_enabled({"sentinel": str(sentinel)})


@pytest.fixture
def boundary(runtime, tmp_path, monkeypatch):
    root = tmp_path / "cgroup"
    leaf = root / "user.slice" / runtime.BACKEND
    leaf.mkdir(parents=True)
    for node in (root, leaf.parent, leaf):
        (node / "memory.max").write_text(str(2 * 1024**3))
        (node / "memory.swap.max").write_text("0")
        (node / "memory.current").write_text("0")
        (node / "memory.stat").write_text(
            "inactive_file 0\nactive_file 0\nfile_dirty 0\nfile_writeback 0\n"
        )
    (leaf / "cpu.max").write_text("200000 100000")
    (leaf / "pids.max").write_text("128")
    monkeypatch.setitem(
        runtime.verify_query_boundary.__globals__, "resolve_cgroup", lambda *a: (leaf, root, 2)
    )
    monkeypatch.setitem(runtime.verify_query_boundary.__globals__, "_host_available", lambda *a: 8 * 1024**3)
    return root, leaf


@pytest.mark.parametrize(
    "filename,value",
    [
        ("cpu.max", None),
        ("cpu.max", "max 100000"),
        ("cpu.max", "200001 100000"),
        ("cpu.max", "200000 0"),
        ("cpu.max", "0 100000"),
        ("cpu.max", "200000"),
        ("cpu.max", "200000 100000 extra"),
        ("cpu.max", "invalid 100000"),
        ("pids.max", None),
        ("pids.max", "max"),
        ("pids.max", "129"),
        ("pids.max", "invalid"),
    ],
)
def test_query_refuses_missing_or_unenforced_cpu_and_task_caps(runtime, boundary, filename, value):
    _, leaf = boundary
    path = leaf / filename
    if value is None:
        path.unlink()
    else:
        path.write_text(value)
    with pytest.raises((OSError, ValueError)):
        runtime.verify_query_boundary("self")


@pytest.mark.parametrize("quota,period,tasks", [(200000, 100000, 128), (50000, 25000, 64), (200000, 100000, 0)])
def test_query_accepts_enforced_cpu_and_task_ceilings(runtime, boundary, quota, period, tasks):
    _, leaf = boundary
    (leaf / "cpu.max").write_text(f"{quota} {period}")
    (leaf / "pids.max").write_text(str(tasks))
    runtime.verify_query_boundary("self")


@pytest.mark.parametrize("ancestor", ["parent", "root"])
@pytest.mark.parametrize("cached", [False, True])
def test_startup_admission_uses_reclaimable_ancestor_charge(runtime, boundary, ancestor, cached):
    root, leaf = boundary
    target = root if ancestor == "root" else leaf.parent
    (target / "memory.max").write_text(str(4 * 1024**3))
    (target / "memory.current").write_text(str(5 * 1024**3))
    if cached:
        (target / "memory.stat").write_text(
            f"inactive_file {5 * 1024**3}\nactive_file 0\nfile_dirty 0\nfile_writeback 0\n"
        )
        runtime.verify_query_boundary("self", startup=True)
    else:
        with pytest.raises(ValueError, match="headroom"):
            runtime.verify_query_boundary("self", startup=True)
    # Existing daemon health remains a physical-containment check.
    runtime.verify_query_boundary("self")


@pytest.mark.parametrize("stat", ["invalid", "inactive_file 999999999999999999999999"])
def test_startup_does_not_credit_unvalidated_cache_or_own_leaf_usage(runtime, boundary, stat):
    root, leaf = boundary
    (root / "memory.max").write_text(str(4 * 1024**3))
    (root / "memory.current").write_text(str(3 * 1024**3))
    (root / "memory.stat").write_text(stat)
    (leaf / "memory.current").write_text(str(2 * 1024**3))
    with pytest.raises(ValueError, match="headroom"):
        runtime.verify_query_boundary("self", startup=True)


def test_startup_requires_host_headroom_even_with_unlimited_ancestors(runtime, boundary, monkeypatch):
    root, leaf = boundary
    for node in (root, leaf.parent):
        (node / "memory.max").write_text("max")
    monkeypatch.setitem(runtime.verify_query_boundary.__globals__, "_host_available", lambda *a: 1024**3)
    with pytest.raises(ValueError, match="host available"):
        runtime.verify_query_boundary("self", startup=True)


@pytest.mark.parametrize(
    "fault", ["none", "leaf", "swap", "parent", "root", "missing-parent", "true-root"]
)
def test_kernel_caps_and_every_visible_ancestor(runtime, boundary, fault):
    root, leaf = boundary
    if fault == "leaf":
        (leaf / "memory.max").write_text("max")
    elif fault == "swap":
        (leaf / "memory.swap.max").write_text("1")
    elif fault in ("parent", "root"):
        target = root if fault == "root" else leaf.parent
        (target / "memory.max").write_text(str(1024**3))
    elif fault in ("missing-parent", "true-root"):
        target = root if fault == "true-root" else leaf.parent
        (target / "memory.max").unlink()
    if fault in ("none", "true-root"):
        runtime.verify_query_boundary("self")
    else:
        with pytest.raises((OSError, ValueError)):
            runtime.verify_query_boundary("self")


def test_wrong_unit_and_v1_refuse(runtime, boundary, monkeypatch):
    root, leaf = boundary
    for selected, version in ((leaf.parent, 2), (leaf, 1)):
        monkeypatch.setitem(
            runtime.verify_query_boundary.__globals__,
            "resolve_cgroup",
            Mock(return_value=(selected, root, version)),
        )
        with pytest.raises(ValueError):
            runtime.verify_query_boundary("self")


@pytest.mark.parametrize(
    "response",
    [
        "daemon: active (permanent)\n  pid: 123\n",
        "daemon: active (permanent)\n  pid: 999\n",
        "daemon: active (temporary)\n  pid: 123\n",
        "daemon: active (permanent)\n  pid: 123\n  state: stopping\n",
    ],
)
def test_readiness_requires_permanent_native_rpc_matching_manager_pid(
    runtime, tmp_path, monkeypatch, response
):
    binary = tmp_path / "binary"
    binary.write_bytes(b"fixture")
    namespace = runtime.ready.__globals__
    monkeypatch.setitem(namespace, "require_enabled", Mock())
    monkeypatch.setitem(namespace, "check_backend", lambda *a, **kw: "123")
    monkeypatch.setitem(namespace, "verified_binary", lambda *a: binary.open("rb"))
    monkeypatch.setattr(namespace["time"], "monotonic", Mock(side_effect=[0] * 10 + [61] * 20))
    monkeypatch.setattr(namespace["time"], "sleep", Mock())
    monkeypatch.setattr(
        namespace["subprocess"],
        "run",
        Mock(return_value=SimpleNamespace(returncode=0, stdout=response)),
    )
    config = dict(
        binary=str(binary), cache=str(tmp_path), runtime=str(tmp_path), main=str(tmp_path)
    )
    if response == "daemon: active (permanent)\n  pid: 123\n":
        runtime.ready(config)
    else:
        with pytest.raises(ValueError, match="ready"):
            runtime.ready(config)


@pytest.mark.parametrize("fault", ["sentinel", "disable"])
def test_readiness_rechecks_authority_after_successful_rpc(runtime, tmp_path, monkeypatch, fault):
    binary, sentinel = tmp_path / "binary", tmp_path / "disabled"
    binary.write_bytes(b"fixture")
    namespace = runtime.ready.__globals__
    state = {"UnitFileState": "enabled"}
    monkeypatch.setitem(namespace, "show", lambda *a, **kw: state)
    monkeypatch.setitem(namespace, "check_backend", lambda *a, **kw: "123")
    monkeypatch.setitem(namespace, "verified_binary", lambda *a: binary.open("rb"))
    monkeypatch.setattr(namespace["time"], "monotonic", Mock(side_effect=[0] * 10 + [61] * 20))
    monkeypatch.setattr(namespace["time"], "sleep", Mock())

    def rpc(*args, **kwargs):
        if fault == "sentinel":
            sentinel.touch()
        else:
            state["UnitFileState"] = "disabled"
        return SimpleNamespace(returncode=0, stdout="daemon: active (permanent)\n  pid: 123\n")

    monkeypatch.setattr(namespace["subprocess"], "run", rpc)
    config = dict(
        binary=str(binary),
        sentinel=str(sentinel),
        cache=str(tmp_path),
        runtime=str(tmp_path),
        main=str(tmp_path),
    )
    with pytest.raises(ValueError, match="ready"):
        runtime.ready(config)


def test_native_status_can_use_remaining_startup_deadline(runtime, tmp_path, monkeypatch):
    """A valid seven-second RPC must not be killed by a separate three-second cap."""
    binary = tmp_path / "binary"
    binary.write_bytes(b"fixture")
    namespace = runtime.ready.__globals__
    monkeypatch.setitem(namespace, "require_enabled", Mock())
    monkeypatch.setitem(namespace, "check_backend", lambda *a, **kw: "123")
    monkeypatch.setitem(namespace, "verified_binary", lambda *a: binary.open("rb"))
    monkeypatch.setattr(namespace["time"], "monotonic", lambda: 0)
    monkeypatch.setattr(namespace["time"], "sleep", Mock())

    def rpc(argv, **kwargs):
        if kwargs["timeout"] < 7:
            raise namespace["subprocess"].TimeoutExpired(argv, kwargs["timeout"])
        assert kwargs["timeout"] == 60
        return SimpleNamespace(returncode=0, stdout="daemon: active (permanent)\n  pid: 123\n")

    monkeypatch.setattr(namespace["subprocess"], "run", rpc)
    runtime.ready(dict(binary=str(binary), main=str(tmp_path), cache=str(tmp_path), runtime=str(tmp_path)))


def test_readiness_does_not_start_rpc_after_deadline(runtime, tmp_path, monkeypatch):
    binary = tmp_path / "binary"
    binary.write_bytes(b"fixture")
    namespace = runtime.ready.__globals__
    monkeypatch.setitem(namespace, "require_enabled", Mock())
    monkeypatch.setitem(namespace, "check_backend", Mock(return_value="123"))
    monkeypatch.setitem(namespace, "verified_binary", lambda *a: binary.open("rb"))
    monkeypatch.setattr(namespace["time"], "monotonic", Mock(side_effect=[0, 120]))
    rpc = Mock()
    monkeypatch.setattr(namespace["subprocess"], "run", rpc)
    with pytest.raises(ValueError, match="ready"):
        runtime.ready(dict(binary=str(binary)))
    rpc.assert_not_called()


@pytest.mark.parametrize("native_at,expected_timeout", [(55, 60), (75, 45), (120, None)])
def test_readiness_native_window_is_clipped_to_total_startup_budget(
    runtime, tmp_path, monkeypatch, native_at, expected_timeout
):
    binary = tmp_path / "binary"
    binary.write_bytes(b"fixture")
    namespace = runtime.ready.__globals__
    clock = [0]
    monkeypatch.setitem(namespace, "require_enabled", Mock())
    monkeypatch.setitem(namespace, "verified_binary", lambda *a: binary.open("rb"))
    monkeypatch.setattr(namespace["time"], "monotonic", lambda: clock[0])
    monkeypatch.setattr(namespace["time"], "sleep", lambda *a: clock.__setitem__(0, clock[0] + 1))

    def native(*args, **kwargs):
        clock[0] = max(clock[0], native_at)
        return "123"

    monkeypatch.setitem(namespace, "check_backend", native)
    rpc = Mock(return_value=SimpleNamespace(returncode=0, stdout="daemon: active (permanent)\n  pid: 123\n"))
    monkeypatch.setattr(namespace["subprocess"], "run", rpc)
    if expected_timeout is None:
        with pytest.raises(ValueError, match="ready"):
            runtime.ready(dict(binary=str(binary)))
        rpc.assert_not_called()
    else:
        runtime.ready(dict(binary=str(binary), main=str(tmp_path), cache=str(tmp_path), runtime=str(tmp_path)))
        assert rpc.call_args.kwargs["timeout"] == expected_timeout


@pytest.mark.parametrize("rpc_finishes_at", [114, 115, 116])
def test_readiness_clips_final_manager_calls_and_rejects_late_success(
    runtime, tmp_path, monkeypatch, rpc_finishes_at
):
    binary = tmp_path / "binary"
    binary.write_bytes(b"fixture")
    namespace = runtime.ready.__globals__
    clock = [0]
    enable = Mock()
    monkeypatch.setitem(namespace, "require_enabled", enable)
    monkeypatch.setitem(namespace, "verified_binary", lambda *a: binary.open("rb"))
    monkeypatch.setattr(namespace["time"], "monotonic", lambda: clock[0])
    monkeypatch.setattr(namespace["time"], "sleep", lambda *a: clock.__setitem__(0, clock[0] + 1))

    def native(*args, **kwargs):
        clock[0] = max(clock[0], 55)
        return "123"

    backend = Mock(side_effect=native)
    monkeypatch.setitem(namespace, "check_backend", backend)

    def rpc(*args, **kwargs):
        assert kwargs["timeout"] == 60
        clock[0] = rpc_finishes_at
        return SimpleNamespace(returncode=0, stdout="daemon: active (permanent)\n  pid: 123\n")

    monkeypatch.setattr(namespace["subprocess"], "run", rpc)
    config = dict(binary=str(binary), main=str(tmp_path), cache=str(tmp_path), runtime=str(tmp_path))
    if rpc_finishes_at < 115:
        runtime.ready(config)
        assert backend.call_args.kwargs["timeout"] == 1
        assert enable.call_args.kwargs["timeout"] == 1
    else:
        with pytest.raises(ValueError, match="ready"):
            runtime.ready(config)
        assert backend.call_count == 1
        assert enable.call_count == 1
