"""Real adapter/owned child/journal wiring; kernel facts and manager are fixtures."""

from __future__ import annotations

import json
import os
import runpy
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_scripts.test_code_intel_cbm_worker import _args, _fake_worker, worker
from tests.test_scripts.test_code_intel_execution import evidence as evidence
from tests.test_scripts.test_code_intel_execution import scope as scope
from tests.test_scripts.test_index_marker_execution import _reservation, _schema5
from tests.test_scripts.test_index_marker_legs import queue as queue

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def adapter(queue, scope, evidence, tmp_path, monkeypatch):
    def prepare(*, error=False, rc=0, preparation=None, force_promotion_failure=False):
        _fake_worker(tmp_path, monkeypatch, error=error, rc=rc)
        managed = worker.load_managed()
        config = managed["runtime_config"](tmp_path / "settings")
        raw = json.dumps(config).encode()
        order = []
        _schema5(queue)
        queue.write_marker(str(tmp_path), "both", "fast")
        claimed = queue.claim_native(queue.marker_hash(str(tmp_path)), provider="cbm")
        reserved = _reservation(claimed)
        reserved["source"]["provider_digest"] = worker.BUILD
        probe, leaf, proc = scope
        leaf = leaf.rename(leaf.with_name(reserved["unit"] + ".scope"))
        membership = "0::/genesis-workload.slice/" + leaf.name + "\n"
        (proc / "self/cgroup").write_text(membership)
        (proc / "self/task" / str(os.getpid()) / "cgroup").write_text(membership)
        (proc / "sys/kernel/random/boot_id").write_text(reserved["owner"]["boot_id"])
        (leaf / "cgroup.events").write_text("populated 1\n")
        probe.unit = reserved["unit"]
        assert queue.reserve_launch(claimed["context"], reserved) == "reserved"
        queued = runpy.run_path(str(ROOT / "scripts/lib/code_intel_queued_worker.py"))
        created = []

        def runtime(path, *, capture=None):
            order.append("config")
            if capture is not None:
                capture(raw)
            return config

        def ready(config):
            order.append("ready")
            assert queue.execution_status(claimed["context"])["scope_registration"] is not None
            if preparation:
                if preparation == "oom":
                    (leaf / "memory.events").write_text("low 0\nhigh 0\nmax 1\noom 1\noom_kill 1\n")
                raise ValueError("synthetic preparation refusal")

        def native_env(config):
            order.append("environment")
            value = dict(os.environ)
            if force_promotion_failure:
                value["CODE_INTEL_TEST_FORCE_OOM_ADJ_FAILURE"] = "1"
            return value

        def factory(config_value, arguments, cap, settings, environment):
            assert config_value is config and settings == raw and cap == 8 * 1024**3
            assert (
                arguments.mode == "full"
                and environment["GENESIS_HOME"] == os.environ["GENESIS_HOME"]
            )
            order.append("factory")

            def registration(unit, facts, *, wall_seconds):
                order.append("registration")
                return dict(unit=unit, facts=facts, invocation_id="1" * 32, stop_usec=90_000_000)

            run = queued["QueuedRun"](
                queue,
                SimpleNamespace(**evidence),
                probe,
                SimpleNamespace(registration=registration),
                claimed["context"],
                source=lambda: deepcopy(reserved["source"]),
                admit=lambda: order.append("admission"),
            )
            created.append(run)
            return run

        managed.update(runtime_config=runtime, ready=ready, native_env=native_env)
        return claimed, factory, created, order

    return prepare


@pytest.mark.parametrize("error,rc", [(False, 0), (True, 0), (False, 5)])
def test_queued_adapter_registers_before_queries_and_seals_actual_native_result(
    adapter, queue, tmp_path, error, rc
):
    claimed, factory, runs, order = adapter(error=error, rc=rc)
    score_before = Path("/proc/self/oom_score_adj").read_text()
    if error:
        with pytest.raises(ValueError):
            worker._execute_stock_worker(
                tmp_path / "settings", _args(tmp_path), 8 * 1024**3, queued_factory=factory
            )
    else:
        result = worker._execute_stock_worker(
            tmp_path / "settings", _args(tmp_path), 8 * 1024**3, queued_factory=factory
        )
        assert result == (0 if rc == 0 else 111)
    assert order.index("registration") < order.index("ready") < order.index("admission")
    assert order.count("environment") == 1
    status = queue.execution_status(claimed["context"])
    assert status["terminal"]["data"]["report"] == runs[0].report
    assert runs[0].report["payload_started"] and runs[0].report["payload_returncode"] == rc
    assert status["terminal"]["data"]["provider_ok"] is (not error and rc == 0)
    assert queue.apply_native(claimed["context"]) == (
        "consumed" if not error and rc == 0 else "pending"
    )
    assert Path("/proc/self/oom_score_adj").read_text() == score_before
    assert not list(tmp_path.glob("genesis-worker-*"))


@pytest.mark.parametrize("preparation", ["refused", "oom"])
def test_preparation_refusal_has_registration_no_intent_and_zero_indexing_debt(
    adapter, queue, tmp_path, preparation
):
    claimed, factory, runs, order = adapter(preparation=preparation)
    with pytest.raises(ValueError, match="preparation"):
        worker._execute_stock_worker(
            tmp_path / "settings", _args(tmp_path), 8 * 1024**3, queued_factory=factory
        )
    status = queue.execution_status(claimed["context"])
    assert status["scope_registration"] is not None and status["native_intent"] is None
    assert status["terminal"]["data"]["report"]["payload_started"] is False
    assert queue.apply_native(claimed["context"]) == "pending"
    status = queue.execution_status(claimed["context"])
    assert status["typed_charged_attempts"] == 0
    assert status["safety_kind"] == ("resource" if preparation == "oom" else None)


def test_owned_child_promotion_failure_is_retained_without_changing_reporter(
    adapter, queue, tmp_path
):
    claimed, factory, runs, order = adapter(force_promotion_failure=True)
    score_before = Path("/proc/self/oom_score_adj").read_text()
    assert (
        worker._execute_stock_worker(
            tmp_path / "settings", _args(tmp_path), 8 * 1024**3, queued_factory=factory
        )
        == 111
    )
    assert runs[0].report["payload_started"] and runs[0].report["payload_returncode"] != 0
    assert queue.apply_native(claimed["context"]) == "pending"
    assert queue.execution_status(claimed["context"])["typed_charged_attempts"] == 1
    assert Path("/proc/self/oom_score_adj").read_text() == score_before
