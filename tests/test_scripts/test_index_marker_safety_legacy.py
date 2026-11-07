"""Conversion interprets legacy evidence without rewriting settlement or debt."""

import json
from contextlib import closing

import pytest

from tests.test_scripts.test_index_marker_legs import queue as queue  # noqa: F401


def _legacy(queue, tmp_path, tools="both", action="consume", *, debt=0, repo=None):
    repo = str(tmp_path / "legacy repo 雪") if repo is None else repo
    h = queue.marker_hash(repo)
    queue.marker_dir().mkdir(parents=True)
    data = {"repo_path": repo, "tools": tools, "mode": "fast", "requested_at": 1,
            "attempts": debt, "claim_id": "fixture-legacy-claim"}
    raw = json.dumps(data).encode()
    (queue.marker_dir() / (h + ".inflight.json")).write_bytes(raw)
    queue._prepare()
    if action is not None:
        (queue.marker_dir() / (".outcome-" + h)).write_text(json.dumps({
            "version": 1, "claim_id": data["claim_id"], "action": action, "recorded_at": 42,
        }))
        queue._prepare()
        if action in ("consume", "consume_full"):
            # The fixture models already-settled history. Public reconciliation
            # deliberately preserves protocol1; it cannot attest old no-start.
            with closing(queue._connect()) as db, queue._transaction(db):
                row = db.execute("SELECT * FROM executions WHERE entry_kind='claim'").fetchone()
                queue._legs.apply(db, {key: row[key] for key in queue._legs.CONTEXT})
    return h, repo, data, raw


def _convert(queue):
    with closing(queue._connect()) as db, queue._transaction(db):
        queue._recovery.upgrade(db, queue._legs, queue._native)


def _project(queue, h=None):
    operation = getattr(queue._recovery, "project_legacy", None)
    assert callable(operation), "strict conversion requires explicit legacy safety projection"
    with closing(queue._connect()) as db, queue._transaction(db):
        operation(db, queue._legs, queue._native, h=h)


def _held(queue, h, provider):
    with closing(queue._connect()) as db:
        return queue._recovery.provider_blocked(db, h, provider, queue._native)


def _rows(queue, table):
    assert table in ("pending", "failed", "inflight", "repo_state", "executions", "provider_state")
    with closing(queue._connect()) as db:
        return [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]


@pytest.mark.parametrize("tools", ["cbm", "gitnexus", "both"])
@pytest.mark.parametrize("action", [None, "consume", "consume_full", "restore", "restore_backoff", "restore_failure"])
def test_legacy_action_matrix_retains_accounting_and_never_invents_oom_or_no_start(queue, tmp_path, tools, action):
    h, repo, data, raw = _legacy(queue, tmp_path, tools, action, debt=2)
    original = {name: _rows(queue, name) for name in ("pending", "failed", "inflight", "repo_state", "executions")}
    _convert(queue)
    _project(queue, h)
    consumed = action in ("consume", "consume_full")
    for provider in ("cbm", "gitnexus"):
        assert _held(queue, h, provider) == (not consumed and tools in (provider, "both"))
    for table in ("pending", "failed", "inflight", "repo_state"):
        assert _rows(queue, table) == original[table]
    old = next(row for row in _rows(queue, "executions") if row["entry_kind"] == "claim")
    for name, value in original["executions"][0].items():
        if name != "safety_kind":
            assert old[name] == value
    assert old["legacy_raw"] == raw
    assert old["safety_kind"] == (None if consumed else "uncertainty")


@pytest.mark.parametrize("pending", ["cbm", "gitnexus", "both"])
def test_initial_combined_consumption_with_pending_holds_only_ambiguous_pending_legs(queue, tmp_path, pending):
    h, repo, data, raw = _legacy(queue, tmp_path)
    queue.write_marker(repo, pending, "full")
    original = _rows(queue, "pending")
    _convert(queue)
    _project(queue, h)
    assert _rows(queue, "pending") == original
    for provider in ("cbm", "gitnexus"):
        assert _held(queue, h, provider) == (pending in (provider, "both"))
    observations = [row for row in _rows(queue, "executions") if row["entry_kind"] == "legacy-observation"]
    assert {row["provider"] for row in observations} == ({"cbm", "gitnexus"} if pending == "both" else {pending})
    assert all(row["claim_id"] is None and row["scalar_action"] is None and row["charged_attempts"] is None for row in observations)
    assert _rows(queue, "inflight") == []


def test_modern_enqueue_does_not_reclassify_consumed_combined_history_after_cutover(queue, tmp_path):
    h, repo, data, raw = _legacy(queue, tmp_path)
    _convert(queue)
    before = _rows(queue, "executions")
    queue.write_marker(repo, "both", "full")
    _project(queue, h)
    assert not _held(queue, h, "cbm") and not _held(queue, h, "gitnexus")
    assert _rows(queue, "executions") == before


def test_pending_only_shared_debt_is_observation_once_not_a_fabricated_claim(queue, tmp_path):
    repo = str(tmp_path / "pending only")
    h = queue.marker_hash(repo)
    queue.marker_dir().mkdir(parents=True)
    raw = json.dumps({"repo_path": repo, "tools": "both", "mode": "full", "requested_at": 1, "attempts": 4}).encode()
    (queue.marker_dir() / (h + ".json")).write_bytes(raw)
    queue._prepare()
    original = _rows(queue, "pending")
    _convert(queue)
    _project(queue, h)
    before = _rows(queue, "executions"), _rows(queue, "provider_state")
    _project(queue, h)
    assert (_rows(queue, "executions"), _rows(queue, "provider_state")) == before
    assert _rows(queue, "pending") == original
    assert _rows(queue, "inflight") == []
    assert len(before[0]) == 2
    assert {row["provider"] for row in before[0]} == {"cbm", "gitnexus"}
    assert all(row["entry_kind"] == "legacy-observation" and row["claim_id"] is None
               and row["attempts"] == 4 and row["legacy_shared_attempts"] == "[4]" for row in before[0])


def test_scalar_reconciliation_uses_same_durable_uncertainty_projection(queue, tmp_path):
    repo = str(tmp_path / "scalar caller")
    queue.write_marker(repo, "cbm", "fast")
    _convert(queue)
    claimed = queue.claim(queue.marker_hash(repo), provider="cbm")
    assert claimed["context"]["protocol"] == 2
    queue.repend_stale_inflight()
    _project(queue)
    assert _held(queue, claimed["hash"], "cbm")
    assert queue.get_inflight(claimed["hash"]) is None
    assert queue.list_markers()[0]["attempts"] == 0


def test_late_outcome_remains_on_old_claim_and_does_not_consume_new_pending(queue, tmp_path):
    h, repo, data, raw = _legacy(queue, tmp_path, "cbm", "restore")
    _convert(queue)
    queue.write_marker(repo, "cbm", "full")
    pending = _rows(queue, "pending")
    (queue.marker_dir() / (".outcome-" + h)).write_text(json.dumps({
        "version": 1, "claim_id": data["claim_id"], "action": "consume", "recorded_at": 50,
    }))
    queue._prepare()
    _project(queue, h)
    assert _rows(queue, "pending") == pending
    assert _held(queue, h, "cbm")
    old = next(row for row in _rows(queue, "executions") if row["entry_kind"] == "claim")
    assert old["scalar_action"] == "restore" and old["conflict_raw"] is not None


@pytest.mark.parametrize("original,incoming_tools,affected", [
    ("cbm", "both", {"cbm", "gitnexus"}), ("both", "cbm", {"cbm", "gitnexus"}),
    ("both", "gitnexus", {"cbm", "gitnexus"}), ("cbm", "gitnexus", {"cbm", "gitnexus"}),
    ("cbm", "cbm", {"cbm"}), ("gitnexus", "gitnexus", {"gitnexus"}),
])
def test_reused_legacy_nonce_with_different_claim_is_new_ambiguity_not_new_work(queue, tmp_path, original, incoming_tools, affected):
    h, repo, data, raw = _legacy(queue, tmp_path, original, "consume")
    _convert(queue)
    candidate = {**data, "tools": incoming_tools, "mode": "full", "requested_at": 10}
    incoming = json.dumps(candidate).encode()
    for attempt in range(2):
        (queue.marker_dir() / (h + ".inflight.json")).write_bytes(incoming)
        queue._prepare()
        _project(queue, h)
        for provider in ("cbm", "gitnexus"):
            assert _held(queue, h, provider) == (provider in affected)
        snapshot = _rows(queue, "executions"), _rows(queue, "provider_state")
        if attempt == 0:
            first = snapshot
        else:
            assert snapshot == first
    observations = [row for row in first[0] if row["entry_kind"] == "legacy-observation"]
    assert {row["provider"] for row in observations} == affected
    assert all(row["claim_id"] is None and row["legacy_raw"] == incoming for row in observations)
    assert _rows(queue, "inflight") == [] and _rows(queue, "pending") == []


def test_embedded_scalar_and_import_projection_advance_once_per_provider(queue, tmp_path):
    queue._prepare()
    _convert(queue)
    repo = str(tmp_path / "embedded outcome")
    h = queue.marker_hash(repo)
    data = {"repo_path": repo, "tools": "both", "mode": "fast", "requested_at": 1,
            "attempts": 2, "claim_id": "fixture-embedded", "outcome_action": "restore", "outcome_recorded_at": 42}
    for attempt in range(2):
        (queue.marker_dir() / (h + ".inflight.json")).write_text(json.dumps(data, indent=attempt + 1))
        queue._prepare()
        assert {row["provider"]: row["hazard_revision"] for row in _rows(queue, "provider_state")} == {"cbm": 1, "gitnexus": 1}
    assert _rows(queue, "pending") == []
    assert len(_rows(queue, "inflight")) == 1


def test_conflicting_embedded_outcome_does_not_invent_claim_collision(queue, tmp_path):
    h, repo, data, raw = _legacy(queue, tmp_path, "cbm", "consume")
    _convert(queue)
    candidate = {**data, "outcome_action": "restore", "outcome_recorded_at": 50}
    for attempt in range(2):
        (queue.marker_dir() / (h + ".inflight.json")).write_text(json.dumps(candidate, indent=attempt + 1))
        queue._prepare()
        assert _held(queue, h, "cbm") and not _held(queue, h, "gitnexus")
        assert _rows(queue, "provider_state")[0]["hazard_revision"] == 1
    assert len(_rows(queue, "executions")) == 1
    assert _rows(queue, "executions")[0]["scalar_action"] == "consume"


def test_reused_identity_after_acceptance_cannot_hide_new_candidate_under_old_sequence(queue, tmp_path):
    from tests.test_scripts import test_index_marker_control as control

    h, repo, data, raw = _legacy(queue, tmp_path, "cbm", "consume", repo=str(tmp_path / "repo with space 雪"))
    _convert(queue)
    claimed, reservation, registration, closure = control._launched(queue, tmp_path, mode="full")
    control._publish(queue, claimed, closure)
    control._physical_end(queue, claimed, registration)
    assert queue.apply_native(claimed["context"]) == "consumed"
    status = queue.execution_status(claimed["context"])
    semantic = {key: status["control_closure"][key] for key in ("version", "kind", "context", "data")}
    with closing(queue._connect()) as db:
        db.execute("INSERT INTO provider_state(hash,repo_path,provider,accepted_sequence,accepted_terminal_sha256,"
                   "accepted_control_sha256,accepted_source_identity,accepted_evidence_reference) VALUES(?,?,'cbm',?,?,?,?,?)",
                   (h, repo, claimed["context"]["execution_seq"], status["terminal_sha256"], queue._native.digest(semantic),
                    queue._native.encode(reservation["source"]), "fixture-qualified-report"))
    candidate = {**data, "tools": "both", "mode": "full", "requested_at": 10}
    before = queue.execution_status(claimed["context"])
    pending = _rows(queue, "pending")
    (queue.marker_dir() / (h + ".inflight.json")).write_text(json.dumps(candidate))
    queue._prepare()
    assert _held(queue, h, "cbm") and _held(queue, h, "gitnexus")
    assert queue.execution_status(claimed["context"]) == before
    assert _rows(queue, "inflight") == [] and _rows(queue, "pending") == pending


def test_unassigned_failed_diagnostic_is_preserved_without_guessing_provider(queue):
    queue._prepare()
    with closing(queue._connect()) as db:
        db.execute("INSERT INTO failed(hash,provider,legacy_diagnostic,reason,raw_payload,failed_at,"
                   "legacy_unqualified,legacy_shared_attempts) VALUES(?,NULL,1,'fixture unknown',?,1,1,'[]')",
                   ("a" * 16, b"unassigned bytes\x00"))
    before = _rows(queue, "failed")
    _convert(queue)
    _project(queue)
    assert _rows(queue, "failed") == before
    assert _rows(queue, "provider_state") == [] and _rows(queue, "executions") == []
