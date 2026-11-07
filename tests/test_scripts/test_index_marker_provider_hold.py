"""Read modeled acceptance states on the real queue; no positive recovery API."""

import json
import time
from contextlib import closing

import pytest

from tests.test_scripts import test_index_marker_control as control

base_queue = control.base_queue
queue = control.queue


def _accepted(queue, tmp_path):
    """Build old history and a clean trial, then model Q4b's future atomic write."""
    repo = str(tmp_path / "repo with space 雪")
    queue.write_marker(repo, "cbm", "fast")
    old = queue.claim(queue.marker_hash(repo), provider="cbm")
    queue.consume(old["context"])
    claimed, reservation, registration, closure = control._launched(queue, tmp_path, mode="full")
    control._publish(queue, claimed, closure)
    control._physical_end(queue, claimed, registration)
    assert queue.apply_native(claimed["context"]) == "consumed"
    status = queue.execution_status(claimed["context"])
    semantic = {key: status["control_closure"][key] for key in ("version", "kind", "context", "data")}
    with closing(queue._connect()) as db:
        db.execute("UPDATE executions SET safety_kind='uncertainty' WHERE execution_seq=?", (old["context"]["execution_seq"],))
        db.execute("INSERT INTO provider_state(hash,repo_path,provider,hazard_revision,accepted_sequence,"
                   "accepted_terminal_sha256,accepted_control_sha256,accepted_source_identity,accepted_evidence_reference) "
                   "VALUES(?,?,?,17,?,?,?,?,?)", (claimed["hash"], claimed["repo_path"], "cbm",
                   claimed["context"]["execution_seq"], status["terminal_sha256"], queue._native.digest(semantic),
                   queue._native.encode(reservation["source"]), "fixture-evidence-v1"))
    return old, claimed, reservation, registration, closure


def _held(queue, claimed, provider="cbm"):
    operation = getattr(queue._recovery, "provider_blocked", None)
    assert callable(operation), "provider holds need one indexed acceptance-watermark reader"
    with closing(queue._connect()) as db:
        return operation(db, claimed["hash"], provider, queue._native)


def test_exact_clean_acceptance_covers_older_uncertainty_without_erasing_history(queue, tmp_path):
    old, claimed, reservation, registration, closure = _accepted(queue, tmp_path)
    before = queue.database_path().read_bytes()
    assert not _held(queue, claimed)
    assert not _held(queue, claimed, "gitnexus")
    assert queue.database_path().read_bytes() == before
    with closing(queue._connect()) as db:
        assert db.execute("SELECT safety_kind FROM executions WHERE execution_seq=?",
                          (old["context"]["execution_seq"],)).fetchone()[0] == "uncertainty"


def test_sequence_zero_covers_nothing_and_ordinary_success_or_enqueue_cannot_accept(queue, tmp_path):
    old, claimed, reservation, registration, closure = _accepted(queue, tmp_path)
    with closing(queue._connect()) as db:
        db.execute("UPDATE provider_state SET accepted_sequence=0,accepted_terminal_sha256=NULL,"
                   "accepted_control_sha256=NULL,accepted_source_identity=NULL,accepted_evidence_reference=NULL")
    assert _held(queue, claimed)
    assert queue.apply_native(claimed["context"]) == "consumed"
    queue.write_marker(claimed["repo_path"], "both", "full")
    assert _held(queue, claimed)
    assert queue.claim(claimed["hash"], provider="cbm") is None
    assert queue.claim_native(claimed["hash"], provider="cbm") is None
    assert queue.claim_native(claimed["hash"], provider="gitnexus") is not None
    with closing(queue._connect()) as db:
        assert db.execute("SELECT accepted_sequence FROM provider_state").fetchone()[0] == 0


@pytest.mark.parametrize("fault", ["resource", "conflict", "terminal", "control", "source",
                                   "missing-trial", "scalar-trial", "missing-control", "missing-terminal"])
def test_acceptance_rechecks_exact_selected_trial_and_both_witnesses(queue, tmp_path, fault):
    old, claimed, reservation, registration, closure = _accepted(queue, tmp_path)
    with closing(queue._connect()) as db:
        seq = claimed["context"]["execution_seq"]
        if fault == "resource":
            db.execute("UPDATE executions SET safety_kind='resource' WHERE execution_seq=?", (seq,))
        elif fault == "conflict":
            db.execute("UPDATE executions SET first_typed_conflict=? WHERE execution_seq=?", (b'{"fixture":"conflict"}', seq))
        elif fault in ("terminal", "control"):
            field = "terminal" if fault == "terminal" else "control_closure"
            value = json.loads(db.execute(f"SELECT {field} FROM executions WHERE execution_seq=?", (seq,)).fetchone()[0])
            if fault == "terminal":
                value["data"]["provider_ok"] = False
            else:
                value["data"].update(state="uncertain", reason="monitor_failed")
            db.execute(f"UPDATE executions SET {field}=? WHERE execution_seq=?", (queue._native.encode(value), seq))
        elif fault == "source":
            source = {**reservation["source"], "configuration_sha256": "f" * 64}
            db.execute("UPDATE provider_state SET accepted_source_identity=?", (queue._native.encode(source),))
        elif fault in ("missing-trial", "scalar-trial"):
            db.execute("UPDATE provider_state SET accepted_sequence=?",
                       (seq + 1000 if fault == "missing-trial" else old["context"]["execution_seq"],))
        else:
            field = "control_closure" if fault == "missing-control" else "terminal"
            db.execute(f"UPDATE executions SET {field}=NULL WHERE execution_seq=?", (seq,))
    assert _held(queue, claimed)


def test_newer_uncertainty_holds_after_a_clean_accepted_trial(queue, tmp_path):
    old, claimed, reservation, registration, closure = _accepted(queue, tmp_path)
    queue.write_marker(claimed["repo_path"], "both", "full")
    # Model a future writer's new legacy observation; it is neither a claim nor a reset.
    with closing(queue._connect()) as db:
        db.execute("INSERT INTO executions(hash,repo_path,provider,protocol,entry_kind,claim_id,original_tools,"
                   "mode,requested_at,attempts,legacy_unqualified,legacy_shared_attempts,nonce_origin,safety_kind) "
                   "VALUES(?,?,?,1,'legacy-observation',NULL,'cbm','full',1,4,1,'[4]','fixture-new','uncertainty')",
                   (claimed["hash"], claimed["repo_path"], "cbm"))
    assert _held(queue, claimed)
    assert not _held(queue, claimed, "gitnexus")


def test_late_control_conflict_at_accepted_trial_revokes_safety_without_reaccounting(queue, tmp_path):
    old, claimed, reservation, registration, closure = _accepted(queue, tmp_path)
    before = queue.execution_status(claimed["context"])
    with pytest.raises(RuntimeError, match="conflict"):
        control._publish(queue, claimed, {**closure, "observed_ns": registration["stop_usec"] + closure["observed_ns"]})
    assert _held(queue, claimed)
    after = queue.execution_status(claimed["context"])
    for field in ("terminal", "typed_applied_state", "typed_charged_attempts", "disposition"):
        assert after[field] == before[field]


def test_late_conflict_at_older_sequence_stays_covered_but_evidence_is_retained(queue, tmp_path):
    old, claimed, reservation, registration, closure = _accepted(queue, tmp_path)
    with closing(queue._connect()) as db:
        db.execute("UPDATE executions SET conflict_reason='late legacy contradiction',conflict_raw=? "
                   "WHERE execution_seq=?", (b"original late bytes", old["context"]["execution_seq"]))
    assert not _held(queue, claimed)


def test_unrepresented_old_control_conflict_is_not_hidden_by_later_acceptance(queue, tmp_path):
    historical, _, registration, closure = control._launched(queue, tmp_path)
    control._publish(queue, historical, closure)
    control._physical_end(queue, historical, registration)
    assert queue.apply_native(historical["context"]) == "consumed"
    _, accepted, _, _, _ = _accepted(queue, tmp_path)
    assert historical["context"]["execution_seq"] < accepted["context"]["execution_seq"]
    for offset in (1, 2):
        with pytest.raises(RuntimeError, match="conflict"):
            control._publish(queue, historical, {**closure, "observed_ns": time.monotonic_ns() + offset})
    # The later clean watermark covers represented older incidents. It cannot
    # authorize ignoring an extra receipt that the bounded conflict slot lacks.
    assert not _held(queue, accepted)
    assert list(queue.marker_dir().glob(".execution-spool-*.spool"))
    queue.write_marker(accepted["repo_path"], "cbm", "fast")
    with pytest.raises(RuntimeError, match="unrepresented"):
        queue.claim_native(accepted["hash"], provider="cbm")
    assert queue.get_inflight(accepted["hash"]) is None
    assert any(row["provider"] == "cbm" for row in queue.list_markers())
    assert queue.claim_native(accepted["hash"], provider="gitnexus") is not None
