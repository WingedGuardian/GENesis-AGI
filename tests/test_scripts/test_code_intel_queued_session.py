"""Actual owned toy publication through the shared queued reporter and SQLite."""

import hashlib
import os
import runpy
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_scripts.test_code_intel_execution import evidence as evidence
from tests.test_scripts.test_code_intel_execution import scope as scope
from tests.test_scripts.test_index_marker_execution import _claim, _reservation
from tests.test_scripts.test_index_marker_legs import queue as queue

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def session(queue, scope, evidence, tmp_path):
    def prepare():
        path = ROOT / "scripts/lib/code_intel_queued_worker.py"
        assert path.is_file(), "queued producer needs the shared actual native-session seam"
        namespace = runpy.run_path(str(path))
        claimed = _claim(queue, tmp_path)
        reserved = _reservation(claimed)
        probe, leaf, proc = scope
        unit = reserved["unit"]
        leaf = leaf.rename(leaf.with_name(unit + ".scope"))
        membership = f"0::/genesis-workload.slice/{unit}.scope\n"
        (proc / "self/cgroup").write_text(membership)
        (proc / "self/task" / str(os.getpid()) / "cgroup").write_text(membership)
        (proc / "sys/kernel/random/boot_id").write_text(reserved["owner"]["boot_id"])
        (leaf / "cgroup.events").write_text("populated 1\n")
        probe.unit = unit
        program = Path(sys.executable).resolve()
        reserved["source"]["provider_digest"] = hashlib.sha256(program.read_bytes()).hexdigest()
        source = deepcopy(reserved["source"])
        assert queue.reserve_launch(claimed["context"], reserved) == "reserved"
        order = []

        def registration(unit, facts, *, wall_seconds):
            order.append("registration")
            assert unit == reserved["unit"] and wall_seconds == reserved["wall_seconds"]
            return dict(unit=unit, invocation_id="1" * 32, facts=facts, stop_usec=90_000_000)

        manager = SimpleNamespace(registration=registration)
        run = namespace["QueuedRun"](queue, SimpleNamespace(**evidence), probe, manager, claimed["context"],
                                      source=lambda: deepcopy(source), admit=lambda: order.append("admission"))
        return run, claimed, reserved, program, source, order, leaf, namespace
    return prepare


def command(fd, output):
    return [f"/proc/self/fd/{fd}", "-I", "-c", "import pathlib,sys;p=pathlib.Path(sys.argv[1]);"
            "p.write_text((p.read_text() if p.exists() else '')+'once\\n')", str(output)]


@pytest.mark.parametrize("counter", ["events", "local_events", "both"])
def test_first_positive_owned_oom_never_registers_or_spends_intent(session, queue, counter):
    run, claimed, reserved, program, source, order, leaf, namespace = session()
    before = queue.execution_status(claimed["context"])
    for key, target in (("events", "memory.events"), ("local_events", "memory.events.local")):
        if counter in (key, "both"):
            (leaf / target).write_text("low 0\nhigh 0\nmax 1\noom 1\noom_kill 1\n")
    with pytest.raises(ValueError, match="startup.*owned OOM"):
        run.register()
    status = queue.execution_status(claimed["context"])
    assert run.registration is run.report is run.launch is None
    assert status["launch_reservation"] == reserved
    assert status["scope_registration"] is status["native_intent"] is status["terminal"] is None
    assert status["launch_permission"] == "reserved"
    assert status == before, "startup refusal must preserve all accounting and reservation state"
    assert order == ["registration"]


def test_ancestor_oom_does_not_become_owned_startup_oom(session):
    run, claimed, reserved, program, source, order, leaf, namespace = session()
    (leaf.parent / "memory.events").write_text("low 0\nhigh 0\nmax 1\noom 1\noom_kill 1\n")
    registration = run.register()
    assert registration["facts"]["ancestors"][0]["events"]["oom_kill"] == 1
    assert run.registration == registration and run.report is run.launch is None


@pytest.mark.parametrize("counter", ["high", "max", "oom"])
def test_nonkill_memory_events_do_not_become_owned_startup_oom(session, counter):
    run, claimed, reserved, program, source, order, leaf, namespace = session()
    counters = dict(low=0, high=0, max=0, oom=0, oom_kill=0)
    counters[counter] = 1
    (leaf / "memory.events").write_text("".join(f"{key} {value}\n" for key, value in counters.items()))
    assert run.register()["facts"]["events"] == counters
    assert run.report is run.launch is None


def test_register_intent_actual_child_terminal_and_replay_are_one_use(session, queue, tmp_path):
    run, claimed, reserved, program, source, order, leaf, namespace = session()
    assert run.register()["facts"] == run.probe.capture()
    output = tmp_path / "actual-publication"
    with program.open("rb") as executable:
        report = run.native(command(executable.fileno(), output), executable,
                            after_spawn=lambda: order.append("released"), pass_fds=(executable.fileno(),))
    assert report["outcome"] == "complete" and output.read_text() == "once\n"
    assert order == ["registration", "registration", "admission", "released"]
    run.terminal(provider_ok=True)
    assert queue.apply_native(claimed["context"]) == "consumed"
    assert queue.apply_native(claimed["context"]) == "consumed"
    status = queue.execution_status(claimed["context"])
    assert status["terminal"]["data"]["report"] == report
    assert status["completion_kind"] == "success" and status["safety_kind"] is None
    with program.open("rb") as executable:
        replay = run.native(command(executable.fileno(), output), executable,
                            after_spawn=lambda: None, pass_fds=(executable.fileno(),))
    assert not replay["payload_started"] and output.read_text() == "once\n"


@pytest.mark.parametrize("field", ["commit", "provider_digest", "configuration_sha256", "files"])
def test_loaded_source_or_configuration_drift_never_spawns(session, tmp_path, field):
    run, claimed, reserved, program, source, order, leaf, namespace = session()
    run.register()
    source[field] = {"scripts/different.py": "1" * 64} if field == "files" else "1" * len(source[field])
    output = tmp_path / "must-not-publish"
    with program.open("rb") as executable:
        report = run.native(command(executable.fileno(), output), executable,
                            after_spawn=lambda: None, pass_fds=(executable.fileno(),))
    assert report["outcome"] == "refused" and not output.exists()


def test_preparation_oom_refuses_indexing_and_preserves_zero_indexing_debt(session, queue, tmp_path):
    run, claimed, reserved, program, source, order, leaf, namespace = session()
    run.register()
    (leaf / "memory.events").write_text("low 0\nhigh 0\nmax 1\noom 1\noom_kill 1\n")
    output = tmp_path / "must-not-publish"
    with program.open("rb") as executable:
        report = run.native(command(executable.fileno(), output), executable,
                            after_spawn=lambda: None, pass_fds=(executable.fileno(),))
    assert not report["payload_started"] and not output.exists()
    run.terminal(provider_ok=False)
    assert queue.apply_native(claimed["context"]) == "pending"
    status = queue.execution_status(claimed["context"])
    assert status["safety_kind"] == "resource" and status["typed_charged_attempts"] == 0


def test_preparation_oom_is_retained_when_admission_refuses_before_begin(session, queue, tmp_path):
    run, claimed, reserved, program, source, order, leaf, namespace = session()
    run.register()
    (leaf / "memory.events").write_text("low 0\nhigh 0\nmax 1\noom 1\noom_kill 1\n")

    def refuse():
        raise ValueError("fresh headroom refused")

    run.admit = refuse
    with program.open("rb") as executable:
        report = run.native(command(executable.fileno(), tmp_path / "must-not-publish"), executable,
                            after_spawn=lambda: None, pass_fds=(executable.fileno(),))
    assert not report["payload_started"] and report["before"]["events"]["oom_kill"] == 1
    assert report["after"] is None
    run.terminal(provider_ok=False)
    assert queue.apply_native(claimed["context"]) == "pending"
    status = queue.execution_status(claimed["context"])
    assert status["safety_kind"] == "resource", "known preparation OOM survives an unrelated admission refusal"
    assert status["native_intent"] is None and status["typed_charged_attempts"] == 0


def test_unaccepted_executable_is_refused_despite_valid_source(session, tmp_path):
    run, claimed, reserved, program, source, order, leaf, namespace = session()
    run.register()
    replacement = tmp_path / "unaccepted"
    replacement.write_text("different executable")
    output = tmp_path / "must-not-publish"
    with replacement.open("rb") as executable:
        report = run.native(command(executable.fileno(), output), executable,
                            after_spawn=lambda: None, pass_fds=(executable.fileno(),))
    assert not report["payload_started"] and not output.exists()


@pytest.mark.parametrize("provider_ok", [True, False])
def test_response_validation_is_independent_of_truthful_native_zero(session, queue, tmp_path, provider_ok):
    run, claimed, reserved, program, source, order, leaf, namespace = session()
    run.register()
    with program.open("rb") as executable:
        report = run.native(command(executable.fileno(), tmp_path / "publication"), executable,
                            after_spawn=lambda: None, pass_fds=(executable.fileno(),))
    assert report["outcome"] == "complete" and report["payload_returncode"] == 0
    run.terminal(provider_ok=provider_ok)
    assert queue.apply_native(claimed["context"]) == ("consumed" if provider_ok else "pending")
    assert queue.execution_status(claimed["context"])["terminal"]["data"]["report"] == report


def test_release_failure_retains_completion_and_separate_uncertainty(session, queue, tmp_path):
    run, claimed, reserved, program, source, order, leaf, namespace = session()
    run.register()

    def release():
        raise OSError("synthetic admission release failure")

    with program.open("rb") as executable:
        report = run.native(command(executable.fileno(), tmp_path / "publication"), executable,
                            after_spawn=release, pass_fds=(executable.fileno(),))
    assert report["outcome"] == "complete"
    run.terminal(provider_ok=True)
    assert queue.apply_native(claimed["context"]) == "consumed"
    status = queue.execution_status(claimed["context"])
    assert status["completion_kind"] == "success" and status["safety_kind"] == "uncertainty"
    assert status["cleanup_first_failure"]["data"]["populated"] == 1


def test_unregistered_reporter_cannot_enter_native_seam(session, tmp_path):
    run, claimed, reserved, program, source, order, leaf, namespace = session()
    with program.open("rb") as executable, pytest.raises(RuntimeError, match="registration"):
        run.native(command(executable.fileno(), tmp_path / "must-not-publish"), executable,
                   after_spawn=lambda: None, pass_fds=(executable.fileno(),))


def test_terminal_cleanup_does_not_reopen_source_after_native_completion(session, queue, tmp_path, monkeypatch):
    run, claimed, reserved, program, source, order, leaf, namespace = session()
    run.register()

    def late_read(*args, **kwargs):
        raise RuntimeError('local source reopened after native completion')

    def release():
        raise OSError('synthetic release failure')

    with program.open('rb') as executable:
        report = run.native(command(executable.fileno(), tmp_path / 'publication'), executable,
                            after_spawn=release, pass_fds=(executable.fileno(),))
    assert report['outcome'] == 'complete'
    monkeypatch.setitem(run.terminal.__globals__, 'runpy', SimpleNamespace(run_path=late_read))
    run.terminal(provider_ok=True)
    assert queue.apply_native(claimed['context']) == 'consumed'
    status = queue.execution_status(claimed['context'])
    assert status['terminal']['data']['report'] == report
    assert status['safety_kind'] == 'uncertainty'
    assert status['cleanup_first_failure']['data']['populated'] == 1
