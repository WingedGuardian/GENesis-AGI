"""Actual source-bound factory drives the adapter, queue and owned toy child."""

import hashlib
import json
import os
import runpy
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
def bound(queue, scope, evidence, tmp_path, monkeypatch):
    def prepare(*, error=False, frozen_environment=False, path_fault=None):
        _fake_worker(tmp_path, monkeypatch, error=error)
        managed = worker.load_managed()
        config = managed["runtime_config"](tmp_path / "settings")
        observation = tmp_path / "native-environment-observation.json"
        if frozen_environment:
            binary = Path(config["binary"])
            record = ("import os\nopen(" + repr(str(observation))
                      + ", 'w').write(json.dumps({key:os.environ.get(key) for key in "
                      "['INVOCATION_ID','SYNTHETIC_VALUE','CBM_CACHE_DIR','CONTROL_ONLY']}))\n")
            binary.write_text(binary.read_text().replace("sys.exit(", record + "sys.exit(", 1))
            monkeypatch.setattr(worker, "BUILD", hashlib.sha256(binary.read_bytes()).hexdigest())
            monkeypatch.setitem(managed["verified_binary"].__globals__, "BUILD", worker.BUILD)
        raw = json.dumps(config).encode()
        args = _args(tmp_path)
        queued = runpy.run_path(str(ROOT / "scripts/lib/code_intel_queued_worker.py"))
        snapshot = SimpleNamespace(commit="a" * 40, files={"scripts/runner.sh": "b" * 64},
                                   current_matches=lambda: True)
        policy = dict(route="workload", slice="genesis-workload.slice", wall_seconds=3600,
                      sibling_reserve_bytes=2 * 1024**3, cache_reserve_bytes=2 * 1024**3)
        native_env = managed["native_env"](config)
        canonical_native_env = managed["native_env"]
        environment_recipe = None
        source_environment = native_env
        source_options = {}
        if frozen_environment:
            frozen = dict(native_env)
            frozen.pop("INVOCATION_ID", None)  # The explicit slot replaces a parent invocation.
            frozen.pop("CONTROL_ONLY", None)
            frozen["SYNTHETIC_VALUE"] = "frozen-fixture-value"
            if path_fault:
                frozen[path_fault] = "/fixture/unaccepted-root"
            environment_recipe = queued["NativeEnvironment"](dict(
                frozen=frozen, registration_slots={"INVOCATION_ID": "scope.invocation_id"}), queue._native)
            source_environment = environment_recipe.frozen
            source_options["registration_slots"] = environment_recipe.recipe["registration_slots"]
        _schema5(queue)
        queue.write_marker(str(tmp_path), "cbm", "full")
        claimed = queue.claim_native(queue.marker_hash(str(tmp_path)), provider="cbm")
        reserved = _reservation(claimed)
        reserved["source"] = queued["cbm_source"](
            snapshot=snapshot, worker=worker, config=config, args=args, cap=8 * 1024**3,
            settings=raw, environment=source_environment, policy=policy, **source_options)
        probe, leaf, proc = scope
        leaf = leaf.rename(leaf.with_name(reserved["unit"] + ".scope"))
        membership = "0::/genesis-workload.slice/" + leaf.name + "\n"
        (proc / "self/cgroup").write_text(membership)
        (proc / "self/task" / str(os.getpid()) / "cgroup").write_text(membership)
        (proc / "sys/kernel/random/boot_id").write_text(reserved["owner"]["boot_id"])
        (leaf / "cgroup.events").write_text("populated 1\n")
        probe.unit = reserved["unit"]
        queue.reserve_launch(claimed["context"], reserved)
        calls = []

        def make_probe(unit, cap, *, route):
            assert unit == reserved["unit"] and cap == 8 * 1024**3
            assert route == ("workload", "genesis-workload.slice")
            return probe

        def registration(unit, facts, *, wall_seconds):
            calls.append("registration")
            assert wall_seconds == policy["wall_seconds"]
            return dict(unit=unit, facts=facts, invocation_id="1" * 32, stop_usec=90_000_000)

        def assess(cap, reserve, unit, *, provider, cache_reserve):
            calls.append("admission")
            assert (cap, reserve, unit, provider, cache_reserve) == (
                8 * 1024**3, 2 * 1024**3, reserved["unit"], "cbm", 2 * 1024**3)

        modules = dict(queue=queue, worker=worker,
                       proof=SimpleNamespace(**{**evidence, "ScopeProbe": make_probe}),
                       scope=SimpleNamespace(ScopeManager=lambda *a, **kw: SimpleNamespace(registration=registration)),
                       unit=SimpleNamespace(bus_call=None, typed_property=None),
                       admission=SimpleNamespace(assess=assess))

        def runtime(path, *, capture=None):
            if capture is not None:
                capture(raw)
            return config

        ready_environments = []

        def ready(config, *, environment=None):
            calls.append("ready")
            if environment is not None:
                ready_environments.append(dict(environment))

        def native_environment(config, *, environment=None):
            if environment is None:
                return dict(native_env)
            return canonical_native_env(config, environment=environment)

        managed.update(runtime_config=runtime, native_env=native_environment, ready=ready)
        return SimpleNamespace(queued=queued, snapshot=snapshot, policy=policy, modules=modules,
                               claimed=claimed, config=config, raw=raw, args=args,
                               environment=native_env, calls=calls, managed=managed,
                               environment_recipe=environment_recipe, ready_environments=ready_environments,
                               environment_observation=observation)
    return prepare


def test_registered_recipe_reaches_readiness_and_the_actual_owned_native_child(bound, queue, tmp_path, monkeypatch):
    case = bound(frozen_environment=True)
    monkeypatch.setenv("INVOCATION_ID", "1" * 32)
    monkeypatch.setenv("SYNTHETIC_VALUE", "later-control-value")
    monkeypatch.setenv("CONTROL_ONLY", "scope-control-value")
    factory = case.queued["cbm_factory"](case.snapshot, case.modules, case.claimed["context"], case.policy,
                                          environment_recipe=case.environment_recipe)
    assert worker._execute_stock_worker(tmp_path / "settings", case.args, 8 * 1024**3,
                                        queued_factory=factory,
                                        queued_native_environment=case.environment_recipe) == 0
    expected = {**case.environment_recipe.frozen, "INVOCATION_ID": "1" * 32}
    assert case.ready_environments == [expected]
    assert json.loads(case.environment_observation.read_text()) == dict(
        INVOCATION_ID="1" * 32, SYNTHETIC_VALUE="frozen-fixture-value",
        CBM_CACHE_DIR=case.config["cache"], CONTROL_ONLY=None)
    assert os.environ["SYNTHETIC_VALUE"] == "later-control-value"
    assert os.environ["CONTROL_ONLY"] == "scope-control-value"
    status = queue.execution_status(case.claimed["context"])
    assert status["terminal"]["data"]["provider_ok"] is True
    assert status["native_intent"]["source"] == status["launch_reservation"]["source"]


@pytest.mark.parametrize("queued", [False, True])
def test_accepted_open_inode_executes_after_named_binary_replacement(bound, queue, tmp_path, queued):
    case = bound()
    original = case.managed["verified_binary"]
    wrong_image = tmp_path / "wrong-image-executed"
    replacement = tmp_path / "replacement-image"
    replacement.write_text("#!/usr/bin/python3\nfrom pathlib import Path\nPath("
                           + repr(str(wrong_image)) + ").write_text('wrong')\nraise SystemExit(93)\n")
    replacement.chmod(0o700)
    accepted = []

    def replace_after_verified_open(path):
        stream = original(path)
        metadata = os.fstat(stream.fileno())
        accepted.append(dict(device=metadata.st_dev, inode=metadata.st_ino, sha256=worker.BUILD))
        replacement.replace(path)
        assert (path.stat().st_dev, path.stat().st_ino) != (metadata.st_dev, metadata.st_ino)
        return stream

    case.managed["verified_binary"] = replace_after_verified_open
    factory = (case.queued["cbm_factory"](case.snapshot, case.modules, case.claimed["context"], case.policy)
               if queued else None)
    assert worker._execute_stock_worker(tmp_path / "settings", case.args, 8 * 1024**3,
                                        queued_factory=factory) == 0
    assert len(accepted) == 1 and not wrong_image.exists()
    if queued:
        status = queue.execution_status(case.claimed["context"])
        assert status["native_intent"]["executable"] == accepted[0]
        assert status["terminal"]["data"]["provider_ok"] is True
        assert status["terminal"]["data"]["report"]["payload_returncode"] == 0


@pytest.mark.parametrize("actual", [None, "2" * 32, "invalid"])
def test_recipe_provenance_refuses_before_readiness_and_native_child(bound, queue, tmp_path, monkeypatch, actual):
    case = bound(frozen_environment=True)
    if actual is None:
        monkeypatch.delenv("INVOCATION_ID", raising=False)
    else:
        monkeypatch.setenv("INVOCATION_ID", actual)
    factory = case.queued["cbm_factory"](case.snapshot, case.modules, case.claimed["context"], case.policy,
                                          environment_recipe=case.environment_recipe)
    with pytest.raises(ValueError, match="invocation"):
        worker._execute_stock_worker(tmp_path / "settings", case.args, 8 * 1024**3,
                                     queued_factory=factory, queued_native_environment=case.environment_recipe)
    assert case.calls == ["registration"] and not case.environment_observation.exists()
    status = queue.execution_status(case.claimed["context"])
    assert status["native_intent"] is None
    assert status["terminal"]["data"]["report"]["payload_started"] is False


@pytest.mark.parametrize("key", ["CBM_CACHE_DIR", "CBM_RUNTIME_DIR", "CBM_ALLOWED_ROOT", "CBM_UNSUPPORTED_OPTION"])
def test_frozen_provider_paths_must_match_current_validated_settings(bound, queue, tmp_path, key):
    case = bound(frozen_environment=True, path_fault=key)
    factory = case.queued["cbm_factory"](case.snapshot, case.modules, case.claimed["context"], case.policy,
                                          environment_recipe=case.environment_recipe)
    with pytest.raises(ValueError, match="environment|path"):
        worker._execute_stock_worker(tmp_path / "settings", case.args, 8 * 1024**3,
                                     queued_factory=factory, queued_native_environment=case.environment_recipe)
    assert not case.calls and not case.environment_observation.exists()
    assert queue.execution_status(case.claimed["context"])["scope_registration"] is None


@pytest.mark.parametrize("error", [False, True])
def test_real_factory_binds_admission_before_actual_child_and_terminal(bound, queue, tmp_path, error):
    case = bound(error=error)
    factory = case.queued["cbm_factory"](case.snapshot, case.modules, case.claimed["context"], case.policy)
    if error:
        with pytest.raises(ValueError, match="worker returned"):
            worker._execute_stock_worker(tmp_path / "settings", case.args, 8 * 1024**3, queued_factory=factory)
    else:
        assert worker._execute_stock_worker(tmp_path / "settings", case.args, 8 * 1024**3, queued_factory=factory) == 0
    assert case.calls == ["registration", "ready", "admission"]
    status = queue.execution_status(case.claimed["context"])
    assert status["native_intent"]["source"] == status["launch_reservation"]["source"]
    assert status["terminal"]["data"]["provider_ok"] is (not error)
    assert queue.apply_native(case.claimed["context"]) == ("pending" if error else "consumed")


@pytest.mark.parametrize("change", ["mode", "persistence", "settings", "environment", "cache", "wall", "reserve", "source"])
def test_effective_drift_refuses_before_registration_or_preparation(bound, queue, tmp_path, change):
    case = bound()
    if change == "mode":
        case.args.mode = "fast"
    elif change == "persistence":
        case.args.persistence = "true"
    elif change == "settings":
        old = case.managed["runtime_config"]
        def changed(path, *, capture=None):
            result = old(path)
            if capture:
                capture(case.raw + b" ")
            return result
        case.managed["runtime_config"] = changed
    elif change == "environment":
        case.environment["PATH"] += ":/fixture/other"
    elif change == "cache":
        case.config["cache"] += "/changed"
    elif change in ("wall", "reserve"):
        case.policy[{"wall": "wall_seconds", "reserve": "sibling_reserve_bytes"}[change]] += 1
    else:
        case.snapshot.current_matches = lambda: False
    factory = case.queued["cbm_factory"](case.snapshot, case.modules, case.claimed["context"], case.policy)
    with pytest.raises((ValueError, RuntimeError)):
        worker._execute_stock_worker(tmp_path / "settings", case.args, 8 * 1024**3, queued_factory=factory)
    status = queue.execution_status(case.claimed["context"])
    assert status["scope_registration"] is None and status["native_intent"] is None
    assert case.calls == []


@pytest.mark.parametrize("change", ["source", "arguments"])
def test_post_preparation_drift_refuses_actual_native_spawn(bound, queue, tmp_path, change):
    case = bound()
    def ready(config):
        case.calls.append("ready")
        if change == "source":
            case.snapshot.current_matches = lambda: False
        else:
            case.args.mode = "fast"
    case.managed["ready"] = ready
    factory = case.queued["cbm_factory"](case.snapshot, case.modules, case.claimed["context"], case.policy)
    assert worker._execute_stock_worker(tmp_path / "settings", case.args, 8 * 1024**3, queued_factory=factory) == 111
    status = queue.execution_status(case.claimed["context"])
    assert status["native_intent"] is None
    assert not status["terminal"]["data"]["report"]["payload_started"]
    assert queue.apply_native(case.claimed["context"]) == "pending"
    assert queue.execution_status(case.claimed["context"])["typed_charged_attempts"] == 0
