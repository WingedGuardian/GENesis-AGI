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
        runtime.require_enabled.__globals__, "show", lambda *a: {"UnitFileState": state}
    )
    with pytest.raises(ValueError, match="persistently enabled"):
        runtime.require_enabled({"sentinel": "/unused"})


def test_persistent_enablement_still_refuses_sentinel(runtime, tmp_path, monkeypatch):
    monkeypatch.setitem(
        runtime.require_enabled.__globals__, "show", lambda *a: {"UnitFileState": "enabled"}
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
    monkeypatch.setitem(
        runtime.verify_query_boundary.__globals__, "resolve_cgroup", lambda *a: (leaf, root, 2)
    )
    return root, leaf


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
    monkeypatch.setattr(namespace["time"], "monotonic", Mock(side_effect=[0, 0, 0, 61]))
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
    monkeypatch.setitem(namespace, "show", lambda *a: state)
    monkeypatch.setitem(namespace, "check_backend", lambda *a, **kw: "123")
    monkeypatch.setitem(namespace, "verified_binary", lambda *a: binary.open("rb"))
    monkeypatch.setattr(namespace["time"], "monotonic", Mock(side_effect=[0, 0, 0, 61]))
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
    monkeypatch.setattr(namespace["time"], "monotonic", Mock(side_effect=[0, 0, 2, 61, 62, 63]))
    monkeypatch.setattr(namespace["time"], "sleep", Mock())

    def rpc(argv, **kwargs):
        if kwargs["timeout"] < 7:
            raise namespace["subprocess"].TimeoutExpired(argv, kwargs["timeout"])
        assert kwargs["timeout"] == 58
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
    monkeypatch.setattr(namespace["time"], "monotonic", Mock(side_effect=[0, 0, 60]))
    rpc = Mock()
    monkeypatch.setattr(namespace["subprocess"], "run", rpc)
    with pytest.raises(ValueError, match="ready"):
        runtime.ready(dict(binary=str(binary)))
    rpc.assert_not_called()
