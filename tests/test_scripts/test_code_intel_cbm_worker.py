"""Stock worker transport errors must never consume a queued refresh."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

HELPER = Path(__file__).resolve().parents[2] / "scripts/lib/code_intel_cbm_worker.py"
SPEC = importlib.util.spec_from_file_location("cbm_worker", HELPER)
assert SPEC and SPEC.loader
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"not json",
        b"[]",
        b"{}",
        b'{"isError": true,"content":[]}',
        b'{"isError":0,"content":[]}',
        b'{"isError":false}',
        b"x" * (worker.MAX_RESPONSE + 1),
    ],
)
def test_invalid_or_failed_response_refuses(payload, tmp_path):
    response = tmp_path / "result"
    response.write_bytes(payload)
    with pytest.raises(ValueError):
        worker.read_result(response)


def test_successful_transport_with_tool_error_fails(tmp_path, monkeypatch):
    """Worker protocol deliberately returns zero for a valid MCP error."""
    _fake_worker(tmp_path, monkeypatch, error=True)
    with pytest.raises(ValueError, match="error"):
        worker._execute_stock_worker(tmp_path / "settings", _args(tmp_path), 8 * 1024**3)


def _args(tmp_path):
    return SimpleNamespace(repo_path=str(tmp_path), mode="full", persistence="false")


def _main_args(tmp_path):
    return ["--managed-config", str(tmp_path / "settings"), "--repo-path", str(tmp_path),
            "--mode", "full", "--persistence", "false"]


def _fake_worker(tmp_path, monkeypatch, *, error=False, rc=0):
    binary = tmp_path / "binary"
    binary.write_text(
        "#!/usr/bin/python3\nimport json,sys\n"
        "assert sys.argv[1:3]==['cli','--index-worker']\n"
        "response=sys.argv[sys.argv.index('--response-out')+1]\n"
        "open(response,'w').write(json.dumps("
        + repr({"isError": error, "content": [{"type": "text", "text": "ok"}]})
        + f"))\nsys.exit({rc})\n"
    )
    binary.chmod(0o700)
    monkeypatch.setattr(worker, "BUILD", hashlib.sha256(binary.read_bytes()).hexdigest())
    managed = runpy.run_path(str(HELPER.parents[1] / "codebase_managed.py"))
    monkeypatch.setitem(managed["verified_binary"].__globals__, "BUILD", worker.BUILD)
    config = dict(
        binary=str(binary), main=str(tmp_path), cache=str(tmp_path), runtime=str(tmp_path)
    )
    managed.update(
        runtime_config=lambda path: config,
        verify_cache=lambda config: None,
        ready=lambda config: None,
        require_enabled=lambda config: None,
        check_backend=lambda config: "123",
        lifecycle_lock=lambda shared: managed["file_lock"](tmp_path / "lock", shared=shared),
    )
    monkeypatch.setattr(worker, "load_managed", lambda: managed)


def test_verified_inode_exec_and_response_cleanup(tmp_path, monkeypatch, capsys):
    _fake_worker(tmp_path, monkeypatch)
    assert worker._execute_stock_worker(tmp_path / "settings", _args(tmp_path), 8 * 1024**3) == 0
    assert json.loads(capsys.readouterr().out)["isError"] is False
    assert not list(tmp_path.glob("genesis-worker-*"))


@pytest.mark.parametrize("rc", [1, 3, 4, 5, 75, 125])
def test_process_failures_are_never_queue_partial_success_or_deferral(rc, tmp_path, monkeypatch):
    _fake_worker(tmp_path, monkeypatch, rc=rc)
    assert worker._execute_stock_worker(tmp_path / "settings", _args(tmp_path), 8 * 1024**3) == 111
    assert not list(tmp_path.glob("genesis-worker-*"))


def test_pin_mismatch_refuses_before_spawn(tmp_path, monkeypatch):
    _fake_worker(tmp_path, monkeypatch)
    monkeypatch.setitem(worker.load_managed()["verified_binary"].__globals__, "BUILD", "0" * 64)
    with pytest.raises(ValueError, match="unsupported"):
        worker._execute_stock_worker(tmp_path / "settings", _args(tmp_path), 8 * 1024**3)
    assert not list(tmp_path.glob("genesis-worker-*"))


def test_standalone_invocation_cannot_bypass_scope_admission(tmp_path, monkeypatch):
    for key in [
        "CODE_INTEL_CHILD_CAP_BYTES",
        "CODE_INTEL_CHILD_RESERVE_BYTES",
        "CODE_INTEL_CHILD_SCOPE_UNIT",
    ]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        worker, "_execute_stock_worker", lambda *args: pytest.fail("unadmitted spawn")
    )
    assert (
        worker.main(_main_args(tmp_path))
        == 111
    )


def test_second_admission_refusal_keeps_attempt_uncharged(tmp_path, monkeypatch):
    marker = tmp_path / "refusal"
    monkeypatch.setenv("CODE_INTEL_CHILD_REFUSAL_MARKER", str(marker))
    # Empty cap is a genuine admission refusal before a worker is launched.
    monkeypatch.delenv("CODE_INTEL_CHILD_CAP_BYTES", raising=False)
    monkeypatch.setattr(
        worker, "_execute_stock_worker", lambda *args: pytest.fail("unadmitted spawn")
    )
    assert (
        worker.main(_main_args(tmp_path))
        == 111
    )
    assert marker.read_text() == "refused\n"
