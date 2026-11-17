"""Explicit exact-row orphan reconciliation on real scratch SQL and kernel files."""

import runpy
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_scripts.test_code_intel_execution import evidence as evidence
from tests.test_scripts.test_code_intel_execution import scope as scope
from tests.test_scripts.test_index_marker_control import base_queue as base_queue
from tests.test_scripts.test_index_marker_control import queue as queue
from tests.test_scripts.test_index_marker_execution import _reservation, _source

ROOT = Path(__file__).resolve().parents[2]


def operation():
    path = ROOT / "scripts/lib/code_intel_supervisor.py"
    assert path.is_file(), "explicit orphan witness must be collected by captured supervision"
    return runpy.run_path(str(path))["reconcile_orphan"]


@pytest.fixture
def orphan_case(queue, scope, evidence):
    _, leaf, proc = scope
    repo = str(leaf.parents[2] / "repo")
    queue.write_marker(repo, "both", "fast")
    claimed = queue.claim_native(queue.marker_hash(repo), provider="cbm")
    reserved = _reservation(claimed)
    reserved["owner"].update(pid=999999, start_ticks=100)
    (proc / "sys/kernel/random/boot_id").write_text(reserved["owner"]["boot_id"])
    leaf = leaf.rename(leaf.with_name(reserved["unit"] + ".scope"))
    membership = "0::/genesis-workload.slice/" + leaf.name + "\n"
    (proc / "self/cgroup").write_text(membership)
    (proc / "self/task" / str(queue.os.getpid()) / "cgroup").write_text(membership)
    facts = evidence["ScopeProbe"](reserved["unit"], reserved["cap_bytes"], proc=proc).capture()
    facts["reporter"].update(pid=1234, start_ticks=100)
    registered = dict(
        unit=reserved["unit"], invocation_id="1" * 32, facts=facts, stop_usec=90_000_000
    )
    assert queue.reserve_launch(claimed["context"], reserved) == "reserved"
    collector = SimpleNamespace(**runpy.run_path(str(ROOT / "scripts/lib/code_intel_scope.py")))
    modules = dict(queue=queue, scope=collector, proof=SimpleNamespace(**evidence))
    return modules, claimed, reserved, registered, leaf, proc


def reconcile(case):
    modules, claimed, _, _, _, proc = case
    queue = modules["queue"]
    with queue._queue_gate(native=True) as db:
        return operation()(modules, claimed["context"], db, proc=proc)


def register(case, *, intent=False, success=False):
    modules, claimed, reserved, registered, _, _ = case
    queue = modules["queue"]
    assert queue.register_scope(claimed["context"], registered) == "registered"
    if intent:
        facts = registered["facts"]
        value = dict(
            source=_source(),
            executable=dict(device=7, inode=88, sha256="f" * 64),
            facts=facts,
            registration_sha256=queue._native.digest(registered),
            before_ns=time.monotonic_ns(),
        )
        assert queue.begin_native(claimed["context"], value) == "started"
        if success:
            report = dict(
                version=1,
                outcome="complete",
                payload_started=True,
                payload_returncode=0,
                before=facts,
                after=facts,
                finality=dict(threads=[1234], processes=[1234], pids_current=1),
                error=None,
            )
            queue.remember_native_event(
                claimed["context"],
                "terminal",
                dict(
                    source=reserved["source"],
                    sealed_ns=time.monotonic_ns(),
                    provider_ok=True,
                    report=report,
                ),
            )


def remove_group(case):
    leaf = case[4]
    for path in leaf.iterdir():
        path.unlink()
    leaf.rmdir()
    observer = leaf.parents[1] / "observer.scope"
    observer.mkdir()
    (case[5] / "self/cgroup").write_text("0::/observer.scope\n")


def test_unregistered_no_intent_structural_exception_is_neutral_and_held(orphan_case):
    modules, claimed, _, _, _, _ = orphan_case
    queue = modules["queue"]
    reconcile(orphan_case)
    status = queue.execution_status(claimed["context"])
    assert status["control_closure"]["data"]["state"] == "orphaned"
    assert status["launch_permission"] == "cancelled" and status["completion_kind"] == "unstarted"
    assert status["typed_charged_attempts"] == 0 and status["safety_kind"] == "uncertainty"
    assert status["cleanup_proof"] is None and status["terminal"] is None
    assert queue.get_inflight(claimed["hash"]) is None
    assert queue.claim_native(claimed["hash"], provider="cbm") is None
    assert queue.claim_native(claimed["hash"], provider="gitnexus") is not None


@pytest.mark.parametrize("pointer", ["original", "absent", "newer"])
def test_exact_unreserved_context_cannot_reconstruct_pointer_ownership(
    queue, evidence, tmp_path, pointer
):
    repo = str(tmp_path / "unreserved-repo")
    queue.write_marker(repo, "both", "fast")
    claimed = queue.claim_native(queue.marker_hash(repo), provider="cbm")
    current = None
    if pointer != "original":
        with closing(queue._connect()) as db:
            db.execute("DELETE FROM inflight")
        if pointer == "newer":
            current = queue.claim_native(claimed["hash"], provider="gitnexus")
            assert current is not None
    before = queue.execution_status(claimed["context"])
    modules = dict(queue=queue, scope=SimpleNamespace(), proof=SimpleNamespace(**evidence))
    if pointer == "original":
        with queue._queue_gate(native=True) as db:
            result = operation()(modules, claimed["context"], db)
        assert result["completion_kind"] == "unstarted" and result["typed_charged_attempts"] == 0
        assert result["control_closure"] is result["launch_reservation"] is None
        assert queue.get_inflight(claimed["hash"]) is None
    else:
        with pytest.raises(RuntimeError), queue._queue_gate(native=True) as db:
            operation()(modules, claimed["context"], db)
        assert queue.execution_status(claimed["context"]) == before
        if current is not None:
            assert queue.get_inflight(claimed["hash"])["claim_id"] == current["claim_id"]


@pytest.mark.parametrize(
    "intent,success,expected,debt",
    [(False, False, "unstarted", 0), (True, False, "unknown", 1), (True, True, "success", 0)],
)
def test_registered_removed_original_settles_once_without_resend(
    orphan_case, intent, success, expected, debt
):
    modules, claimed, _, _, _, _ = orphan_case
    queue = modules["queue"]
    register(orphan_case, intent=intent, success=success)
    remove_group(orphan_case)
    reconcile(orphan_case)
    before = queue.execution_status(claimed["context"])
    assert before["completion_kind"] == expected and before["typed_charged_attempts"] == debt
    assert before["control_closure"]["data"]["state"] == "orphaned"
    assert (
        before["safety_kind"] == "uncertainty"
        and before["cleanup_proof"]["data"]["state"] == "removed"
    )
    assert queue.get_inflight(claimed["hash"]) is None
    reconcile(orphan_case)
    assert queue.execution_status(claimed["context"]) == before
    assert queue.claim_native(claimed["hash"], provider="cbm") is None
    assert queue.get_failed(claimed["hash"], provider="cbm") is None


@pytest.mark.parametrize("population", [0, 1])
def test_original_existing_group_without_owned_wait_is_not_clean(orphan_case, population):
    modules, claimed, _, _, leaf, _ = orphan_case
    queue = modules["queue"]
    register(orphan_case, intent=True)
    (leaf / "cgroup.events").write_text(f"populated {population}\n")
    with pytest.raises(ValueError):
        reconcile(orphan_case)
    status = queue.execution_status(claimed["context"])
    assert status["control_closure"] is None and status["typed_applied_state"] is None
    assert queue.get_inflight(claimed["hash"]) is not None


def test_dead_owner_with_live_original_reporter_refuses(orphan_case):
    _, _, _, registered, _, proc = orphan_case
    register(orphan_case, intent=True)
    remove_group(orphan_case)
    reporter = registered["facts"]["reporter"]
    target = proc / str(reporter["pid"])
    target.mkdir()
    (target / "stat").write_text("1234 (still live) " + " ".join(["S", "1"] + ["0"] * 17 + ["100"]))
    with pytest.raises(ValueError):
        reconcile(orphan_case)


def test_unaccounted_missing_pointer_cannot_recreate_ownership(orphan_case):
    modules, claimed, _, _, _, _ = orphan_case
    queue = modules["queue"]
    with closing(queue._connect()) as db:
        db.execute("DELETE FROM inflight")
    with pytest.raises(RuntimeError):
        reconcile(orphan_case)
    assert queue.execution_status(claimed["context"])["control_closure"] is None


def test_matching_live_owner_is_not_reconciled(orphan_case):
    modules, claimed, reserved, _, _, proc = orphan_case
    target = proc / str(reserved["owner"]["pid"])
    target.mkdir()
    (target / "stat").write_text(
        "999999 (live owner) " + " ".join(["D", "1"] + ["0"] * 17 + ["100"])
    )
    with pytest.raises(ValueError, match="owner"):
        reconcile(orphan_case)
    assert modules["queue"].execution_status(claimed["context"])["control_closure"] is None


@pytest.mark.parametrize("role", ["owner", "reporter"])
@pytest.mark.parametrize("state", ["Z", "X", "x"])
@pytest.mark.parametrize("tasks", ["live-thread", "unavailable"])
def test_terminal_leader_cannot_close_control_or_release_original_pointer(
    orphan_case, role, state, tasks
):
    modules, claimed, reserved, registered, _, proc = orphan_case
    queue = modules["queue"]
    if role == "reporter":
        register(orphan_case, intent=True)
        remove_group(orphan_case)
    identity = reserved["owner"] if role == "owner" else registered["facts"]["reporter"]
    target = proc / str(identity["pid"])
    target.mkdir()
    (target / "stat").write_text(
        str(identity["pid"]) + " (terminal leader) "
        + " ".join([state, "1"] + ["0"] * 17 + [str(identity["start_ticks"])])
    )
    if tasks == "live-thread":
        thread = target / "task" / str(identity["pid"] + 1)
        thread.mkdir(parents=True)
        (thread / "stat").write_text(
            str(identity["pid"] + 1) + " (live worker) "
            + " ".join(["S", "1"] + ["0"] * 17 + ["101"])
        )
    before = queue.execution_status(claimed["context"])
    with pytest.raises(ValueError, match=role):
        reconcile(orphan_case)
    assert queue.execution_status(claimed["context"]) == before
    assert queue.get_inflight(claimed["hash"])["claim_id"] == claimed["claim_id"]


def test_reboot_closes_original_lifetime_without_comparing_old_monotonic_times(
    orphan_case, monkeypatch
):
    modules, claimed, _, _, _, proc = orphan_case
    queue = modules["queue"]
    register(orphan_case, intent=True)
    boot = "00000000-0000-0000-0000-000000000002"
    (proc / "sys/kernel/random/boot_id").write_text(boot)
    observer = queue._recovery.owner_identity()
    monkeypatch.setattr(queue._recovery, "owner_identity", lambda: {**observer, "boot_id": boot})
    reconcile(orphan_case)
    status = queue.execution_status(claimed["context"])
    assert status["cleanup_proof"]["data"]["state"] == "rebooted"
    assert status["control_closure"]["data"]["observer"]["boot_id"] == boot
    assert status["completion_kind"] == "unknown" and status["typed_charged_attempts"] == 1
    assert queue.get_inflight(claimed["hash"]) is None


@pytest.mark.parametrize("registered", [False, True])
def test_replay_uses_existing_witness_and_never_changes_a_newer_pointer(
    orphan_case, monkeypatch, registered
):
    modules, claimed, _, _, _, _ = orphan_case
    queue = modules["queue"]
    if registered:
        register(orphan_case, intent=True, success=True)
        remove_group(orphan_case)
    reconcile(orphan_case)
    before = queue.execution_status(claimed["context"])
    current = queue.claim_native(claimed["hash"], provider="gitnexus")
    assert current is not None

    def forbidden():
        raise AssertionError("lost-ACK recovery must not invent a new closure/time/observer")

    monkeypatch.setattr(queue._recovery, "owner_identity", forbidden)
    reconcile(orphan_case)
    assert queue.execution_status(claimed["context"]) == before
    assert queue.get_inflight(claimed["hash"])["claim_id"] == current["claim_id"]


def test_late_success_retains_unknown_accounting_and_never_requeues_old_work(orphan_case):
    modules, claimed, reserved, registered, _, _ = orphan_case
    queue = modules["queue"]
    register(orphan_case, intent=True)
    remove_group(orphan_case)
    reconcile(orphan_case)
    before = queue.execution_status(claimed["context"])
    report = dict(
        version=1,
        outcome="complete",
        payload_started=True,
        payload_returncode=0,
        before=registered["facts"],
        after=registered["facts"],
        finality=dict(threads=[1234], processes=[1234], pids_current=1),
        error=None,
    )
    queue.remember_native_event(
        claimed["context"],
        "terminal",
        dict(
            source=reserved["source"],
            sealed_ns=before["cleanup_proof"]["data"]["observed_ns"] - 1,
            provider_ok=True,
            report=report,
        ),
    )
    reconcile(orphan_case)
    after = queue.execution_status(claimed["context"])
    assert after["completion_kind"] == "unknown" and after["typed_charged_attempts"] == 1
    assert after["control_closure"] == before["control_closure"]
    assert after["terminal"]["data"]["provider_ok"] is True
    assert queue.get_inflight(claimed["hash"]) is None
    assert [r["attempts"] for r in queue.list_markers() if r["provider"] == "cbm"] == [1]


def test_already_applied_unknown_is_not_charged_again_when_control_closes(orphan_case):
    modules, claimed, _, registered, _, proc = orphan_case
    queue = modules["queue"]
    register(orphan_case, intent=True)
    remove_group(orphan_case)
    proof = modules["scope"].cleanup_observation(
        registered, queue._native.digest(registered), modules["proof"], owned_wait=False, proc=proc
    )
    queue.remember_native_event(claimed["context"], "cleanup", proof)
    assert queue.apply_native(claimed["context"]) == "pending"
    before = queue.execution_status(claimed["context"])
    assert queue.get_inflight(claimed["hash"]) is not None and before["control_closure"] is None
    reconcile(orphan_case)
    after = queue.execution_status(claimed["context"])
    assert after["completion_kind"] == before["completion_kind"] == "unknown"
    assert after["typed_charged_attempts"] == before["typed_charged_attempts"] == 1
    assert after["cleanup_proof"] == before["cleanup_proof"]
    assert queue.get_inflight(claimed["hash"]) is None


def test_reconciliation_rollback_keeps_original_control_and_accounting(orphan_case, monkeypatch):
    modules, claimed, _, _, _, _ = orphan_case
    queue = modules["queue"]
    register(orphan_case, intent=True)
    remove_group(orphan_case)
    before = queue.execution_status(claimed["context"])
    reducer = queue._native.reconcile_execution

    def failed(*args, **kwargs):
        raise OSError("injected failure after witness, before accounting")

    with monkeypatch.context() as patch:
        patch.setattr(queue._native, "reconcile_execution", failed)
        with pytest.raises(OSError, match="injected"):
            reconcile(orphan_case)
    assert queue.execution_status(claimed["context"]) == before
    assert queue._native.reconcile_execution is reducer
    reconcile(orphan_case)
    assert queue.execution_status(claimed["context"])["typed_charged_attempts"] == 1


def test_orphan_collector_requires_existing_admitted_transaction(orphan_case):
    modules, claimed, _, _, _, proc = orphan_case
    with (
        closing(modules["queue"]._connect()) as db,
        pytest.raises(RuntimeError, match="transaction"),
    ):
        operation()(modules, claimed["context"], db, proc=proc)


def test_existing_valid_original_wait_is_reused_truthfully_with_fresh_population(
    orphan_case, monkeypatch
):
    modules, claimed, _, registered, leaf, proc = orphan_case
    queue = modules["queue"]
    register(orphan_case, intent=True)
    (leaf / "cgroup.events").write_text("populated 0\n")
    original = modules["scope"].cleanup_observation
    waited = original(
        registered, queue._native.digest(registered), modules["proof"], owned_wait=True, proc=proc
    )
    queue.remember_native_event(claimed["context"], "cleanup", waited)
    observed = []

    def collect(*args, **kwargs):
        observed.append(kwargs["owned_wait"])
        return original(*args, **kwargs)

    monkeypatch.setattr(modules["scope"], "cleanup_observation", collect)
    reconcile(orphan_case)
    status = queue.execution_status(claimed["context"])
    assert observed == [False]  # Fresh no-wait observation is never relabelled.
    assert status["cleanup_proof"]["data"] == waited
    assert status["control_closure"]["data"]["state"] == "orphaned"
    assert queue.get_inflight(claimed["hash"]) is None
