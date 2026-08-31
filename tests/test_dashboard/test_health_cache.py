"""Route-level contract for /api/genesis/health freshness.

The snapshot cache itself lives in `HealthDataService` and its behaviour is
covered by tests/test_observability/test_health_snapshot_cache.py. What this
file covers is the part that is genuinely the ROUTE's: the freshness policy it
chooses, the per-request keys it must not leak into the shared snapshot, and —
the half that produced real defects — getting an invalidation from a Flask
request thread onto the runtime event loop.

That last point is why these tests spin real loops in real threads. The
dashboard is threaded WSGI Flask: an async route reaches invalidation from the
worker thread that bridges with `run_coroutine_threadsafe`, and a sync route
(`provider_toggle`) has no running loop at all. The previous design let both
touch producer state directly, which is how a cross-thread double-read turned a
successful provider toggle into a 500.
"""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from flask import Flask

from genesis.dashboard.routes import health as health_route
from genesis.observability.health_data import HealthDataService


def _loop_in_thread():
    """A real event loop running in its own thread, with its thread id.

    Returned rather than fixtured so each test can shut it down explicitly in a
    `finally` — a leaked running loop makes every later test in the process
    flaky in a way that is very hard to attribute.
    """
    loop = asyncio.new_event_loop()
    ident: dict = {}
    ready = threading.Event()

    def _run():
        ident["thread"] = threading.get_ident()
        asyncio.set_event_loop(loop)
        ready.set()
        loop.run_forever()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    ready.wait(timeout=5)
    return loop, t, ident


def _stop(loop, t):
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=5)
    loop.close()


def _runtime_with(health_data):
    rt = MagicMock()
    rt.is_bootstrapped = True
    rt.health_data = health_data
    return rt


# --------------------------------------------------------------------------
# Freshness policy + per-request keys
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_route_opts_into_the_cache_and_owns_the_ttl():
    """The route is the only caller that accepts a stale snapshot.

    Every other caller (ego context, sentinel, the alert collector) computes
    fresh, so the staleness window must be requested explicitly here rather than
    being a default the producer applies to everyone.
    """
    hd = MagicMock()
    hd.snapshot = AsyncMock(return_value={"infrastructure": {}})
    app = Flask(__name__)

    with patch("genesis.runtime.GenesisRuntime") as GR:
        GR.instance.return_value = _runtime_with(hd)
        with app.test_request_context("/api/genesis/health"):
            await health_route.health_snapshot.__wrapped__()

    assert hd.snapshot.await_args.kwargs.get("max_age_s") == health_route._SNAPSHOT_CACHE_TTL_S, (
        "the route did not request its own freshness window — either it is "
        "recomputing a ~2s snapshot on every 15s poll from every open tab, or "
        "it is accepting a default staleness other callers must not have"
    )


@pytest.mark.asyncio
async def test_per_request_keys_never_reach_the_shared_snapshot():
    """`bridge` and `status` are per-request and must not taint the cache.

    The producer hands out the SAME dict to coalesced callers by contract, so
    mutating it here would publish this request's bridge status to every other
    reader — and into the cache, for the rest of the window.
    """
    svc = HealthDataService()

    async def _compute():
        return {"infrastructure": {"genesis.db": {"status": "healthy"}}}

    svc._compute_snapshot = _compute
    app = Flask(__name__)

    with patch("genesis.runtime.GenesisRuntime") as GR:
        GR.instance.return_value = _runtime_with(svc)
        with app.test_request_context("/api/genesis/health"):
            await health_route.health_snapshot.__wrapped__()

    assert svc._cache is not None, "nothing was cached, so this proves nothing"
    assert "bridge" not in svc._cache, "per-request bridge status leaked into the shared snapshot"
    assert "status" not in svc._cache, "per-request verdict leaked into the shared snapshot"


# --------------------------------------------------------------------------
# Getting the invalidation onto the loop
# --------------------------------------------------------------------------

def test_invalidate_is_marshalled_onto_the_runtime_loop():
    """Invalidation from a request thread must RUN on the loop thread.

    This is the structural fix for the class that produced a cross-thread
    double-read crash and a TOCTOU on the publish guard. Rather than making each
    field individually thread-safe, every field is touched from one thread — so
    the hop is the invariant worth pinning.
    """
    loop, t, ident = _loop_in_thread()
    try:
        seen: dict = {}
        hd = MagicMock()
        hd.invalidate.side_effect = lambda: seen.setdefault("thread", threading.get_ident())

        app = Flask(__name__)
        app.config["GENESIS_EVENT_LOOP"] = loop

        with patch("genesis.runtime.GenesisRuntime") as GR:
            GR.instance.return_value = _runtime_with(hd)
            with app.app_context():
                health_route.invalidate_snapshot_cache()

        deadline = time.monotonic() + 5.0
        while "thread" not in seen and time.monotonic() < deadline:
            time.sleep(0.005)

        assert "thread" in seen, "invalidate() never ran — the hop was dropped"
        assert seen["thread"] == ident["thread"], (
            "invalidate() ran on the calling thread instead of the runtime loop, "
            "so producer state is being touched from two threads again"
        )
        assert seen["thread"] != threading.get_ident(), "the test thread ran it directly"
    finally:
        _stop(loop, t)


def test_invalidate_lands_before_a_later_dispatched_request():
    """The hop is ORDERED, not merely eventual.

    A sync route returns as soon as it has queued the invalidation, so the
    client's next request is dispatched afterwards. If that request could
    overtake the queued callback, the toggle would answer with pre-mutation
    state and the whole exercise would be pointless.

    Both are queued with `call_soon_threadsafe`, which appends to one FIFO ready
    queue, so the invalidation runs first. This asserts that end to end against a
    real loop rather than trusting the reasoning.
    """
    loop, t, ident = _loop_in_thread()
    try:
        svc = HealthDataService()
        gate = asyncio.Event()
        state = {"n": 0}

        async def _compute():
            state["n"] += 1
            n = state["n"]
            await gate.wait()
            return {"n": n}

        svc._compute_snapshot = _compute

        app = Flask(__name__)
        app.config["GENESIS_EVENT_LOOP"] = loop

        # A compute is in flight, started BEFORE the mutation.
        first = asyncio.run_coroutine_threadsafe(svc.snapshot(max_age_s=30.0), loop)
        deadline = time.monotonic() + 5.0
        while state["n"] == 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert state["n"] == 1, "the pre-mutation compute never started"

        # The mutation commits, then the route queues its invalidation.
        with patch("genesis.runtime.GenesisRuntime") as GR:
            GR.instance.return_value = _runtime_with(svc)
            with app.app_context():
                health_route.invalidate_snapshot_cache()

        # The client's next request, dispatched after the route returned.
        second = asyncio.run_coroutine_threadsafe(svc.snapshot(max_age_s=30.0), loop)

        loop.call_soon_threadsafe(gate.set)

        assert first.result(timeout=5)["n"] == 1
        assert second.result(timeout=5)["n"] == 2, (
            "the request dispatched after the invalidation was served the "
            "pre-mutation compute — the queued invalidate was overtaken"
        )
    finally:
        _stop(loop, t)


def test_invalidate_survives_a_closed_loop():
    """Shutdown must not turn a committed mutation into a 500.

    This runs after the caller's DELETE has already committed, so raising here
    would report failure for work that succeeded.
    """
    loop, t, _ = _loop_in_thread()
    _stop(loop, t)                       # closed before we use it

    hd = MagicMock()
    app = Flask(__name__)
    app.config["GENESIS_EVENT_LOOP"] = loop

    with patch("genesis.runtime.GenesisRuntime") as GR:
        GR.instance.return_value = _runtime_with(hd)
        with app.app_context():
            health_route.invalidate_snapshot_cache()      # must not raise

    assert hd.invalidate.called, (
        "the closed-loop fallback did not invalidate at all, so the next poll "
        "would still be served pre-mutation data"
    )


def test_invalidation_reaches_the_producer():
    """`invalidate_snapshot_cache()` must actually CALL into the producer.

    Asserted as an EDGE, deliberately. A behaviour test cannot catch a severed
    wire, because it supplies the missing signal itself: the previous suite
    called the invalidator and then the producer hook by hand, and replacing the
    real call with `pass` left every test green.
    """
    hd = MagicMock()

    with patch("genesis.runtime.GenesisRuntime") as GR:
        GR.instance.return_value = _runtime_with(hd)
        health_route.invalidate_snapshot_cache()

    assert hd.invalidate.called, (
        "invalidation never reached the producer, so the in-flight compute is "
        "still published and later callers are still served pre-mutation data"
    )


def test_invalidation_tolerates_a_producer_without_the_hook():
    """A runtime whose health_data predates the hook must not 500 the mutation."""
    rt = _runtime_with(object())          # no invalidate attribute

    with patch("genesis.runtime.GenesisRuntime") as GR:
        GR.instance.return_value = rt
        health_route.invalidate_snapshot_cache()          # must not raise


# --------------------------------------------------------------------------
# Route -> invalidator wiring, one test per mutating route
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_clear_all_deferred_invalidates_the_snapshot_cache():
    """Guards the wiring: a correct invalidator nobody calls fixes nothing."""
    from genesis.dashboard.routes import errors as errors_route

    cur = MagicMock()
    cur.rowcount = 148

    async def _execute(*_a, **_k):
        return cur

    async def _commit():
        return None

    rt = MagicMock()
    rt.is_bootstrapped = True
    rt.db = MagicMock()
    rt.db.execute = _execute
    rt.db.commit = _commit

    app = Flask(__name__)
    with patch("genesis.runtime.GenesisRuntime") as GR:
        GR.instance.return_value = rt
        with (
            patch.object(health_route, "invalidate_snapshot_cache") as inval,
            app.test_request_context("/api/genesis/deferred/all/clear", method="DELETE"),
        ):
            resp = await errors_route.clear_deferred_item.__wrapped__("all")

    assert inval.called, "clear-all did not invalidate the health snapshot cache"
    payload = resp[0].get_json() if isinstance(resp, tuple) else resp.get_json()
    assert payload["cleared"] == 148


@pytest.mark.asyncio
async def test_set_budget_invalidates_the_snapshot_cache():
    """The cost card and the budget-pressure attention item read `budgets`."""
    from genesis.dashboard.routes import budget as budget_route

    async def _execute(*_a, **_k):
        return MagicMock()

    async def _commit():
        return None

    rt = MagicMock()
    rt.is_bootstrapped = True
    rt.db = MagicMock()
    rt.db.execute = _execute
    rt.db.commit = _commit

    app = Flask(__name__)
    with patch("genesis.runtime.GenesisRuntime") as GR:
        GR.instance.return_value = rt
        with (
            patch.object(health_route, "invalidate_snapshot_cache") as inval,
            app.test_request_context(
                "/api/genesis/budgets",
                method="POST",
                json={"budget_type": "monthly", "limit_usd": 100},
            ),
        ):
            await budget_route.set_budget.__wrapped__()

    assert inval.called, (
        "saving a budget did not invalidate, so the cost card keeps showing the "
        "previous limit for a full window"
    )


def test_provider_toggle_invalidates_the_snapshot_cache():
    """The toggle is a SYNC route — it runs on a Flask worker thread.

    That is the reason invalidation must marshal onto the loop rather than
    touching producer state directly, and it is the route that made the
    cross-thread hazard real. Pinning the edge here keeps the constraint visible.
    """
    from genesis.dashboard.routes import providers as providers_route
    from genesis.routing.types import ProviderState

    cb = MagicMock()
    cb.state = ProviderState.OPEN          # take the re-enable branch

    breakers = MagicMock()
    breakers.get.return_value = cb

    rt = MagicMock()
    rt.is_bootstrapped = True
    rt.circuit_breakers = breakers

    app = Flask(__name__)
    with patch("genesis.runtime.GenesisRuntime") as GR:
        GR.instance.return_value = rt
        with (
            patch.object(health_route, "invalidate_snapshot_cache") as inval,
            app.test_request_context("/api/genesis/providers/qdrant/toggle", method="POST"),
        ):
            providers_route.provider_toggle("qdrant")

    assert inval.called, (
        "toggling a provider did not invalidate, so the Call Sites card and the "
        "attention strip keep the pre-toggle status"
    )
