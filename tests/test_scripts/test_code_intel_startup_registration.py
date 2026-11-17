"""Fresh registration preserves the owner baseline; startup OOM never prepares native."""

from copy import deepcopy

import pytest

from tests.test_scripts.test_code_intel_execution import evidence as evidence
from tests.test_scripts.test_code_intel_execution import scope as scope
from tests.test_scripts.test_code_intel_queued_session import command
from tests.test_scripts.test_code_intel_queued_session import session as session
from tests.test_scripts.test_index_marker_legs import queue as queue


def _bound(session, observer=None):
    run, claimed, reserved, program, source, order, leaf, namespace = session()
    early = run.manager.registration(reserved["unit"], run.probe.capture(), wall_seconds=reserved["wall_seconds"])
    order.clear()
    operation = getattr(run, "bind_startup", None)
    assert callable(operation), "queued registration must consume the original startup witness"
    observations = []
    operation(early, observer or observations.append)
    return run, claimed, reserved, program, early, observations, leaf


def test_fresh_counters_do_not_replace_original_baseline_and_actual_native_still_runs(session, queue, tmp_path):
    run, claimed, reserved, program, early, observed, leaf = _bound(session)
    (leaf / "memory.events").write_text("low 0\nhigh 1\nmax 0\noom 0\noom_kill 0\n")
    assert run.register() == early
    status = queue.execution_status(claimed["context"])
    assert status["scope_registration"] == early
    assert observed[-1]["facts"]["events"]["high"] == 1
    assert observed[-1]["scope_sha256"] == queue._native.digest(early)
    assert observed[-1]["reservation_sha256"] == queue._native.digest(reserved)
    marker = tmp_path / "once"
    with program.open("rb") as executable:
        report = run.native(command(executable.fileno(), marker), executable,
                            after_spawn=lambda: None, pass_fds=(executable.fileno(),))
    assert report["outcome"] == "complete" and marker.read_text() == "once\n"
    run.terminal(provider_ok=True)
    assert queue.execution_status(claimed["context"])["completion_kind"] == "success"


@pytest.mark.parametrize("counter,filename", [("events", "memory.events"), ("local_events", "memory.events.local")])
def test_positive_fresh_startup_counter_is_reported_before_refusing_registration_and_native(session, queue, tmp_path, counter, filename):
    run, claimed, reserved, program, early, observed, leaf = _bound(session)
    (leaf / filename).write_text("low 0\nhigh 0\nmax 1\noom 1\noom_kill 1\n")
    with pytest.raises(ValueError, match="startup.*OOM"):
        run.register()
    assert observed[-1]["facts"][counter]["oom_kill"] == 1
    assert observed[-1]["scope_sha256"] == queue._native.digest(early)
    status = queue.execution_status(claimed["context"])
    assert status["scope_registration"] is None and status["native_intent"] is None
    marker = tmp_path / "must-not-start"
    with program.open("rb") as executable, pytest.raises(RuntimeError, match="registration"):
        run.native(command(executable.fileno(), marker), executable,
                   after_spawn=lambda: None, pass_fds=(executable.fileno(),))
    assert not marker.exists()


@pytest.mark.parametrize("fault", ["invocation", "leaf"])
def test_fresh_lifetime_replacement_never_registers_or_relays_it_as_original(session, queue, fault):
    run, claimed, reserved, program, early, observed, leaf = _bound(session)
    original = run.manager.registration

    def replaced(*args, **kwargs):
        result = deepcopy(original(*args, **kwargs))
        if fault == "invocation":
            result["invocation_id"] = "2" * 32
        else:
            result["facts"]["leaf"]["inode"] += 1
        return result

    run.manager.registration = replaced
    with pytest.raises(ValueError):
        run.register()
    assert not observed and queue.execution_status(claimed["context"])["scope_registration"] is None


def test_failed_startup_observation_delivery_refuses_before_queue_registration(session, queue):
    def lost(_observation):
        raise BrokenPipeError("synthetic owner-output failure")

    run, claimed, reserved, program, early, observed, leaf = _bound(session, lost)
    with pytest.raises(BrokenPipeError):
        run.register()
    status = queue.execution_status(claimed["context"])
    assert status["scope_registration"] is None and status["native_intent"] is None


@pytest.mark.parametrize("fault", ["unit", "budget", "cap", "boot", "shape", "observer"])
def test_initial_binding_validates_full_reservation_and_observer(session, fault):
    run, claimed, reserved, program, source, order, leaf, namespace = session()
    early = run.manager.registration(reserved["unit"], run.probe.capture(), wall_seconds=reserved["wall_seconds"])
    def observer(value):
        return None
    if fault == "unit":
        early["unit"] = "another-unit"
    elif fault == "budget":
        early["stop_usec"] = reserved["wall_seconds"] * 1_000_000
    elif fault == "cap":
        early["facts"]["memory_max"] = 1
    elif fault == "boot":
        early["facts"]["reporter"]["boot_id"] = "22222222-2222-4222-8222-222222222222"
    elif fault == "shape":
        early["unknown"] = True
    else:
        observer = None
    with pytest.raises(ValueError):
        run.bind_startup(early, observer)
    assert run.registration is None


def test_binding_is_defensively_copied_and_cannot_be_replaced(session):
    run, claimed, reserved, program, early, observed, leaf = _bound(session)
    expected = deepcopy(early)
    early["invocation_id"] = "2" * 32
    with pytest.raises(RuntimeError, match="single-use"):
        run.bind_startup(early, observed.append)
    assert run.register() == expected
    with pytest.raises(RuntimeError, match="single-use"):
        run.bind_startup(expected, observed.append)


def test_fresh_counter_reset_is_not_a_new_original_baseline(session, queue):
    run, claimed, reserved, program, source, order, leaf, namespace = session()
    (leaf / "memory.events").write_text("low 0\nhigh 1\nmax 0\noom 0\noom_kill 0\n")
    early = run.manager.registration(reserved["unit"], run.probe.capture(), wall_seconds=reserved["wall_seconds"])
    observed = []
    run.bind_startup(early, observed.append)
    (leaf / "memory.events").write_text("low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n")
    with pytest.raises(ValueError, match="counter reset"):
        run.register()
    assert not observed and queue.execution_status(claimed["context"])["scope_registration"] is None


@pytest.mark.parametrize("counter,filename", [("events", "memory.events"), ("local_events", "memory.events.local")])
def test_positive_first_sample_remains_owned_oom_even_without_a_new_delta(session, queue, counter, filename):
    run, claimed, reserved, program, source, order, leaf, namespace = session()
    (leaf / filename).write_text("low 0\nhigh 0\nmax 1\noom 1\noom_kill 1\n")
    early = run.manager.registration(reserved["unit"], run.probe.capture(), wall_seconds=reserved["wall_seconds"])
    observed = []
    run.bind_startup(early, observed.append)
    with pytest.raises(ValueError, match="startup.*OOM"):
        run.register()
    assert observed[-1]["facts"][counter]["oom_kill"] == 1
    assert queue.execution_status(claimed["context"])["scope_registration"] is None


@pytest.mark.parametrize("fault", ["invocation", "manager-unavailable"])
def test_manager_is_revalidated_after_preparation_before_native_intent(session, queue, tmp_path, fault):
    run, claimed, reserved, program, early, observed, leaf = _bound(session)
    run.register()
    original = run.manager.registration

    def changed(*args, **kwargs):
        if fault == "manager-unavailable":
            raise ValueError("synthetic lost original manager")
        value = original(*args, **kwargs)
        return {**value, "invocation_id": "2" * 32}

    run.manager.registration = changed
    marker = tmp_path / "must-not-start-after-manager-drift"
    with program.open("rb") as executable:
        report = run.native(command(executable.fileno(), marker), executable,
                            after_spawn=lambda: None, pass_fds=(executable.fileno(),))
    assert not report["payload_started"] and not marker.exists()
    assert queue.execution_status(claimed["context"])["native_intent"] is None


def test_live_startup_guard_is_rechecked_before_registration_and_native_intent(session, queue, tmp_path):
    run, claimed, reserved, program, source, order, leaf, namespace = session()
    early = run.manager.registration(reserved['unit'], run.probe.capture(), wall_seconds=reserved['wall_seconds'])
    checks = []
    def live():
        checks.append('verify')
        if len(checks) > 1:
            raise ValueError('synthetic original owner died after preparation')
    run.bind_startup(early, lambda observation: None, verify=live)
    run.register()
    marker = tmp_path / 'must-not-start-after-parent-loss'
    with program.open('rb') as executable:
        report = run.native(command(executable.fileno(), marker), executable,
                            after_spawn=lambda: None, pass_fds=(executable.fileno(),))
    assert checks == ['verify', 'verify']
    assert not report['payload_started'] and not marker.exists()
    assert queue.execution_status(claimed['context'])['native_intent'] is None


def test_live_startup_guard_can_refuse_before_queue_registration(session, queue):
    run, claimed, reserved, program, source, order, leaf, namespace = session()
    early = run.manager.registration(reserved['unit'], run.probe.capture(), wall_seconds=reserved['wall_seconds'])
    def refuse():
        raise ValueError('synthetic parent unavailable')
    run.bind_startup(early, lambda observation: None, verify=refuse)
    with pytest.raises(ValueError, match='parent unavailable'):
        run.register()
    assert queue.execution_status(claimed['context'])['scope_registration'] is None
