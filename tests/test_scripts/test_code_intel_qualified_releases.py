"""Selected build identity reaches queue evidence and the actual harmless child."""

from copy import deepcopy
from pathlib import Path

import pytest

from tests.test_scripts.managed_code_intel_fixture import qualify_bytes
from tests.test_scripts.test_code_intel_cbm_factory import bound as bound
from tests.test_scripts.test_code_intel_cbm_worker import worker
from tests.test_scripts.test_code_intel_execution import evidence as evidence
from tests.test_scripts.test_code_intel_execution import scope as scope
from tests.test_scripts.test_code_intel_runtime_capsule import runtime
from tests.test_scripts.test_code_intel_source import case
from tests.test_scripts.test_code_intel_source import isolated_modules as isolated_modules
from tests.test_scripts.test_index_marker_legs import queue as queue


@pytest.mark.parametrize("variant", ["first", "second"])
def test_qualified_selection_reaches_actual_child_and_durable_source(bound, queue, tmp_path, monkeypatch, variant):
    value = bound(variant=variant)
    monkeypatch.setattr(worker, "BUILD", "f" * 64)
    factory = value.queued["cbm_factory"](value.snapshot, value.modules, value.claimed["context"], value.policy)
    assert worker._execute_stock_worker(tmp_path / "settings", value.args, worker.JOB_CAP,
                                        queued_factory=factory) == 0
    state = queue.execution_status(value.claimed["context"])
    # The child asserts --index-worker-build equals its own opened bytes.
    assert state["native_intent"]["source"]["provider_digest"] == value.config["build"]
    assert state["native_intent"]["executable"]["sha256"] == value.config["build"]
    assert state["native_intent"]["source"] == state["launch_reservation"]["source"]
    assert state["terminal"]["data"]["provider_ok"] is True


def test_other_qualified_image_at_configured_path_cannot_spawn(bound, queue, tmp_path, monkeypatch):
    value = bound()
    replacement = b"#!/bin/sh\nexit 97\n"
    qualify_bytes(monkeypatch, worker.qualified_release, replacement)
    qualify_bytes(monkeypatch, value.managed["qualified_release"], replacement)
    Path(value.config["binary"]).write_bytes(replacement)
    factory = value.queued["cbm_factory"](value.snapshot, value.modules, value.claimed["context"], value.policy)
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *a, **k: pytest.fail("wrong build spawned"))
    with pytest.raises(ValueError, match="configured build"):
        worker._execute_stock_worker(tmp_path / "settings", value.args, worker.JOB_CAP,
                                     queued_factory=factory)
    state = queue.execution_status(value.claimed["context"])
    assert state["native_intent"] is None
    assert state["terminal"]["data"]["report"]["payload_started"] is False
    assert state["launch_reservation"]["source"]["provider_digest"] == value.config["build"]


@pytest.mark.parametrize("selected", [0, 1])
@pytest.mark.parametrize("fault", ["none", "unknown", "label"])
def test_private_runtime_decodes_captured_release_without_io(tmp_path, monkeypatch, selected, fault):
    _, _, captured = case(tmp_path, monkeypatch, "runner")
    modules = captured.assemble()
    digests = qualify_bytes(monkeypatch, modules["worker"].qualified_release, b"first", b"second")
    value = runtime(captured, modules)
    source = value["reservation"]["source"]
    source["provider_digest"] = "0" * 64 if fault == "unknown" else digests[selected]
    source["provider_version"] = "wrong-contract" if fault == "label" else "fixture-stock-worker"
    bound_source = captured.with_runtime(value)
    before = deepcopy(value)
    def no_io(*args, **kwargs):
        pytest.fail("runtime decoding performed filesystem or queue I/O")
    monkeypatch.setattr(Path, "open", no_io)
    monkeypatch.setattr(modules["queue"], "_connect", no_io)
    if fault == "none":
        decoded, _ = modules["queued"].runtime_inputs(bound_source, modules)
        assert decoded == before
    else:
        with pytest.raises(ValueError, match="unsupported|accepted"):
            modules["queued"].runtime_inputs(bound_source, modules)


def test_new_live_policy_does_not_modify_captured_release_records(tmp_path, monkeypatch):
    root, _, captured = case(tmp_path, monkeypatch, "runner")
    worker_path = root / "scripts/lib/code_intel_cbm_worker.py"
    worker_path.write_text(worker_path.read_text() + "\nRELEASES = {}\n")
    modules = captured.assemble()
    assert modules["worker"].qualified_release(modules["worker"].BUILD)["version"] == "0.11.0"
    assert not captured.current_matches()
