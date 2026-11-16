"""Selected native transitions recheck current grant, clocks and independent holds."""
from contextlib import closing

import pytest

from tests.test_scripts.test_index_marker_control import base_queue as base_queue
from tests.test_scripts.test_index_marker_control import queue as queue
from tests.test_scripts.test_index_marker_execution import _intent, _registration
from tests.test_scripts.test_index_marker_retry_consumption import issued


@pytest.mark.parametrize("phase", ["register", "begin"])
@pytest.mark.parametrize("fault", ["wall", "mono", "wall-back", "mono-back", "boot", "nonce",
                                  "trial", "source", "resource", "stronger", "debt", "physical", "revoked"])
def test_new_native_ack_requires_current_selected_authority(queue, tmp_path, monkeypatch, phase, fault):
    retry, grant, request = issued(queue, tmp_path, mode="fast")
    with queue._decision_gate(native=True) as (db, finalize):
        claimed, _ = retry.consume(db, request, queue._native, queue._legs, finalize=finalize,
                                   bind_runtime=lambda context, reserved: object())
    context = claimed["context"]
    if phase == "begin":
        assert queue.register_scope(context, _registration(claimed)) == "registered"
    if fault in ("wall", "mono", "wall-back", "mono-back", "boot"):
        now = dict(boot_id=grant["issued_boot_id"], wall_ns=grant["issued_wall_ns"] + 1,
                   monotonic_ns=grant["issued_monotonic_ns"] + 1)
        if fault == "wall":
            now["wall_ns"] = grant["not_after_wall_ns"]
        elif fault == "mono":
            now["monotonic_ns"] = grant["not_after_monotonic_ns"]
        elif fault == "wall-back":
            now["wall_ns"] = grant["issued_wall_ns"] - 1
        elif fault == "mono-back":
            now["monotonic_ns"] = grant["issued_monotonic_ns"] - 1
        else:
            first = "00000000-0000-0000-0000-000000000001"
            second = "00000000-0000-0000-0000-000000000002"
            now["boot_id"] = second if grant["issued_boot_id"] == first else first
        monkeypatch.setattr(queue._retry, "_clock", lambda: now)
    elif fault == "stronger":
        queue.write_marker(context["repo_path"], "cbm", "full")
    elif fault == "physical":
        monkeypatch.setattr(queue._native, "physical_blocked", lambda *args, **kwargs: True)
    else:
        with closing(queue._connect()) as db, queue._transaction(db):
            value = queue._retry.load(db, grant["hash"], "cbm", queue._native)
            if fault == "nonce":
                value["nonce"] = value["trial"]["claim_id"] = "c" * 32
            elif fault == "trial":
                value["trial"]["execution_seq"] += 1
            elif fault == "source":
                value["source"]["provider_digest"] = "f" * 64
            elif fault == "revoked":
                value.update(status="revoked", trial=None)
            elif fault == "resource":
                db.execute("UPDATE executions SET safety_kind='resource' WHERE execution_seq=?",
                           (context["execution_seq"],))
            elif fault == "debt":
                db.execute("UPDATE executions SET attempts=attempts+1 WHERE execution_seq=?",
                           (context["execution_seq"],))
            if fault not in ("resource", "debt"):
                db.execute("UPDATE provider_state SET grant=? WHERE hash=? AND provider='cbm'",
                           (queue._native.encode(value), grant["hash"]))
    before = queue.execution_status(context)
    with pytest.raises((ValueError, RuntimeError)):
        if phase == "register":
            queue.register_scope(context, _registration(claimed))
        else:
            queue.begin_native(context, _intent(queue, claimed))
    after = queue.execution_status(context)
    for key in ("launch_reservation", "launch_permission", "scope_registration", "native_intent"):
        assert after[key] == before[key]
    assert after["native_intent"] is None
    if fault == "stronger":
        assert queue.list_markers()[0]["mode"] == "full"
