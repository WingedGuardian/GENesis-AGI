"""Tests for the /api/genesis/health snapshot cache (TTL).

The full snapshot is ~2s; the route caches it briefly so frequent polls are
served instantly and don't recompute every request. These tests exercise the
undecorated coroutine (`__wrapped__`) directly so we avoid the cross-thread
event-loop machinery of _async_route and just verify the cache logic.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

from genesis.dashboard.routes import health as health_route


@pytest.fixture(autouse=True)
def _clean_snapshot_cache(monkeypatch):
    """Restore the module globals after every test.

    These are process-globals. Hand-rolled setup without teardown left the last
    test's fake snapshot in place with a LIVE monotonic timestamp, so any later
    test hitting /api/genesis/health within the TTL was served that fake instead
    of computing one — an ordering-dependent false green with no diagnostic.
    """
    monkeypatch.setattr(health_route, "_snapshot_cache", None)
    monkeypatch.setattr(health_route, "_snapshot_cache_ts", 0.0)
    monkeypatch.setattr(health_route, "_snapshot_cache_gen", 0)


def _fake_runtime(counter: dict):
    async def _snapshot():
        counter["n"] += 1
        return {"infrastructure": {"genesis.db": {"status": "healthy"}}, "timestamp": "t"}

    rt = MagicMock()
    rt.is_bootstrapped = True
    rt.health_data = MagicMock()
    rt.health_data.snapshot = _snapshot
    return rt


async def _until(cond, what, *, timeout: float = 2.0):
    """Poll the CONDITION on a bounded deadline — never a fixed sleep.

    `snapshot()` is itself a coroutine that then creates the compute task, so
    the number of loop turns before `_compute` runs is an implementation
    detail; asserting after a fixed number of yields is flaky by design (an
    earlier version of these tests did exactly that and passed against the
    broken implementation).
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if cond():
            return
        await asyncio.sleep(0)
    raise AssertionError(f"timed out waiting for {what}")


async def _settles(cond, window):
    """True if `cond` becomes true within `window` seconds.

    The inverse of _until: used to assert something does NOT happen. A bare
    `await asyncio.sleep(0)` is not enough — a freshly created task needs
    several loop turns to reach its first await, so a single yield reports
    "didn't happen" even when it was about to (an earlier version of these
    tests passed against the broken implementation for exactly that reason).
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + window
    while loop.time() < deadline:
        if cond():
            return True
        await asyncio.sleep(0.01)
    return False


def _gated_service(results):
    """A real HealthDataService whose compute is blocked on a caller-held gate.

    `results` maps the 1-based compute ordinal to the payload it returns, so a
    test can make the pre-mutation compute and the post-mutation compute
    distinguishable. `state` records how many computes ran and — the property
    single-flight actually promises — the most that were ever live at once.
    """
    from genesis.observability.health_data import HealthDataService

    svc = object.__new__(HealthDataService)
    svc._inflight = None
    svc._inflight_stale = False

    gate = asyncio.Event()
    state = {"n": 0, "live": 0, "peak": 0}

    async def _compute():
        state["n"] += 1
        state["live"] += 1
        state["peak"] = max(state["peak"], state["live"])
        n = state["n"]
        try:
            await gate.wait()
            return results(n)
        finally:
            state["live"] -= 1

    svc._compute_snapshot = _compute
    return svc, gate, state


@pytest.mark.asyncio
async def test_cache_hit_within_ttl_computes_once():
    calls = {"n": 0}
    rt = _fake_runtime(calls)
    app = Flask(__name__)

    with patch("genesis.runtime.GenesisRuntime") as GR:
        GR.instance.return_value = rt
        with app.test_request_context("/api/genesis/health"):
            await health_route.health_snapshot.__wrapped__()
            await health_route.health_snapshot.__wrapped__()

    # Two requests within the TTL → snapshot() computed exactly once.
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_cache_recomputes_after_ttl():
    calls = {"n": 0}
    rt = _fake_runtime(calls)
    app = Flask(__name__)

    with patch("genesis.runtime.GenesisRuntime") as GR:
        GR.instance.return_value = rt
        with app.test_request_context("/api/genesis/health"):
            await health_route.health_snapshot.__wrapped__()
            # Force the cache to look stale (older than the TTL).
            health_route._snapshot_cache_ts -= health_route._SNAPSHOT_CACHE_TTL_S + 1
            await health_route.health_snapshot.__wrapped__()

    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_cache_does_not_taint_on_per_request_mutation():
    """Bridge/status added per request must not pollute the cached snapshot."""
    calls = {"n": 0}
    rt = _fake_runtime(calls)
    app = Flask(__name__)

    with patch("genesis.runtime.GenesisRuntime") as GR:
        GR.instance.return_value = rt
        with app.test_request_context("/api/genesis/health"):
            await health_route.health_snapshot.__wrapped__()
            # The cached snapshot should not carry the per-request "bridge"/"status"
            assert "bridge" not in health_route._snapshot_cache
            assert "status" not in health_route._snapshot_cache


@pytest.mark.asyncio
async def test_invalidate_forces_recompute_within_ttl():
    """A mutation must be able to bust the cache before the TTL expires.

    Without this, a DELETE followed by the client's immediate refetch is served
    from the <=30s-old cache, so the UI renders a success toast beside the very
    counts it just cleared (observed 2026-08-30: "Cleared 148 discarded items"
    next to "showing 20 of 148", with dead rows still listed and clickable).
    """
    calls = {"n": 0}
    rt = _fake_runtime(calls)
    app = Flask(__name__)

    with patch("genesis.runtime.GenesisRuntime") as GR:
        GR.instance.return_value = rt
        with app.test_request_context("/api/genesis/health"):
            await health_route.health_snapshot.__wrapped__()
            assert calls["n"] == 1
            # Second call inside the TTL would normally be a cache hit...
            health_route.invalidate_snapshot_cache()
            await health_route.health_snapshot.__wrapped__()

    # ...but invalidation forced a real recompute.
    assert calls["n"] == 2, (
        "invalidate_snapshot_cache() did not force a recompute — a post-delete "
        "refetch would still render pre-delete counts."
    )


@pytest.mark.asyncio
async def test_clear_all_deferred_invalidates_the_snapshot_cache():
    """The clear-all route must actually CALL the invalidator.

    Guards the wiring, not just the helper: a correct invalidator nobody calls
    leaves the stale-panel bug exactly as it was.
    """
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
async def test_inflight_snapshot_does_not_republish_after_invalidation():
    """A snapshot begun BEFORE a mutation must not publish itself after it.

    The store-back happens after a ~2s await, and every request runs on one
    shared runtime loop, so without a generation check this sequence restores
    pre-delete counts for a full TTL — the exact symptom invalidation exists to
    prevent, but harder to spot because the toast says the clear succeeded:

        t0  poll A misses the cache, begins snapshot()  (reads 148 rows)
        t1  user clears all -> DELETE commits -> invalidate_snapshot_cache()
        t2  A completes and stores its PRE-DELETE result, timestamped now
    """
    gate = asyncio.Event()
    calls = {"n": 0}

    async def _slow_snapshot():
        calls["n"] += 1
        await gate.wait()  # still in flight while the mutation lands
        return {"infrastructure": {"stale": True}, "timestamp": "pre-delete"}

    rt = MagicMock()
    rt.is_bootstrapped = True
    rt.health_data = MagicMock()
    rt.health_data.snapshot = _slow_snapshot
    app = Flask(__name__)

    with patch("genesis.runtime.GenesisRuntime") as GR:
        GR.instance.return_value = rt
        with app.test_request_context("/api/genesis/health"):
            task = asyncio.create_task(health_route.health_snapshot.__wrapped__())
            await asyncio.sleep(0)  # let it reach the await
            assert calls["n"] == 1, "snapshot should be in flight"

            health_route.invalidate_snapshot_cache()  # the mutation
            gate.set()
            await task

    assert health_route._snapshot_cache is None, (
        "an in-flight snapshot that predates the invalidation republished "
        "itself — stale counts would be served for a further full TTL"
    )


@pytest.mark.asyncio
async def test_invalidation_abandons_the_producers_inflight_compute():
    """A caller arriving AFTER a mutation must not be handed a pre-mutation result.

    `HealthDataService.snapshot()` coalesces overlapping callers onto one
    computation. Clearing this module's cache is not enough: the post-mutation
    refetch is *handed* the in-flight pre-mutation snapshot, and because it
    sampled the generation counter AFTER the bump, the generation check passes
    and it republishes stale data with a full fresh TTL. Worse, invalidation
    nulls the cache, so every request in that window misses and coalesces onto
    that same stale compute.

    Built on the REAL service rather than a bare async stub — a stub bypasses
    the coalescer, which is exactly how this bug passed a review round green.
    """
    from genesis.observability.health_data import HealthDataService

    svc = object.__new__(HealthDataService)
    svc._inflight = None
    svc._inflight_stale = False
    gate = asyncio.Event()
    # Track CONCURRENCY semantically, not by counter-timing: `live` is the
    # number of computes inside _compute_snapshot right now, `peak` the most
    # ever simultaneous. Single-flight means peak == 1, and that holds no matter
    # how the event loop interleaves — an assertion on a start-counter after a
    # fixed number of yields does not (it passed against the broken design).
    state = {"n": 0, "live": 0, "peak": 0}

    async def _compute():
        state["n"] += 1
        state["live"] += 1
        state["peak"] = max(state["peak"], state["live"])
        n = state["n"]
        try:
            await gate.wait()
            # first compute reads pre-delete state, later ones read post-delete
            return {"queues": {"discarded_count": 148 if n == 1 else 0}}
        finally:
            state["live"] -= 1

    svc._compute_snapshot = _compute

    first = asyncio.create_task(svc.snapshot())   # in flight, pre-delete
    await _until(lambda: state["n"] == 1, "the first compute to start")

    health_route.invalidate_snapshot_cache()      # the mutation lands
    svc.mark_inflight_stale()                     # what invalidation must do

    second = asyncio.create_task(svc.snapshot())  # post-delete caller

    # THE DISCRIMINATOR. The first compute is still blocked on `gate`. If the
    # implementation abandons the in-flight handle, this caller starts a SECOND
    # compute immediately and n reaches 2 while the first is still inside
    # _compute_snapshot — two concurrent computes. If it flags the compute
    # stale instead, this caller awaits the first, so n cannot move until the
    # gate opens. Bounded wait, then assert it did NOT happen.
    started_concurrently = await _settles(lambda: state["n"] == 2, 0.4)
    assert not started_concurrently, (
        "a SECOND compute started while the first was still running — that "
        "breaks the single-flight contract ProbeTransitionTracker relies on "
        "(it takes no lock precisely because snapshot() promises single-flight), "
        "so an older compute finishing last can emit a false reverse transition"
    )

    gate.set()                                    # release the stale compute
    await _until(lambda: state["n"] == 2, "the fresh post-mutation compute")
    a, b = await first, await second

    assert state["peak"] == 1, (
        f"{state['peak']} computes ran CONCURRENTLY — that breaks the "
        "single-flight contract ProbeTransitionTracker relies on (it takes no "
        "lock precisely because snapshot() promises single-flight), so an older "
        "compute finishing last can emit a false reverse probe transition"
    )
    assert state["n"] == 2, "the post-mutation caller must get its own fresh compute"
    assert a["queues"]["discarded_count"] == 148, (
        "the pre-mutation caller keeps its own result — correct for its request"
    )
    assert b["queues"]["discarded_count"] == 0, (
        "the post-mutation caller must see post-mutation state, not the stale "
        "compute it waited on"
    )
@pytest.mark.asyncio
async def test_every_post_mutation_caller_waits_for_the_replacement_compute():
    """A SECOND post-mutation caller must not be handed the stale result.

    The regression this pins: the stale marker used to be cleared by the first
    caller that noticed it, *before* the compute it flagged had finished. The
    handle stayed pointed at that same pre-mutation task for the whole ~2s it
    was still running, so the next caller to arrive read `_inflight_stale ==
    False`, took the ordinary coalescing branch, and was handed exactly the
    pre-mutation snapshot the marker existed to withhold.

    That second caller is not hypothetical: `/api/genesis/health` sampled the
    cache generation AFTER the invalidation bumped it, so the generation guard
    passes and it republishes the stale snapshot with a full fresh 30s TTL —
    and a later fresh compute by a direct consumer does not repair the route
    cache. One user-visible symptom (a cleared queue that reappears for 30s)
    from a marker cleared one compute too early.

    So the marker must stay set until a replacement compute is actually
    installed, which puts EVERY post-mutation caller behind that one
    replacement — while still never running two computes at once, because
    `ProbeTransitionTracker` takes no lock on the strength of that promise.
    """
    svc, gate, state = _gated_service(
        # compute #1 predates the mutation, #2 and later see post-mutation state
        lambda n: {"queues": {"discarded_count": 148 if n == 1 else 0}}
    )

    first = asyncio.create_task(svc.snapshot())
    await _until(lambda: state["n"] == 1, "the pre-mutation compute to start")

    svc.mark_inflight_stale()  # the mutation lands, compute #1 still running

    second = asyncio.create_task(svc.snapshot())
    third = asyncio.create_task(svc.snapshot())

    # Both post-mutation callers must park behind the replacement rather than
    # spawning their own compute. Bounded wait, then assert it did NOT happen.
    assert not await _settles(lambda: state["n"] > 1, 0.4), (
        "a second compute started while the pre-mutation one was still "
        "running — that breaks the single-flight contract "
        "ProbeTransitionTracker relies on"
    )

    gate.set()
    a, b, c = await first, await second, await third

    assert state["peak"] == 1, (
        f"{state['peak']} computes ran CONCURRENTLY — single-flight broken, so "
        "an older compute finishing last can emit a false reverse transition"
    )
    assert state["n"] == 2, (
        f"expected exactly one replacement compute for both post-mutation "
        f"callers, got {state['n']} computes"
    )
    assert a["queues"]["discarded_count"] == 148, (
        "the pre-mutation caller keeps its own result — correct for its request"
    )
    # THE DISCRIMINATOR. `b` passed even with the marker cleared early; `c` is
    # the caller that arrived while the marker was already (wrongly) clear.
    assert b["queues"]["discarded_count"] == 0, (
        "the first post-mutation caller was served pre-mutation state"
    )
    assert c["queues"]["discarded_count"] == 0, (
        "the SECOND post-mutation caller was handed the pre-mutation snapshot: "
        "the stale marker was cleared before the replacement compute existed, "
        "so this caller coalesced straight back onto the stale task"
    )

@pytest.mark.asyncio
async def test_mark_inflight_stale_reads_the_shared_handle_exactly_once():
    """`mark_inflight_stale()` must not re-read `_inflight` for its own check.

    It runs on a Flask REQUEST thread — the dashboard is threaded WSGI and
    `provider_toggle` is a plain sync route — while `_release_inflight` nulls
    `_inflight` from the runtime loop thread. Written as
    `self._inflight is not None and not self._inflight.done()`, that check reads
    the shared attribute TWICE, so a thread switch landing between the reads
    calls `.done()` on None.

    The blast radius is what makes it worth pinning: the AttributeError escapes
    through `invalidate_snapshot_cache()` into a route that has ALREADY
    committed its mutation, so a clear/toggle that fully succeeded answers 500
    and the UI reports failure for work that was done.

    The interleaving is FORCED, not raced: `_inflight` here yields the live task
    on its first read and None on every read after — exactly what the losing
    thread observes — so this is deterministic rather than a timing lottery.
    """
    from genesis.observability.health_data import HealthDataService

    live = asyncio.create_task(asyncio.sleep(60))
    try:

        class _HandleClearedMidCheck(HealthDataService):
            reads = 0

            @property
            def _inflight(self):
                type(self).reads += 1
                return live if type(self).reads == 1 else None

            @_inflight.setter
            def _inflight(self, value):  # the loop thread's write; irrelevant here
                pass

        svc = object.__new__(_HandleClearedMidCheck)
        svc._inflight_stale = False

        svc.mark_inflight_stale()

        assert _HandleClearedMidCheck.reads == 1, (
            "mark_inflight_stale() read the shared _inflight handle "
            f"{_HandleClearedMidCheck.reads} times; it must read it once into a "
            "local, or a concurrent _release_inflight turns the second read "
            "into AttributeError on None inside a mutating route"
        )
        assert svc._inflight_stale is True, (
            "a live in-flight compute was not flagged as predating the mutation"
        )
    finally:
        live.cancel()
def test_invalidation_reaches_the_producers_coalescer():
    """`invalidate_snapshot_cache()` must actually CALL into the producer.

    The other cache tests hand-perform this wiring -- they call
    `invalidate_snapshot_cache()` and then `svc.mark_inflight_stale()`
    themselves -- so they verify the producer's BEHAVIOUR while proving nothing
    about whether invalidation reaches it. Deleting the call at
    `routes/health.py` (the third of the three things this function's own
    docstring calls "required", and the entire reason the coalescer was made
    stale-aware) left the whole suite green.

    So this asserts the EDGE, not the effect: a behaviour test cannot catch a
    severed wire, because it supplies the missing signal itself.
    """
    rt = MagicMock()
    rt.is_bootstrapped = True
    rt.health_data = MagicMock()

    with patch("genesis.runtime.GenesisRuntime") as GR:
        GR.instance.return_value = rt
        health_route.invalidate_snapshot_cache()

    assert rt.health_data.mark_inflight_stale.called, (
        "invalidate_snapshot_cache() did not reach the producer's single-flight "
        "coalescer, so a caller arriving after the mutation is still handed the "
        "pre-mutation snapshot -- clearing this module's cache alone cannot fix "
        "that, because the stale value comes from the in-flight compute"
    )


def test_invalidation_tolerates_a_producer_without_the_hook():
    """A runtime whose health_data predates the hook must not 500 the mutation.

    `invalidate_snapshot_cache()` runs AFTER the route has committed its
    delete/toggle, so anything it raises turns completed work into a 500. The
    hasattr guard is what keeps that safe; this pins it, since the wiring test
    above would otherwise invite someone to drop the guard and call directly.
    """
    rt = MagicMock()
    rt.is_bootstrapped = True
    rt.health_data = object()          # no mark_inflight_stale attribute

    with patch("genesis.runtime.GenesisRuntime") as GR:
        GR.instance.return_value = rt
        health_route.invalidate_snapshot_cache()   # must not raise

    assert health_route._snapshot_cache is None, (
        "the cache must still be cleared even when the producer has no hook"
    )
@pytest.mark.asyncio
async def test_set_budget_invalidates_the_snapshot_cache():
    """Every mutating route that feeds the snapshot must bust it, not just one.

    The cost snapshot reads `budgets` for the limit and percent-used that drive
    the cost card and the budget-pressure attention item, so a saved budget that
    does not invalidate leaves the dashboard showing the old ceiling for a full
    TTL -- the same class as the queue panel listing rows it had just deleted,
    on a different surface. Only the clear-all route was pinned; this and the
    provider toggle were wired but unheld.
    """
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
        "saving a budget did not invalidate the health snapshot cache, so the "
        "cost card keeps showing the previous limit for a full TTL"
    )


def test_provider_toggle_invalidates_the_snapshot_cache():
    """The toggle is a SYNC route -- it runs on a Flask worker thread.

    That is why invalidation must stay callable without a running event loop,
    and it is the reason the producer hook is a bare flag set rather than
    something that schedules work. Pinning the edge here keeps that constraint
    visible: if this route ever becomes the one that needs a loop, this test is
    where it shows up.
    """
    from genesis.dashboard.routes import providers as providers_route
    from genesis.routing.types import ProviderState

    cb = MagicMock()
    cb.state = ProviderState.OPEN          # so the re-enable branch runs

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
        "toggling a provider did not invalidate the health snapshot cache, so "
        "the Call Sites card and the attention strip keep the pre-toggle status"
    )
