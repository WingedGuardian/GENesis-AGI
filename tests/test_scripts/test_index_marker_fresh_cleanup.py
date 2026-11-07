"""Real scratch SQLite gates with synthetic original-kernel files; no native run."""

import json
import runpy
import time
from copy import deepcopy
from pathlib import Path

import pytest

from tests.test_scripts.test_code_intel_execution import evidence as evidence
from tests.test_scripts.test_code_intel_execution import scope as scope
from tests.test_scripts.test_index_marker_execution import (
    _claim,
    _intent,
    _registration,
    _reservation,
    _source,
)
from tests.test_scripts.test_index_marker_legs import queue as queue


@pytest.fixture
def historical(queue, scope, evidence, monkeypatch):
    return lambda **kwargs: _historical(queue, scope, evidence, monkeypatch, **kwargs)


def _historical(queue, scope, evidence, monkeypatch, *, settle_before=True):
    operation = getattr(queue, "_fresh_cleanup", None)
    assert callable(operation), "invalidated historical cleanup needs a fresh original-kernel gate"
    claimed = _claim(queue, scope[1].parents[2])
    reserved = _reservation(claimed)
    _, leaf, proc = scope
    leaf = leaf.rename(leaf.with_name(reserved["unit"] + ".scope"))
    membership = "0::/genesis-workload.slice/" + leaf.name + "\n"
    (proc / "self/cgroup").write_text(membership)
    (proc / "self/task" / str(queue.os.getpid()) / "cgroup").write_text(membership)
    probe = evidence["ScopeProbe"](reserved["unit"], reserved["cap_bytes"], proc=proc)
    facts = probe.capture()
    reserved["owner"] = deepcopy(facts["reporter"])
    assert queue.reserve_launch(claimed["context"], reserved) == "reserved"
    registration = dict(unit=reserved["unit"], invocation_id="1" * 32, facts=facts, stop_usec=90_000_000)
    assert queue.register_scope(claimed["context"], registration) == "registered"
    intent = dict(source=_source(), executable=dict(device=7, inode=88, sha256="f" * 64),
                  facts=facts, registration_sha256=queue._native.digest(registration), before_ns=time.monotonic_ns())
    assert queue.begin_native(claimed["context"], intent) == "started"
    terminal = dict(source=_source(), sealed_ns=time.monotonic_ns(), provider_ok=True,
                    report=dict(version=1, outcome="complete", payload_started=True, payload_returncode=0,
                                before=facts, after=facts,
                                finality=dict(threads=[queue.os.getpid()], processes=[queue.os.getpid()], pids_current=1),
                                error=None))
    queue.remember_native_event(claimed["context"], "terminal", terminal)
    collector = runpy.run_path(str(Path(queue.__dict__["__file__"]).with_name("code_intel_scope.py")))["cleanup_observation"]
    (leaf / "cgroup.events").write_text("populated 0\nfrozen 0\n")
    proof = collector(registration, queue._native.digest(registration), queue._proof, owned_wait=True, proc=proc)
    queue.remember_native_event(claimed["context"], "cleanup", proof)
    if settle_before:
        assert queue.apply_native(claimed["context"]) == "consumed"
        assert queue.get_inflight(claimed["context"]["hash"]) is None
    disproof = {**proof, "populated": 1, "observed_ns": time.monotonic_ns()}
    queue.remember_native_event(claimed["context"], "cleanup-disproof", disproof)
    # A later queue observer belongs to its own live cgroup, independently of
    # whether the historical reporter's original subtree still exists.
    (leaf.parents[1] / "observer.scope").mkdir()
    (proc / "self/cgroup").write_text("0::/observer.scope\n")
    ns = queue.claim_native.__globals__
    monkeypatch.setitem(ns, "_fresh_cleanup", lambda db, row: operation(db, row, proc=proc))
    return claimed, reserved, leaf, proc, ns


def test_invalidated_current_owner_retires_only_after_fresh_cleanup_without_refund(queue, historical):
    claimed, _, leaf, _, ns = historical(settle_before=False)
    (leaf / "cgroup.events").write_text("populated 1\n")
    assert queue.apply_native(claimed["context"]) == "consumed"
    before = queue.execution_status(claimed["context"])
    assert queue.get_inflight(claimed["context"]["hash"]) is not None
    (leaf / "cgroup.events").write_text("populated 0\n")
    assert queue.apply_native(claimed["context"]) == "consumed"
    assert queue.get_inflight(claimed["context"]["hash"]) is None
    assert queue.execution_status(claimed["context"]) == before
    assert queue.claim_native(claimed["context"]["hash"], provider="gitnexus")


def test_new_owned_oom_seen_by_fresh_gate_is_durable_without_repeating_completed_work(queue, historical):
    claimed, _, leaf, _, ns = historical()
    before = queue.execution_status(claimed["context"])
    (leaf / "memory.events").write_text("low 0\nhigh 0\nmax 1\noom 1\noom_kill 1\n")
    assert queue.claim_native(claimed["context"]["hash"], provider="gitnexus")
    after = queue.execution_status(claimed["context"])
    assert after["safety_kind"] == "resource", "a measured fresh owned OOM cannot stay an ephemeral gate observation"
    assert after["typed_applied_state"] == before["typed_applied_state"] == "consumed"
    assert after["typed_charged_attempts"] == before["typed_charged_attempts"] == 0
    assert after["terminal"] == before["terminal"] and after["cleanup_proof"] == before["cleanup_proof"]
    assert after["cleanup_invalidated"] == 1 and after["evidence_pending"] is True


def test_last_descendant_oom_is_durable_before_fresh_physical_release(queue, historical, monkeypatch):
    from tests.test_scripts.test_code_intel_scope import _last_descendant_oom

    claimed, _, leaf, _, _ = historical()
    before = queue.execution_status(claimed["context"])
    (leaf / "cgroup.events").write_text("populated 1\nfrozen 0\n")
    seen = _last_descendant_oom(monkeypatch, leaf)
    sibling = queue.claim_native(claimed["context"]["hash"], provider="gitnexus")
    assert seen == ["last-owned-exit"] and sibling
    after = queue.execution_status(claimed["context"])
    assert after["safety_kind"] == "resource" and after["evidence_pending"] is True
    assert after["cleanup_invalidated"] == 1
    assert after["typed_applied_state"] == before["typed_applied_state"] == "consumed"
    assert after["typed_charged_attempts"] == before["typed_charged_attempts"] == 0
    assert after["terminal"] == before["terminal"] and after["cleanup_proof"] == before["cleanup_proof"]
    assert len(list(queue.marker_dir().glob(".execution-spool-*.spool"))) == 1
    assert queue.get_inflight(claimed["context"]["hash"])["claim_id"] == sibling["context"]["claim_id"]


def test_unchanged_resource_counter_does_not_create_repeated_hazards_but_an_increase_is_retained(queue, historical):
    claimed, _, leaf, _, ns = historical()
    path = leaf / "memory.events"
    path.write_text("low 0\nhigh 0\nmax 1\noom 1\noom_kill 1\n")
    sibling = queue.claim_native(claimed["context"]["hash"], provider="gitnexus")
    assert sibling
    files = set(queue.marker_dir().glob(".execution-spool-*.spool"))
    assert len(files) == 1
    assert queue.claim_native(claimed["context"]["hash"], provider="gitnexus") is None
    assert set(queue.marker_dir().glob(".execution-spool-*.spool")) == files
    path.write_text("low 0\nhigh 0\nmax 2\noom 2\noom_kill 2\n")
    assert queue.claim_native(claimed["context"]["hash"], provider="gitnexus") is None
    assert len(set(queue.marker_dir().glob(".execution-spool-*.spool")) - files) == 1
    assert queue.get_inflight(claimed["context"]["hash"])["claim_id"] == sibling["context"]["claim_id"]
    assert queue.execution_status(claimed["context"])["typed_applied_state"] == "consumed"


@pytest.mark.parametrize("fault", ["file-sync", "directory-sync", "record-after-publication"])
def test_failed_resource_publication_never_releases_a_claim_and_retained_evidence_replays(
    queue, historical, monkeypatch, fault
):
    claimed, _, leaf, _, ns = historical()
    (leaf / "memory.events").write_text("low 0\nhigh 0\nmax 1\noom 1\noom_kill 1\n")

    def fail(*args, **kwargs):
        raise OSError("injected resource publication failure")

    with monkeypatch.context() as patcher:
        if fault == "file-sync":
            patcher.setattr(queue.os, "fsync", fail)
        elif fault == "directory-sync":
            patcher.setitem(ns, "_fsync_dir", fail)
        else:
            record = queue._native.record

            def recorded(*args, **kwargs):
                record(*args, **kwargs)
                fail()

            patcher.setattr(queue._native, "record", recorded)
        with pytest.raises(OSError, match="injected resource"):
            queue.claim_native(claimed["context"]["hash"], provider="gitnexus")
    assert queue.get_inflight(claimed["context"]["hash"]) is None
    status = queue.execution_status(claimed["context"])
    assert status["typed_applied_state"] == "consumed" and status["safety_kind"] == "uncertainty"
    retained = list(queue.marker_dir().glob(".execution-spool-*.spool"))
    assert len(retained) == int(fault != "file-sync")
    assert queue.claim_native(claimed["context"]["hash"], provider="gitnexus")
    assert queue.execution_status(claimed["context"])["safety_kind"] == "resource"


@pytest.mark.parametrize("protocol", ["scalar", "native"])
def test_each_claim_reads_population_again_and_keeps_original_accounting(queue, historical, protocol):
    claimed, _, leaf, _, ns = historical()
    before = queue.execution_status(claimed["context"])
    operation = queue.claim if protocol == "scalar" else queue.claim_native
    (leaf / "cgroup.events").write_text("populated 1\n")
    assert operation(claimed["context"]["hash"], provider="gitnexus") is None
    assert queue.get_inflight(claimed["context"]["hash"]) is None
    (leaf / "cgroup.events").write_text("populated 0\n")
    sibling = operation(claimed["context"]["hash"], provider="gitnexus")
    assert sibling and sibling["context"]["provider"] == "gitnexus"
    after = queue.execution_status(claimed["context"])
    assert after == before and after["cleanup_invalidated"] == 1
    assert queue.apply_native(claimed["context"]) == "consumed"
    assert queue.get_inflight(claimed["context"]["hash"])["claim_id"] == sibling["context"]["claim_id"]


@pytest.mark.parametrize("stage", ["reserve", "register", "begin"])
def test_all_remaining_launch_gates_recheck_after_a_successful_claim(queue, historical, stage):
    claimed, _, leaf, _, ns = historical()
    sibling = queue.claim_native(claimed["context"]["hash"], provider="gitnexus")
    assert sibling
    reserved = _reservation(sibling)
    if stage != "reserve":
        assert queue.reserve_launch(sibling["context"], reserved) == "reserved"
    if stage == "begin":
        assert queue.register_scope(sibling["context"], _registration(sibling)) == "registered"
    (leaf / "cgroup.events").write_text("populated 1\n")
    operation, data = {
        "reserve": (queue.reserve_launch, reserved),
        "register": (queue.register_scope, _registration(sibling)),
        "begin": (queue.begin_native, _intent(queue, sibling)),
    }[stage]
    with pytest.raises(RuntimeError, match="held"):
        operation(sibling["context"], data)
    assert queue.execution_status(sibling["context"])["native_intent"] is None
    assert queue.execution_status(claimed["context"])["cleanup_invalidated"] == 1


@pytest.mark.parametrize("fault", ["missing-counter", "replacement", "root", "mount"])
def test_unprovable_current_identity_never_releases_a_claim(queue, historical, fault):
    claimed, _, leaf, proc, ns = historical()
    before = queue.execution_status(claimed["context"])
    if fault == "missing-counter":
        (leaf / "memory.events").unlink()
    elif fault == "replacement":
        retained = leaf.rename(leaf.with_name("retained-original.scope"))
        leaf.mkdir()
        for path in retained.iterdir():
            (leaf / path.name).write_bytes(path.read_bytes())
    elif fault == "root":
        root = leaf.parents[1]
        root.rename(root.with_name("retained-root"))
        root.mkdir()
    else:
        path = proc / "self/mountinfo"
        path.write_text(path.read_text().replace("1 0", "2 0"))
    assert queue.claim_native(claimed["context"]["hash"], provider="gitnexus") is None
    assert queue.get_inflight(claimed["context"]["hash"]) is None
    assert queue.execution_status(claimed["context"]) == before


@pytest.mark.parametrize("state", ["removed", "rebooted"])
def test_fresh_independent_lifetime_proof_releases_only_physical_hold(queue, historical, state):
    claimed, _, leaf, proc, ns = historical()
    before = queue.execution_status(claimed["context"])
    if state == "removed":
        for path in leaf.iterdir():
            path.unlink()
        leaf.rmdir()
    else:
        (proc / "sys/kernel/random/boot_id").write_text("00000000-0000-0000-0000-000000000002")
    assert queue.claim_native(claimed["context"]["hash"], provider="gitnexus")
    assert queue.execution_status(claimed["context"]) == before


@pytest.mark.parametrize("hold", ["uncertainty", "resource"])
def test_physical_cleanup_does_not_release_provider_hold_or_hide_sticky_disproof(queue, historical, hold):
    claimed, reserved, leaf, _, ns = historical()
    if hold == "resource":
        queue.remember_native_event(claimed["context"], "stop", dict(reason="pressure_stop",
                                    observed_ns=time.monotonic_ns(), unit=reserved["unit"],
                                    boot_id=reserved["owner"]["boot_id"]))
    queue.write_marker(claimed["repo_path"], "cbm", "full")
    assert queue.claim_native(claimed["context"]["hash"], provider="cbm") is None
    assert queue.claim_native(claimed["context"]["hash"], provider="gitnexus")
    status = queue.execution_status(claimed["context"])
    assert status["safety_kind"] == hold and status["cleanup_invalidated"] == 1
    assert status["typed_charged_attempts"] == 0 and status["completion_kind"] == "success"


@pytest.mark.parametrize("fault", ["missing-counter", "changed-mapping"])
def test_unreadable_disputed_scope_does_not_roll_back_unrelated_reconciliation(
    queue, historical, tmp_path, fault
):
    earlier = _claim(queue, tmp_path / "earlier")
    disputed, _, leaf, proc, _ = historical(settle_before=False)
    later = _claim(queue, tmp_path / "later")
    before = queue.execution_status(disputed["context"])
    if fault == "missing-counter":
        (leaf / "memory.events").unlink()
    else:
        mapping = proc / "self/mountinfo"
        mapping.write_text(mapping.read_text().replace("1 0", "2 0"))

    failure = None
    try:
        queue.reconcile_native()
    except (OSError, ValueError) as error:
        failure = error
    for claimed in (earlier, later):
        assert queue.get_inflight(claimed["context"]["hash"]) is None, (
            "one unreadable scope rolled back or prevented unrelated recovery"
        )
        rows = [row for row in queue.list_markers() if row["hash"] == claimed["context"]["hash"]]
        assert {row["provider"] for row in rows} == {"cbm", "gitnexus"}
        assert all(row["attempts"] == 0 for row in rows)
    assert failure is None
    assert queue.get_inflight(disputed["context"]["hash"])["claim_id"] == disputed["context"]["claim_id"]
    after = queue.execution_status(disputed["context"])
    assert after["cleanup_invalidated"] == 1 and after["safety_kind"] == "uncertainty"
    assert after["typed_applied_state"] == "consumed" and after["typed_charged_attempts"] == 0
    assert all(after[key] == before[key] for key in ("terminal", "cleanup_proof", "native_intent"))
    assert queue.reconcile_native() == []
    assert queue.execution_status(disputed["context"]) == after


@pytest.mark.parametrize("fault", ["registration-digest", "collector-runtime", "returned-proof", "retained-event"])
def test_fresh_cleanup_does_not_hide_digest_or_unexpected_collector_failures(
    queue, historical, monkeypatch, fault
):
    claimed, _, _, proc, _ = historical()
    before = queue.execution_status(claimed["context"])
    collected = []

    def fail_digest(*args):
        raise ValueError("synthetic registration digest failure")

    def collect(*args, **kwargs):
        collected.append(True)
        if fault == "collector-runtime":
            raise RuntimeError("synthetic unexpected collector failure")
        if fault == "returned-proof":
            return {}
        return {**json.loads(row["cleanup_proof"])["data"], "observed_ns": time.monotonic_ns()}

    def publish(item):
        pytest.fail("a failed observation cannot publish evidence")

    with queue._queue_gate() as db:
        row = queue._native.execution(db, claimed["context"], queue._legs)
        with monkeypatch.context() as patcher:
            if fault == "registration-digest":
                patcher.setattr(queue._native, "digest", fail_digest)
            expected = RuntimeError if fault == "collector-runtime" else ValueError
            message = ("invalid cleanup" if fault == "returned-proof" else
                       "retained resource evidence" if fault == "retained-event" else "synthetic")
            with pytest.raises(expected, match=message):
                queue._native.retain_fresh_cleanup(
                    db, row, queue._legs, queue._proof, collect=collect, proc=proc,
                    clock=time.time, retained=({"context": {}},) if fault == "retained-event" else (), publish=publish,
                )
    assert collected == ([] if fault == "registration-digest" else [True])
    assert queue.execution_status(claimed["context"]) == before
