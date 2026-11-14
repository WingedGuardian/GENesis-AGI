"""Selected native ACKs compare the final retained semantic evidence set."""

import json
import time
from contextlib import closing

import pytest

from tests.test_scripts import test_index_marker_control as control
from tests.test_scripts.test_index_marker_control import base_queue as base_queue
from tests.test_scripts.test_index_marker_control import queue as queue
from tests.test_scripts.test_index_marker_execution import _intent, _registration, _reservation
from tests.test_scripts.test_index_marker_recovery_barrier import conflict, retained
from tests.test_scripts.test_index_marker_retry_grant import helper


def selected_with_residual(queue, tmp_path, monkeypatch):
    old, extra, path = retained(queue, tmp_path)
    status = queue.execution_status(old["context"])
    closure = control._closure(queue, status["launch_reservation"])
    closure["foreground"] = {"identity": {**closure["observer"], "pid": 1234}, "returncode": 0}
    closure["monitor"] = {"identity": {**closure["observer"], "pid": 1235}, "returncode": 0}
    control._publish(queue, old, closure)
    control._physical_end(queue, old, status["scope_registration"])
    monkeypatch.setitem(queue.apply_native.__globals__, "_fresh_gate", lambda db: lambda row: True)
    queue.apply_native(old["context"])
    assert queue.get_inflight(old["hash"]) is None
    queue.write_marker(old["repo_path"], "cbm", "full")
    retry = helper()
    now = dict(boot_id=queue._recovery.owner_identity()["boot_id"],
               wall_ns=time.time_ns(), monotonic_ns=time.monotonic_ns())
    data = dict(hash=old["hash"], repo_path=old["repo_path"], provider="cbm", origin="pending",
                run_mode="full", nonce="b" * 32, source=status["launch_reservation"]["source"],
                policy_sha256="a" * 64, evidence_reference="fixture://retained-conflict",
                not_after_wall_ns=now["wall_ns"] + 3600 * 10**9)
    with queue._decision_gate(native=True) as (db, finalize):
        grant = retry.issue(db, data, queue._native, queue._legs, now=now,
                            finalize=finalize, fresh_cleanup=lambda row: True)
    assert path.exists() and len(grant["evidence"]) >= 2
    prospective = dict(context=dict(hash=old["hash"], provider="cbm", claim_id=grant["nonce"]),
                       mode="full")
    request = dict(hash=old["hash"], provider="cbm", nonce=grant["nonce"],
                   reservation=_reservation(prospective), policy_sha256=data["policy_sha256"])
    with queue._decision_gate(native=True) as (db, finalize):
        claimed, _ = retry.consume(db, request, queue._native, queue._legs, finalize=finalize,
                                   bind_runtime=lambda context, reservation: object(),
                                   fresh_cleanup=lambda row: True)
    return old, claimed, path


@pytest.mark.parametrize("late", [False, True])
def test_public_selected_registration_requires_exact_final_evidence(
    queue, tmp_path, monkeypatch, late
):
    old, claimed, original = selected_with_residual(queue, tmp_path, monkeypatch)
    produced = []
    if late:
        physical = queue._native.physical_blocked

        def observe(db, h, **kwargs):
            produced.append(queue._spool_native_event(conflict(queue, old, 3)))
            return physical(db, h, **kwargs)

        monkeypatch.setattr(queue._native, "physical_blocked", observe)
        with pytest.raises((ValueError, RuntimeError)):
            queue.register_scope(claimed["context"], _registration(claimed))
        assert produced and all(path.exists() for path in produced)
        assert queue.execution_status(claimed["context"])["scope_registration"] is None
    else:
        assert queue.register_scope(claimed["context"], _registration(claimed)) == "registered"
        assert queue.begin_native(claimed["context"], _intent(queue, claimed)) == "started"
        with closing(queue._connect()) as db:
            assert queue._recovery.provider_blocked(db, claimed["hash"], "cbm", queue._native)
    assert original.exists()
    assert json.loads(original.read_bytes())["data"]["provider_ok"] is False
