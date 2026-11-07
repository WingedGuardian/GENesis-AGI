"""Review reproductions run the actual staged journal against scratch queues."""

import pytest

from tests.test_scripts.test_index_marker_execution import (
    _begin,
    _claim,
    _intent,
    _register,
    _reservation,
    _reserve,
)
from tests.test_scripts.test_index_marker_legs import queue as queue  # noqa: F401
from tests.test_scripts.test_index_marker_native_replay import (
    _apply,
    _cleanup,
    _event,
    _started,
    _terminal,
)


def test_dropped_final_sample_is_uncertain_and_never_success_or_oom(queue, tmp_path):
    claimed = _started(queue, tmp_path)
    terminal = _terminal(claimed)
    terminal["report"].update(outcome="uncertain", after=None, finality=None, error="proof_unavailable")
    _event(queue, claimed, "terminal", terminal)
    assert _apply(queue, claimed) == "pending"
    status = queue.execution_status(claimed["context"])
    assert status["completion_kind"] == "unknown"
    assert status["safety_kind"] == "uncertainty"
    assert status["typed_charged_attempts"] == 1
    assert queue.get_inflight(claimed["context"]["hash"]) is not None
    _event(queue, claimed, "cleanup", _cleanup(queue, claimed))
    assert _apply(queue, claimed) == "pending"
    assert queue.get_inflight(claimed["context"]["hash"]) is None
    queue.write_marker(claimed["repo_path"], "cbm", "full")
    assert queue.claim_native(claimed["context"]["hash"], provider="cbm") is None


@pytest.mark.parametrize("owned_oom,expected", [(0, None), (1, "resource")])
def test_preparation_oom_survives_refusal_without_a_final_sample(
    queue, tmp_path, owned_oom, expected
):
    claimed = _claim(queue, tmp_path)
    _reserve(queue, claimed, _reservation(claimed))
    _register(queue, claimed)
    terminal = _terminal(claimed)
    terminal["provider_ok"] = False
    terminal["report"].update(outcome="refused", payload_started=False,
                              payload_returncode=None, after=None, finality=None,
                              error="proof_unavailable")
    terminal["report"]["before"]["events"]["oom_kill"] = owned_oom
    _event(queue, claimed, "terminal", terminal)
    assert _apply(queue, claimed) == "pending"
    status = queue.execution_status(claimed["context"])
    assert status["native_intent"] is None and status["completion_kind"] == "unstarted"
    assert status["typed_charged_attempts"] == 0 and status["safety_kind"] == expected
    assert queue.get_inflight(claimed["context"]["hash"]) is not None


def test_registered_cancellation_keeps_cleanup_obligation(queue, tmp_path):
    claimed = _claim(queue, tmp_path)
    _reserve(queue, claimed, _reservation(claimed))
    _register(queue, claimed)
    queue.cancel_launch(claimed["context"])
    queue.reconcile_native()
    queue.reconcile_native()
    status = queue.execution_status(claimed["context"])
    assert status["typed_applied_state"] == "pending" and status["typed_charged_attempts"] == 0
    assert queue.get_inflight(claimed["context"]["hash"]) is not None
    assert queue.claim_native(claimed["context"]["hash"], provider="gitnexus") is None
    _event(queue, claimed, "cleanup", _cleanup(queue, claimed))
    assert _apply(queue, claimed) == "pending"
    assert queue.get_inflight(claimed["context"]["hash"]) is None


def test_cleanup_cannot_leave_permission_for_a_later_native_start(queue, tmp_path):
    claimed = _claim(queue, tmp_path)
    _reserve(queue, claimed, _reservation(claimed))
    _register(queue, claimed)
    with pytest.raises((ValueError, RuntimeError)):
        _event(queue, claimed, "cleanup", _cleanup(queue, claimed))
    queue.cancel_launch(claimed["context"])
    _event(queue, claimed, "cleanup", _cleanup(queue, claimed))
    with pytest.raises((ValueError, RuntimeError)):
        _begin(queue, claimed, {**_intent(queue, claimed), "before_ns": 400})


@pytest.mark.parametrize("kind", ["cleanup", "cleanup-disproof"])
@pytest.mark.parametrize("observed_ns,invalidated", [(250, 0), (400, 1)])
def test_population_disproof_uses_bound_observation_and_not_event_name_or_arrival(queue, tmp_path, monkeypatch, kind, observed_ns, invalidated):
    # These synthetic identities provide no fresh kernel evidence. A changed
    # actual host boot must not accidentally qualify this journal-only premise.
    monkeypatch.setitem(queue.claim_native.__globals__, "_fresh_cleanup", lambda db, row: False)
    claimed = _started(queue, tmp_path)
    _event(queue, claimed, "terminal", _terminal(claimed))
    _event(queue, claimed, "cleanup", _cleanup(queue, claimed))
    assert _apply(queue, claimed) == "consumed"
    data = {**_cleanup(queue, claimed), "populated": 1, "observed_ns": observed_ns}
    if kind == "cleanup":
        data["state"] = "unresolved"
    _event(queue, claimed, kind, data)
    assert queue.execution_status(claimed["context"])["cleanup_invalidated"] == invalidated
    assert _apply(queue, claimed) == "consumed"
    next_claim = queue.claim_native(claimed["context"]["hash"], provider="gitnexus")
    assert (next_claim is None) == bool(invalidated)


@pytest.mark.parametrize("stage", ["claim", "reserved", "registered"])
def test_terminal_missing_prerequisite_is_a_durable_conflict_never_no_start_proof(queue, tmp_path, stage):
    claimed = _claim(queue, tmp_path)
    if stage != "claim":
        _reserve(queue, claimed, _reservation(claimed))
    if stage == "registered":
        _register(queue, claimed)
    with pytest.raises(RuntimeError):
        _event(queue, claimed, "terminal", _terminal(claimed))
    queue.cancel_launch(claimed["context"])
    queue.reconcile_native()
    queue.reconcile_native()
    status = queue.execution_status(claimed["context"])
    assert status["completion_kind"] == "unbound" and status["typed_applied_state"] is None
    assert status["first_typed_conflict"]["data"] == _terminal(claimed)
    assert queue.claim_native(claimed["context"]["hash"], provider="gitnexus") is None
