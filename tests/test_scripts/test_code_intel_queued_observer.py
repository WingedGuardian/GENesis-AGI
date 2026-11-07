"""Queued hooks surround the actual child; standalone evidence stays compatible."""

import os
import sys
import time
from copy import deepcopy
from types import SimpleNamespace

import pytest

from tests.test_scripts.test_code_intel_execution import evidence as evidence
from tests.test_scripts.test_code_intel_execution import scope as scope
from tests.test_scripts.test_index_marker_execution import _claim, _reservation
from tests.test_scripts.test_index_marker_legs import queue as queue


def launch_options(evidence, baseline, **changes):
    constructor = evidence.get("QueuedLaunch", SimpleNamespace)
    values = dict(baseline=baseline, before_spawn=lambda facts: "started",
                  after_spawn=lambda: None, pass_fds=(), env=None, cwd=None)
    return constructor(**{**values, **changes})


@pytest.mark.parametrize("answer", ["already_started", None, False, ""])
def test_only_observed_first_intent_authorizes_the_actual_child(scope, evidence, tmp_path, answer):
    probe, leaf, proc = scope
    output = tmp_path / "must-not-exist"
    launch = launch_options(evidence, probe.capture(), before_spawn=lambda facts: answer)
    report = evidence["observe"](probe, [sys.executable, "-I", "-c",
                                       "import pathlib,sys;pathlib.Path(sys.argv[1]).touch()", str(output)],
                                 seconds=1, launch=launch)
    assert report["outcome"] == "refused" and report["payload_started"] is False
    assert not output.exists()


def test_gate_failure_does_not_create_a_child(scope, evidence, tmp_path):
    probe, leaf, proc = scope
    output = tmp_path / "must-not-exist"

    def refuse(facts):
        raise ValueError("durable intent not acknowledged")

    report = evidence["observe"](probe, [sys.executable, "-I", "-c",
                                       "import pathlib,sys;pathlib.Path(sys.argv[1]).touch()", str(output)],
                                 seconds=1, launch=launch_options(evidence, probe.capture(), before_spawn=refuse))
    assert report["outcome"] == "refused" and not output.exists()


@pytest.mark.parametrize('message', ['native launch refused: resource', 'unexpected callback defect'])
def test_callback_runtime_error_refuses_without_hiding_its_diagnostic(scope, evidence, tmp_path, message):
    probe, _, _ = scope
    output = tmp_path / 'must-not-exist'

    def refuse(facts):
        raise RuntimeError(message)

    launch = launch_options(evidence, probe.capture(), before_spawn=refuse)
    report = evidence['observe'](probe, [sys.executable, '-I', '-c',
        'import pathlib,sys;pathlib.Path(sys.argv[1]).touch()', str(output)], seconds=1, launch=launch)
    assert report['outcome'] == 'refused' and report['payload_started'] is False
    assert not output.exists()
    assert launch.diagnostics == ['native_intent_callback_failed: RuntimeError: ' + message]


@pytest.mark.parametrize('boundary', ['capture', 'popen'])
def test_runtime_error_outside_permission_callback_still_propagates(scope, evidence, monkeypatch, boundary):
    probe, _, _ = scope
    launch = launch_options(evidence, probe.capture())

    def fail(*args, **kwargs):
        raise RuntimeError('unexpected observer failure')

    if boundary == 'capture':
        monkeypatch.setattr(probe, 'capture', fail)
    else:
        monkeypatch.setattr(evidence['subprocess'], 'Popen', fail)
    with pytest.raises(RuntimeError, match='unexpected observer failure'):
        evidence['observe'](probe, ['harmless-never-created-child'], seconds=1, launch=launch)
    assert launch.diagnostics == []


@pytest.mark.parametrize('reason', ['resource', 'held'])
def test_real_journal_gate_refusal_preserves_hold_without_starting_child(scope, evidence, queue, tmp_path, reason):
    probe, leaf, proc = scope
    claimed = _claim(queue, tmp_path)
    context, reserved = claimed['context'], _reservation(claimed)
    unit = reserved['unit']
    leaf.rename(leaf.with_name(unit + '.scope'))
    leaf = leaf.with_name(unit + '.scope')
    membership = f'0::/genesis-workload.slice/{unit}.scope\n'
    (proc / 'self/cgroup').write_text(membership)
    (proc / 'self/task' / str(os.getpid()) / 'cgroup').write_text(membership)
    probe.unit = unit
    baseline = probe.capture()
    registered = {'unit': unit, 'invocation_id': '1' * 32, 'facts': baseline, 'stop_usec': 90_000_000}
    assert queue.reserve_launch(context, reserved) == 'reserved'
    assert queue.register_scope(context, registered) == 'registered'
    if reason == 'resource':
        (leaf / 'memory.events').write_text('low 0\nhigh 0\nmax 0\noom 1\noom_kill 1\n')
    else:
        with pytest.raises(RuntimeError, match='native reservation refused: conflict'):
            queue.reserve_launch(context, {**reserved, 'wall_seconds': reserved['wall_seconds'] - 1})

    def committed(facts):
        return queue.begin_native(context, {'source': reserved['source'],
            'executable': {'device': 7, 'inode': 88, 'sha256': 'f' * 64}, 'facts': facts,
            'registration_sha256': queue._native.digest(registered), 'before_ns': time.monotonic_ns()})

    output = tmp_path / 'must-not-exist'
    released = []
    launch = launch_options(evidence, baseline, before_spawn=committed, after_spawn=lambda: released.append(True))
    report = evidence['observe'](probe, [sys.executable, '-I', '-c',
        'import pathlib,sys;pathlib.Path(sys.argv[1]).touch()', str(output)], seconds=3, launch=launch)
    assert report['outcome'] == 'refused' and report['payload_started'] is False
    assert not output.exists() and released == []
    assert launch.diagnostics == ['native_intent_callback_failed: RuntimeError: native launch refused: ' + reason]
    status = queue.execution_status(context)
    assert status['safety_kind'] == ('resource' if reason == 'resource' else 'conflict')
    assert status['native_intent'] is None and status['completion_kind'] is None
    assert status['attempts'] == 0 and status['typed_charged_attempts'] is None
    assert status['typed_applied_state'] is None
    assert status['launch_permission'] == 'reserved'
    assert queue.get_inflight(context['hash'])['claim_id'] == context['claim_id']
    assert {row['provider']: row['attempts'] for row in queue.list_markers()} == {'gitnexus': 0}


def test_expired_observation_after_intent_does_not_spawn(scope, evidence, monkeypatch):
    probe, leaf, proc = scope
    clock = [100.0]
    starts = []

    def committed(facts):
        clock[0] = 102.0
        return "started"

    def spawn(*args, **kwargs):
        starts.append(args)
        return SimpleNamespace(poll=lambda: 0)

    monkeypatch.setitem(evidence["observe"].__globals__, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(evidence["subprocess"], "Popen", spawn)
    report = evidence["observe"](probe, ["harmless-fake-child"], seconds=1,
                                 launch=launch_options(evidence, probe.capture(), before_spawn=committed))
    assert report["outcome"] == "refused" and report["payload_started"] is False
    assert starts == []


@pytest.mark.parametrize("fault", ["identity", "counter-reset", "other-task"])
def test_registered_baseline_is_checked_before_intent(scope, evidence, monkeypatch, fault):
    probe, leaf, proc = scope
    baseline = deepcopy(probe.capture())
    if fault == "identity":
        baseline["leaf"]["inode"] += 1
    elif fault == "counter-reset":
        baseline["events"]["high"] = 1
    else:
        (leaf / "pids.current").write_text("2")
    starts = []

    def spawn(*args, **kwargs):
        starts.append(args)
        return SimpleNamespace(poll=lambda: 0)

    monkeypatch.setattr(evidence["subprocess"], "Popen", spawn)
    permits = []
    launch = launch_options(evidence, baseline, before_spawn=lambda facts: permits.append(facts) or "started")
    report = evidence["observe"](probe, ["harmless-fake-child"], seconds=1, launch=launch)
    assert report["outcome"] == "refused"
    assert starts == permits == []


def test_native_child_uses_accepted_descriptor_and_owned_wait(scope, evidence, tmp_path):
    probe, leaf, proc = scope
    baseline = probe.capture()
    (leaf / "memory.events").write_text("low 0\nhigh 1\nmax 0\noom 0\noom_kill 0\n")
    accepted = tmp_path / "accepted"
    accepted.write_text("accepted inode")
    output = tmp_path / "answer"
    order = []

    def gate(facts):
        assert facts["events"]["high"] == 1
        order.append("intent-committed")
        return "started"

    with accepted.open() as descriptor:
        launch = launch_options(evidence, baseline, before_spawn=gate,
                                after_spawn=lambda: order.append("admission-released"),
                                pass_fds=(descriptor.fileno(),), env={**os.environ, "OWNED_PROBE": "bound"},
                                cwd=str(tmp_path))
        command = [sys.executable, "-I", "-c",
                   "import os,pathlib,sys;assert os.environ['OWNED_PROBE']=='bound';"
                   "assert pathlib.Path.cwd()==pathlib.Path(sys.argv[3]);"
                   "pathlib.Path(sys.argv[2]).write_text(os.read(int(sys.argv[1]),64).decode())",
                   str(descriptor.fileno()), str(output), str(tmp_path)]
        report = evidence["observe"](probe, command, seconds=3, launch=launch)
    assert report["outcome"] == "complete" and report["payload_returncode"] == 0
    # The terminal binds the exact fresh intent sample; the separately retained
    # registration baseline covers preparation and must not be rewritten.
    assert report["before"]["events"]["high"] == report["after"]["events"]["high"] == 1
    assert baseline["events"]["high"] == 0
    assert order == ["intent-committed", "admission-released"]
    assert output.read_text() == "accepted inode"
    assert evidence["validate_report"](report) == report


@pytest.mark.parametrize("failure", [OSError, ValueError, RuntimeError])
def test_post_spawn_failure_keeps_completed_work_and_reaps_it(scope, evidence, tmp_path, monkeypatch, failure):
    probe, leaf, proc = scope
    actual = evidence["subprocess"].Popen
    children = []

    def start(*args, **kwargs):
        child = actual(*args, **kwargs)
        children.append(child)
        return child

    def failed_release():
        raise failure("owned admission release failed")

    monkeypatch.setattr(evidence["subprocess"], "Popen", start)
    launch = launch_options(evidence, probe.capture(), after_spawn=failed_release)
    report = evidence["observe"](probe, [sys.executable, "-I", "-c", "pass"], seconds=3,
                                 launch=launch)
    assert report["outcome"] == "complete" and report["payload_started"] is True
    assert report["payload_returncode"] == 0 and report["error"] is None
    assert launch.diagnostics == [f"admission_release_failed: {failure.__name__}: owned admission release failed"]
    assert len(children) == 1
    with pytest.raises(ChildProcessError):
        os.waitpid(children[0].pid, os.WNOHANG)


def test_release_error_preserves_real_journal_completion_and_never_resends(scope, evidence, queue, tmp_path):
    probe, leaf, proc = scope
    claimed = _claim(queue, tmp_path)
    context = claimed["context"]
    reserved = _reservation(claimed)
    unit = reserved["unit"]
    destination = leaf.with_name(unit + ".scope")
    leaf.rename(destination)
    membership = f"0::/genesis-workload.slice/{unit}.scope\n"
    (proc / "self/cgroup").write_text(membership)
    (proc / "self/task" / str(os.getpid()) / "cgroup").write_text(membership)
    probe.unit = unit
    baseline = probe.capture()
    registered = {"unit": unit, "invocation_id": "1" * 32, "facts": baseline, "stop_usec": 90_000_000}
    assert queue.reserve_launch(context, reserved) == "reserved"
    assert queue.register_scope(context, registered) == "registered"
    intent = []

    def committed(facts):
        if not intent:
            intent.append({"source": reserved["source"],
                           "executable": {"device": 7, "inode": 88, "sha256": "f" * 64},
                           "facts": facts, "registration_sha256": queue._native.digest(registered),
                           "before_ns": time.monotonic_ns()})
        return queue.begin_native(context, intent[0])

    def release_error():
        raise OSError("owned admission release failed")

    output = tmp_path / "published-invocations"
    command = [sys.executable, "-I", "-c",
               "import pathlib,sys;p=pathlib.Path(sys.argv[1]);"
               "p.write_text((p.read_text() if p.exists() else '')+'one\\n')", str(output)]
    launch = launch_options(evidence, baseline, before_spawn=committed, after_spawn=release_error)
    report = evidence["observe"](probe, command, seconds=3, launch=launch)
    assert output.read_text() == "one\n"  # Actual toy publication, not an inferred rc.
    queue.remember_native_event(context, "terminal", {"report": report, "provider_ok": True,
                                "source": reserved["source"], "sealed_ns": time.monotonic_ns()})
    assert queue.apply_native(context) == "consumed"
    assert queue.apply_native(context) == "consumed"
    # The reporter's administrative failure and still-live ownership can be
    # retained independently after completion; neither may resend known work.
    queue.remember_native_event(context, "cleanup", {"state": "unresolved",
                                "registration_sha256": queue._native.digest(registered),
                                "facts": probe.capture(), "populated": 1, "owned_wait": True,
                                "observed_ns": time.monotonic_ns()})
    assert queue.apply_native(context) == "consumed"
    status = queue.execution_status(context)
    assert status["completion_kind"] == "success" and status["safety_kind"] == "uncertainty"
    assert queue.get_inflight(context["hash"])["claim_id"] == context["claim_id"]
    assert launch.diagnostics == ["admission_release_failed: OSError: owned admission release failed"]
    replay = evidence["observe"](probe, command, seconds=3, launch=launch)
    assert replay["outcome"] == "refused" and replay["payload_started"] is False
    assert output.read_text() == "one\n"
