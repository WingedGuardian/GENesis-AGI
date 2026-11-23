"""Final selected acknowledgements retain independent sibling physical holds."""

import time
from contextlib import closing, contextmanager
from copy import deepcopy

import pytest

from tests.test_scripts.test_code_intel_selected_supervision import tables
from tests.test_scripts.test_index_marker_control import base_queue as base_queue
from tests.test_scripts.test_index_marker_control import queue as queue
from tests.test_scripts.test_index_marker_execution import (
    _finish_control,
    _intent,
    _registration,
    _reservation,
)
from tests.test_scripts.test_index_marker_fresh_cleanup import _historical
from tests.test_scripts.test_index_marker_fresh_cleanup import evidence as evidence
from tests.test_scripts.test_index_marker_fresh_cleanup import scope as scope
from tests.test_scripts.test_index_marker_native_replay import _cleanup, _terminal
from tests.test_scripts.test_index_marker_retry_consumption import issued


def completed_sibling(queue, tmp_path, provider):
    repo = str(tmp_path / "repo")
    queue.write_marker(repo, "both", "full")
    sibling = queue.claim_native(queue.marker_hash(repo), provider=provider)
    context = sibling["context"]
    assert queue.reserve_launch(context, _reservation(sibling)) == "reserved"
    assert queue.register_scope(context, _registration(sibling)) == "registered"
    assert queue.begin_native(context, _intent(queue, sibling)) == "started"
    queue.remember_native_event(context, "terminal", _terminal(sibling))
    queue.remember_native_event(context, "cleanup", _cleanup(queue, sibling))
    _finish_control(queue, sibling)
    assert queue.apply_native(context) == "consumed"
    assert queue.get_inflight(sibling["hash"]) is None
    return sibling


@pytest.mark.parametrize("provider", ["cbm", "gitnexus"])
@pytest.mark.parametrize("phase", ["consume", "register", "begin"])
@pytest.mark.parametrize("late", [False, True])
@pytest.mark.parametrize("prior_disproof", [False, True])
def test_other_provider_hold_from_final_drain_prevents_selected_ack(
    queue, tmp_path, monkeypatch, provider, phase, late, prior_disproof
):
    sibling = completed_sibling(queue, tmp_path, "gitnexus" if provider == "cbm" else "cbm")
    retry, grant, request = issued(queue, tmp_path, provider)

    def fresh(row):
        assert row["execution_seq"] == sibling["context"]["execution_seq"]
        return True

    if prior_disproof:
        for observed_ns in (400, 500):
            data = {**_cleanup(queue, sibling), "state": "unresolved", "populated": 1,
                    "owned_wait": False, "observed_ns": observed_ns}
            if observed_ns == 400:
                queue.remember_native_event(sibling["context"], "cleanup-disproof", data)
            else:
                with pytest.raises(RuntimeError, match="conflicting native evidence"):
                    queue.remember_native_event(sibling["context"], "cleanup-disproof", data)
        monkeypatch.setitem(queue.claim_native.__globals__, "_fresh_gate", lambda db: fresh)
    if phase != "consume":
        with queue._decision_gate(native=True) as (db, finalize):
            claimed, _ = retry.consume(db, request, queue._native, queue._legs,
                finalize=finalize, bind_runtime=lambda *args: object(), fresh_cleanup=fresh)
        if phase == "begin":
            assert queue.register_scope(claimed["context"], _registration(claimed)) == "registered"
    with closing(queue._connect()) as db:
        assert not queue._native.physical_blocked(db, grant["hash"],
            exclude=claimed["context"]["execution_seq"] if phase != "consume" else 0,
            fresh_cleanup=fresh)
    before = tables(queue)
    original = queue._decision_gate
    paths = []
    observed = []
    armed = False

    def bind(*args):
        nonlocal armed
        armed = True
        return object()

    if phase != "consume":
        name = "register" if phase == "register" else "begin"
        operation = getattr(queue._native, name)

        def stage(*args, **kwargs):
            nonlocal armed
            result = operation(*args, **kwargs)
            if result in ("registered", "started"):
                armed = True
            return result

        monkeypatch.setattr(queue._native, name, stage)

    @contextmanager
    def decision(*args, **kwargs):
        with original(*args, **kwargs) as (db, finalize):
            def last(**options):
                if late and armed and not paths:
                    previous = queue._native.execution(db, sibling["context"], queue._legs)
                    data = {**_cleanup(queue, sibling), "state": "unresolved", "populated": 1,
                            "owned_wait": False, "observed_ns": 600 if prior_disproof else 400}
                    event = queue._native.event(sibling["context"], "cleanup-disproof", data,
                                                time.time(), queue._proof)
                    paths.append(queue._spool_native_event(event))
                residual = finalize(**options)
                if late and armed:
                    row = queue._native.execution(db, sibling["context"], queue._legs)
                    assert row["cleanup_invalidated"] == 1
                    if prior_disproof:
                        assert row == previous, "the full-slot disproof must exist only as raw residual evidence"
                        assert any(key[:3] == (sibling["hash"], sibling["provider"], "cleanup-disproof")
                                   for key in residual)
                    assert queue._native.physical_blocked(db, grant["hash"],
                        exclude=claimed["context"]["execution_seq"] if phase != "consume" else
                        db.execute("SELECT execution_seq FROM inflight WHERE hash=?", (grant["hash"],)).fetchone()[0])
                    current = retry.view(db, grant["hash"], provider, queue._native, queue._legs,
                                         residual, exclude=db.execute(
                                             "SELECT execution_seq FROM inflight WHERE hash=?",
                                             (grant["hash"],)).fetchone()[0])
                    assert all(current[key] == grant[key] for key in ("hazard_revision", "frontier", "evidence"))
                    observed.append(row["execution_seq"])
                return residual
            yield db, last

    monkeypatch.setitem(queue.claim_native.__globals__, "_decision_gate", decision)

    def transition():
        if phase == "consume":
            with decision(native=True) as (db, finalize):
                return retry.consume(db, request, queue._native, queue._legs,
                                     finalize=finalize, bind_runtime=bind, fresh_cleanup=fresh)
        if phase == "register":
            return queue.register_scope(claimed["context"], _registration(claimed))
        return queue.begin_native(claimed["context"], _intent(queue, claimed))

    if late:
        with pytest.raises(RuntimeError, match="physical"):
            try:
                transition()
            finally:
                assert observed and len(paths) == 1
        assert paths[0].is_file()
        assert tables(queue) == before
    else:
        result = transition()
        assert result if phase == "consume" else result == ("registered" if phase == "register" else "started")
        assert not paths and not observed


@pytest.mark.parametrize("provider", ["cbm", "gitnexus"])
@pytest.mark.parametrize("phase", ["consume", "register", "begin"])
@pytest.mark.parametrize("reservation_conflict", [False, True])
def test_unchanged_sibling_fresh_cleanup_still_allows_selected_ack(
    queue, tmp_path, monkeypatch, provider, phase, reservation_conflict
):
    sibling = completed_sibling(queue, tmp_path, "gitnexus" if provider == "cbm" else "cbm")
    retry, grant, request = issued(queue, tmp_path, provider)
    if reservation_conflict:
        with pytest.raises(RuntimeError, match="native reservation refused: conflict"):
            queue.reserve_launch(sibling["context"], {**_reservation(sibling), "wall_seconds": 3601})
    disproof = {**_cleanup(queue, sibling), "state": "unresolved", "populated": 1,
                "owned_wait": False, "observed_ns": 400}
    queue.remember_native_event(sibling["context"], "cleanup-disproof", disproof)
    timeline = []

    def fresh(row):
        assert row["execution_seq"] == sibling["context"]["execution_seq"]
        assert row["cleanup_invalidated"] == 1
        timeline.append("fresh")
        return True  # Stand-in for the existing original-scope kernel collector.

    monkeypatch.setitem(queue.claim_native.__globals__, "_fresh_gate", lambda db: fresh)
    if phase != "consume":
        with queue._decision_gate(native=True) as (db, finalize):
            claimed, _ = retry.consume(db, request, queue._native, queue._legs,
                finalize=finalize, bind_runtime=lambda *args: object(), fresh_cleanup=fresh)
        if phase == "begin":
            assert queue.register_scope(claimed["context"], _registration(claimed)) == "registered"
    timeline.clear()
    original = queue._decision_gate

    @contextmanager
    def decision(*args, **kwargs):
        with original(*args, **kwargs) as (db, finalize):
            def last(**options):
                result = finalize(**options)
                timeline.append("drain")
                return result
            yield db, last

    monkeypatch.setitem(queue.claim_native.__globals__, "_decision_gate", decision)
    if phase == "consume":
        with decision(native=True) as (db, finalize):
            result = retry.consume(db, request, queue._native, queue._legs,
                finalize=finalize, bind_runtime=lambda *args: object(), fresh_cleanup=fresh)
        assert result
    elif phase == "register":
        assert queue.register_scope(claimed["context"], _registration(claimed)) == "registered"
    else:
        assert queue.begin_native(claimed["context"], _intent(queue, claimed)) == "started"
    assert "fresh" in timeline and timeline[-1] == "drain"
    with closing(queue._connect()) as db:
        row = queue._native.execution(db, sibling["context"], queue._legs)
        assert row["cleanup_invalidated"] == 1 and row["cleanup_proof"] is not None
        assert db.execute("SELECT accepted_sequence FROM provider_state WHERE hash=? AND provider=?",
                          (grant["hash"], provider)).fetchone()[0] == 0


@pytest.mark.parametrize("fault", ["missing", "protocol", "state", "provider", "claim", "sequence"])
def test_physical_residuals_require_complete_exact_native_annotations(queue, tmp_path, fault):
    sibling = completed_sibling(queue, tmp_path, "gitnexus")
    for observed_ns in (400, 500, 600):
        data = {**_cleanup(queue, sibling), "state": "unresolved", "populated": 1,
                "owned_wait": False, "observed_ns": observed_ns}
        if observed_ns == 400:
            queue.remember_native_event(sibling["context"], "cleanup-disproof", data)
        elif observed_ns == 500:
            with pytest.raises(RuntimeError, match="conflicting native evidence"):
                queue.remember_native_event(sibling["context"], "cleanup-disproof", data)
        else:
            queue._spool_native_event(queue._native.event(sibling["context"], "cleanup-disproof",
                                                         data, time.time(), queue._proof))
    with queue._decision_gate(native=True) as (db, finalize):
        annotations = {"stale": None}
        residual = finalize(residual_context=annotations)
        assert len(residual) == len(annotations) == 1 and "stale" not in annotations
        project = queue._retry._cleanup_residuals
        assert project(db, residual, annotations, queue._native, queue._legs) == {
            sibling["context"]["execution_seq"]: set(residual)}
        changed = deepcopy(annotations)
        key = residual[0]
        context, state = changed[key]
        if fault == "missing":
            changed.clear()
        elif fault == "state":
            changed[key] = (context, "conflict")
        else:
            context[{"protocol": "protocol", "provider": "provider", "claim": "claim_id",
                     "sequence": "execution_seq"}[fault]] = {
                         "protocol": True, "provider": "cbm", "claim": "0" * 32,
                         "sequence": True}[fault]
        with pytest.raises((ValueError, RuntimeError)):
            project(db, residual, changed, queue._native, queue._legs)


@pytest.mark.parametrize("result", [False, None, 1, "true"])
def test_nonproof_never_clears_a_sibling_or_publishes_partial_selected_work(queue, tmp_path, result):
    sibling = completed_sibling(queue, tmp_path, "gitnexus")
    retry, grant, request = issued(queue, tmp_path)
    queue.remember_native_event(sibling["context"], "cleanup-disproof",
        {**_cleanup(queue, sibling), "state": "unresolved", "populated": 1,
         "owned_wait": False, "observed_ns": 400})
    before = tables(queue)
    with (pytest.raises(RuntimeError, match="reservation remains held"),
          queue._decision_gate(native=True) as (db, finalize)):
        retry.consume(db, request, queue._native, queue._legs, finalize=finalize,
            bind_runtime=lambda *args: pytest.fail("held lifetime cannot reach runtime binding"),
            fresh_cleanup=lambda row: result)
    assert tables(queue) == before


def test_fresh_proof_is_recollected_for_the_next_selected_decision(queue, tmp_path, monkeypatch):
    sibling = completed_sibling(queue, tmp_path, "gitnexus")
    retry, grant, request = issued(queue, tmp_path)
    queue.remember_native_event(sibling["context"], "cleanup-disproof",
        {**_cleanup(queue, sibling), "state": "unresolved", "populated": 1,
         "owned_wait": False, "observed_ns": 400})
    with queue._decision_gate(native=True) as (db, finalize):
        claimed, _ = retry.consume(db, request, queue._native, queue._legs,
            finalize=finalize, bind_runtime=lambda *args: object(), fresh_cleanup=lambda row: True)
    before = tables(queue)
    observed = []

    def unavailable(row):
        observed.append(row["execution_seq"])
        return False

    monkeypatch.setitem(queue.claim_native.__globals__, "_fresh_gate", lambda db: unavailable)
    with pytest.raises(RuntimeError, match="native launch refused: held"):
        queue.register_scope(claimed["context"], _registration(claimed))
    assert observed == [sibling["context"]["execution_seq"]]
    assert tables(queue) == before


def test_each_invalidated_sibling_needs_its_own_fresh_subject(queue, tmp_path):
    siblings = [completed_sibling(queue, tmp_path, "gitnexus") for _ in range(2)]
    retry, grant, request = issued(queue, tmp_path)
    for sibling in siblings:
        queue.remember_native_event(sibling["context"], "cleanup-disproof",
            {**_cleanup(queue, sibling), "state": "unresolved", "populated": 1,
             "owned_wait": False, "observed_ns": 400})
    before = tables(queue)
    with (pytest.raises(RuntimeError, match="reservation remains held"),
          queue._decision_gate(native=True) as (db, finalize)):
        retry.consume(db, request, queue._native, queue._legs, finalize=finalize,
            bind_runtime=lambda *args: pytest.fail("one proof cannot cover two subjects"),
            fresh_cleanup=lambda row: row["execution_seq"] == siblings[0]["context"]["execution_seq"])
    assert tables(queue) == before
    collected = []

    def fresh(row):
        collected.append(row["execution_seq"])
        return True

    with queue._decision_gate(native=True) as (db, finalize):
        assert retry.consume(db, request, queue._native, queue._legs, finalize=finalize,
            bind_runtime=lambda *args: object(), fresh_cleanup=fresh)
    assert collected == [sibling["context"]["execution_seq"] for sibling in siblings]


@pytest.mark.parametrize("populated", [0, 1])
@pytest.mark.parametrize("oom", [0, 1])
def test_real_fresh_collector_retains_new_oom_and_reconciles_same_explicit_grant(
    queue, scope, evidence, tmp_path, monkeypatch, populated, oom
):
    sibling, _, leaf, _, _ = _historical(queue, scope, evidence, monkeypatch)
    repo, h = sibling["repo_path"], sibling["hash"]
    queue.write_marker(repo, "gitnexus", "full")
    retry = queue._retry
    with queue._decision_gate(native=True) as (db, finalize):
        grant = retry.issue(db, dict(hash=h, repo_path=repo, provider="gitnexus", origin="pending",
            run_mode="full", nonce="b" * 32, source=_reservation(sibling)["source"], policy_sha256="a" * 64,
            evidence_reference="fixture://fresh-original-scope", not_after_wall_ns=time.time_ns()+3600*10**9),
            queue._native, queue._legs, finalize=finalize, fresh_cleanup=queue._fresh_gate(db))
    prospective = dict(context=dict(hash=h, provider="gitnexus", claim_id=grant["nonce"]), mode="full")
    request = dict(hash=h, provider="gitnexus", nonce=grant["nonce"],
                   reservation=_reservation(prospective), policy_sha256=grant["policy_sha256"])
    (leaf / "cgroup.events").write_text(f"populated {populated}\nfrozen 0\n")
    if oom:
        (leaf / "memory.events").write_text("low 0\nhigh 0\nmax 1\noom 1\noom_kill 1\n")

    def consume():
        with queue._decision_gate(native=True) as (db, finalize):
            return retry.consume(db, request, queue._native, queue._legs, finalize=finalize,
                bind_runtime=lambda *args: object(), fresh_cleanup=queue._fresh_gate(db))

    if populated or oom:
        before = tables(queue)
        with pytest.raises(RuntimeError, match="held|physical"):
            consume()
        assert tables(queue) == before
        if oom:
            assert list(queue.marker_dir().glob(".execution-spool-*.spool"))
        (leaf / "cgroup.events").write_text("populated 0\nfrozen 0\n")
    claimed, _ = consume()
    assert claimed["provider"] == "gitnexus"
    with closing(queue._connect()) as db:
        old = queue._native.execution(db, sibling["context"], queue._legs)
        assert old["cleanup_invalidated"] == 1 and old["typed_charged_attempts"] == 0
        assert (old["safety_kind"] == "resource") is bool(oom)
        assert retry.load(db, h, "gitnexus", queue._native)["status"] == "spent"
        assert db.execute("SELECT count(*) FROM executions WHERE nonce_origin='selected-recovery'").fetchone()[0] == 1
