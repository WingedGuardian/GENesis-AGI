"""Recall rerank resilience: rate-gate + timeout-only circuit breaker
(follow-up ac27b693, PR-3).

`_maybe_rerank` now consults an optional shared rate gate and circuit breaker
before calling Voyage, so under free-tier RPM pressure it skips straight to the
RRF+graph floor instead of burning a 429, and skips during a genuine Voyage
hang. Only a timebox expiry (a real hang) feeds the breaker — a 429/empty return
never trips it (the gate is the RPM brake). These tests pin every branch plus
the kill-switch byte-parity (gate/breaker None → today's unguarded behavior).
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.memory.retrieval import HybridRetriever
from genesis.routing.circuit_breaker import CircuitBreaker
from genesis.routing.rate_gate import ProviderRateGate
from genesis.routing.types import ErrorCategory, ProviderConfig


def _real_gate(rpm: int = 3) -> ProviderRateGate:
    return ProviderRateGate("voyage", rpm=rpm)


def _real_breaker() -> CircuitBreaker:
    return CircuitBreaker(
        ProviderConfig(
            name="voyage",
            provider_type="rerank",
            model_id="rerank-2.5",
            is_free=True,
            rpm_limit=3,
            open_duration_s=120,
        )
    )


_FUSED = {"m1": 0.05, "m2": 0.03}
_QBI = {
    "m1": {"payload": {"content": "text one"}},
    "m2": {"payload": {"content": "text two"}},
}


def _reranker(rerank_fn) -> MagicMock:
    rr = MagicMock()
    rr.enabled = True
    rr.rerank = rerank_fn
    return rr


def _breaker(*, available: bool = True) -> MagicMock:
    cb = MagicMock()
    cb.is_available.return_value = available
    cb.record_success = MagicMock()
    cb.record_failure = MagicMock()
    return cb


def _gate(*, granted: bool = True) -> MagicMock:
    g = MagicMock()
    g.try_acquire = AsyncMock(return_value=granted)
    return g


def _retriever(*, reranker=None, gate=None, breaker=None) -> HybridRetriever:
    embed = MagicMock()
    embed.embed = AsyncMock(return_value=[0.1] * 1024)
    return HybridRetriever(
        embedding_provider=embed,
        qdrant_client=MagicMock(),
        db=MagicMock(),
        reranker=reranker,
        rerank_gate=gate,
        rerank_breaker=breaker,
    )


async def _run(retriever, *, timeout_s=None):
    fused = dict(_FUSED)
    stats: dict = {}
    result = await retriever._maybe_rerank(
        query="q",
        fused=fused,
        qdrant_by_id=_QBI,
        fts_by_id={},
        limit=5,
        rerank=True,
        timeout_s=timeout_s,
        stats=stats,
    )
    return result, fused, stats


@pytest.mark.asyncio
async def test_breaker_open_skips_without_calling_voyage():
    rr = _reranker(AsyncMock(return_value=[{"id": "m1", "score": 0.9}]))
    gate = _gate(granted=True)
    breaker = _breaker(available=False)
    r = _retriever(reranker=rr, gate=gate, breaker=breaker)

    result, fused, stats = await _run(r)

    assert result is fused  # unchanged → RRF+graph floor
    assert stats["rerank_skipped_breaker_open"] is True
    rr.rerank.assert_not_called()
    gate.try_acquire.assert_not_called()  # breaker checked first, short-circuits
    breaker.record_failure.assert_not_called()


@pytest.mark.asyncio
async def test_gate_denied_skips_instantly_to_rrf():
    rr = _reranker(AsyncMock(return_value=[{"id": "m1", "score": 0.9}]))
    gate = _gate(granted=False)
    breaker = _breaker(available=True)
    r = _retriever(reranker=rr, gate=gate, breaker=breaker)

    result, fused, stats = await _run(r)

    assert result is fused
    assert stats["rerank_skipped_ratelimited"] is True
    rr.rerank.assert_not_called()  # no 429 burned — never called Voyage
    breaker.record_failure.assert_not_called()
    breaker.record_success.assert_not_called()


@pytest.mark.asyncio
async def test_success_records_success_and_applies_rerank():
    rr = _reranker(AsyncMock(return_value=[{"id": "m1", "score": 0.9}]))
    gate = _gate(granted=True)
    breaker = _breaker(available=True)
    r = _retriever(reranker=rr, gate=gate, breaker=breaker)

    result, fused, stats = await _run(r)

    assert result is not fused  # rebuilt from reranker order
    assert result == {"m1": 1.0}
    assert stats["rerank_executed"] is True
    breaker.record_success.assert_called_once()
    breaker.record_failure.assert_not_called()


@pytest.mark.asyncio
async def test_timeout_records_failure_only():
    async def _hang(*_a, **_k):
        await asyncio.sleep(1)

    rr = _reranker(_hang)
    gate = _gate(granted=True)
    breaker = _breaker(available=True)
    r = _retriever(reranker=rr, gate=gate, breaker=breaker)

    result, fused, stats = await _run(r, timeout_s=0.01)

    assert result is fused  # kept RRF+graph
    assert stats["rerank_timed_out"] is True
    breaker.record_failure.assert_called_once_with(ErrorCategory.TIMEOUT)
    breaker.record_success.assert_not_called()


@pytest.mark.asyncio
async def test_empty_return_gives_no_breaker_signal():
    """A 429/5xx surfaces as an empty list from VoyageReranker; it must fall back
    to RRF WITHOUT touching the breaker (the gate, not the breaker, brakes 429s)."""
    rr = _reranker(AsyncMock(return_value=[]))
    gate = _gate(granted=True)
    breaker = _breaker(available=True)
    r = _retriever(reranker=rr, gate=gate, breaker=breaker)

    result, fused, stats = await _run(r)

    assert result is fused
    assert stats.get("rerank_executed") is False
    breaker.record_failure.assert_not_called()
    breaker.record_success.assert_not_called()


@pytest.mark.asyncio
async def test_kill_switch_none_is_byte_parity():
    """gate=None and breaker=None (kill switch / legacy callers): the rerank runs
    exactly as before — no gate/breaker interaction, reranker applied."""
    rr = _reranker(AsyncMock(return_value=[{"id": "m1", "score": 0.9}]))
    r = _retriever(reranker=rr, gate=None, breaker=None)

    result, fused, stats = await _run(r)

    assert result == {"m1": 1.0}
    assert stats["rerank_executed"] is True
    rr.rerank.assert_awaited_once()


# --- Integration: REAL ProviderRateGate + CircuitBreaker through the call site ---


@pytest.mark.asyncio
async def test_integration_real_gate_denies_second_call_within_interval():
    """End-to-end with the REAL gate + breaker (no mocks): the first rerank is
    granted and applied; a second within the 20s interval is gate-denied and
    skips to the RRF floor — the exact behavior under concurrent recalls at 3
    RPM. The window is then rewound (no real sleep) to prove a later call grants."""
    gate = _real_gate(rpm=3)
    breaker = _real_breaker()
    rr = _reranker(AsyncMock(return_value=[{"id": "m1", "score": 0.9}]))
    r = _retriever(reranker=rr, gate=gate, breaker=breaker)

    _, _, s1 = await _run(r)
    assert s1["rerank_executed"] is True
    assert "rerank_skipped_ratelimited" not in s1

    r2, f2, s2 = await _run(r)
    assert r2 is f2
    assert s2["rerank_skipped_ratelimited"] is True

    gate._last_request = time.monotonic() - gate.interval - 0.01
    _, _, s3 = await _run(r)
    assert s3["rerank_executed"] is True


@pytest.mark.asyncio
async def test_integration_real_breaker_trips_after_consecutive_timeouts():
    """REAL CircuitBreaker: 3 consecutive rerank timeouts (failure_threshold) trip
    it OPEN, after which reranks skip with rerank_skipped_breaker_open and Voyage
    is never called. gate=None isolates the breaker."""

    async def _hang(*_a, **_k):
        await asyncio.sleep(1)

    breaker = _real_breaker()
    r = _retriever(reranker=_reranker(_hang), gate=None, breaker=breaker)

    for _ in range(3):
        _, _, stats = await _run(r, timeout_s=0.01)
        assert stats["rerank_timed_out"] is True
    assert breaker.is_available() is False  # tripped OPEN by the 3rd timeout

    # Breaker now open → next rerank skips without calling Voyage.
    healthy = _reranker(AsyncMock(return_value=[{"id": "m1", "score": 0.9}]))
    r._reranker = healthy
    _, fused, stats = await _run(r)
    assert stats["rerank_skipped_breaker_open"] is True
    healthy.rerank.assert_not_called()
