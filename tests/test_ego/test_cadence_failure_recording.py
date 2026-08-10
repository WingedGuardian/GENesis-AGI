"""Tests for EgoCadenceManager._record_failure exception threading.

Regression guard for follow-up ff762018: the ego cadence failure recorder must
forward the caught exception to ``record_job_failure`` so exception-driven
ego-cycle failures carry ``error_type``/``error_frames`` and fire the throttled
``job.failed`` reflex event (the reflex arc ingests job.failed since #1304).
Semantic (no-exception) failures must forward ``exc=None`` so they correctly
stay off the reflex bus.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from genesis.ego.cadence import EgoCadenceManager


def _make_manager() -> EgoCadenceManager:
    """Minimal manager instance for exercising _record_failure in isolation.

    Avoids the heavyweight __init__ (session/db/scheduler) — _record_failure
    only touches the circuit-breaker fields, _config, and _session._source_tag.
    """
    mgr = object.__new__(EgoCadenceManager)
    mgr._consecutive_failures = 0
    mgr._circuit_open_until = None
    mgr._config = SimpleNamespace(consecutive_failure_limit=3, failure_backoff_minutes=30)
    mgr._session = SimpleNamespace(_source_tag="genesis_ego_cycle")
    return mgr


def test_record_failure_forwards_exception():
    """Exception path forwards exc -> error_type derived + job.failed emitted."""
    mgr = _make_manager()
    boom = ValueError("boom")
    rt = MagicMock()
    with patch("genesis.runtime.GenesisRuntime") as GR:
        GR.instance.return_value = rt
        mgr._record_failure(str(boom), exc=boom)

    rt.record_job_failure.assert_called_once()
    args, kwargs = rt.record_job_failure.call_args
    assert args[0] == "genesis_ego_cycle"
    assert args[1] == "boom"
    assert kwargs.get("exc") is boom


def test_record_failure_semantic_path_forwards_no_exception():
    """Semantic (no-exc) failure forwards exc=None -> stays off the reflex bus."""
    mgr = _make_manager()
    rt = MagicMock()
    with patch("genesis.runtime.GenesisRuntime") as GR:
        GR.instance.return_value = rt
        mgr._record_failure("unified cycle returned None")

    rt.record_job_failure.assert_called_once()
    _, kwargs = rt.record_job_failure.call_args
    assert kwargs.get("exc") is None


def test_record_failure_still_increments_circuit_breaker():
    """The circuit-breaker accounting is unchanged by the exc threading."""
    mgr = _make_manager()
    rt = MagicMock()
    with patch("genesis.runtime.GenesisRuntime") as GR:
        GR.instance.return_value = rt
        mgr._record_failure("boom", exc=RuntimeError("x"))
    assert mgr._consecutive_failures == 1
