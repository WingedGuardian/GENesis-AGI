"""Freshness and single-flight contract for `HealthDataService.snapshot()`.

The snapshot is expensive (~2s: systemd subprocesses + DB + network), so it is
cached for the dashboard poll and coalesced across overlapping callers. Those
two optimisations plus mutation-invalidation interact, and every defect this
suite pins came from that interaction rather than from any one of them.

The mechanism these tests describe replaced one split across two modules and two
threads (a cache in the route module, a generation counter, and a boolean stale
marker in the service). Three review rounds each found a new defect in it, and
round 1's fix caused round 2's. The replacement holds all of the state in this
one object, touched only from the event loop, and expresses staleness as the
IDENTITY of the task that predates the mutation rather than as a flag — so there
is no "clear the marker" step, which is where round 2's defect lived.

Two invariants are load-bearing and each has its own tests below:

1. AT MOST ONE compute runs at a time. `ProbeTransitionTracker` takes no lock,
   explicitly on the strength of this promise (see its docstring), so two
   overlapping computes let the older finish last and emit a false REVERSE
   health transition.
2. No caller that arrives after a mutation is handed data computed before it,
   and no such data reaches the cache — where it would be served for a full TTL.
"""

from __future__ import annotations

import asyncio

import pytest

from genesis.observability.health_data import HealthDataService


async def _until(cond, what, *, timeout: float = 2.0):
    """Poll the CONDITION on a bounded deadline — never a fixed sleep.

    `snapshot()` is a coroutine that then creates the compute task, so the
    number of loop turns before the compute body runs is an implementation
    detail. Asserting after a fixed number of yields is flaky by construction,
    and an earlier version of these tests did exactly that and passed against a
    broken implementation.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if cond():
            return
        await asyncio.sleep(0)
    raise AssertionError(f"timed out waiting for {what}")


async def _settles(cond, window: float) -> bool:
    """True if `cond` becomes true within `window` seconds.

    The inverse of `_until`, for asserting something does NOT happen. A single
    `await asyncio.sleep(0)` is not enough: a freshly created task needs several
    loop turns to reach its first await, so one yield reports "didn't happen"
    even when it was about to.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + window
    while loop.time() < deadline:
        if cond():
            return True
        await asyncio.sleep(0.01)
    return False


def _gated_service(results=None):
    """A real service whose compute is blocked on a caller-held gate.

    Uses the real class (every constructor argument is optional) rather than
    `object.__new__`, so a field added to `__init__` cannot leave these tests
    exercising a half-built object.

    `state` records how many computes ran and — the property single-flight
    actually promises — the most that were ever live simultaneously. Counting
    starts, not overlap, would pass against a broken implementation.
    """
    svc = HealthDataService()
    gate = asyncio.Event()
    state = {"n": 0, "live": 0, "peak": 0}

    async def _compute():
        state["n"] += 1
        state["live"] += 1
        state["peak"] = max(state["peak"], state["live"])
        n = state["n"]
        try:
            await gate.wait()
            return results(n) if results is not None else {"n": n}
        finally:
            state["live"] -= 1

    svc._compute_snapshot = _compute
    return svc, gate, state


# --------------------------------------------------------------------------
# Caching
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cache_hit_within_max_age_computes_once():
    """Two callers inside the window share one compute."""
    svc, gate, state = _gated_service()
    gate.set()

    a = await svc.snapshot(max_age_s=30.0)
    b = await svc.snapshot(max_age_s=30.0)

    assert state["n"] == 1, f"expected one compute, got {state['n']}"
    assert a == b


@pytest.mark.asyncio
async def test_cache_expires_after_max_age():
    svc, gate, state = _gated_service()
    gate.set()

    await svc.snapshot(max_age_s=30.0)
    svc._cache_ts -= 31.0                      # age the entry past the window
    await svc.snapshot(max_age_s=30.0)

    assert state["n"] == 2


@pytest.mark.asyncio
async def test_default_caller_never_reads_the_cache():
    """`max_age_s` defaults to 0, so every existing caller keeps today's behaviour.

    Seven production call sites call `snapshot()` bare (measured): both ego
    context builders, the sentinel dispatcher, the morning report, the alert
    collector, `health_status`, and the Agent Zero health route. Only the
    dashboard route opts into staleness. If the default served cached data, the
    alert collector would fire and clear alerts on input it never gathered.
    """
    svc, gate, state = _gated_service()
    gate.set()

    await svc.snapshot(max_age_s=30.0)         # populate
    await svc.snapshot()                       # default: must recompute

    assert state["n"] == 2, "a bare snapshot() call was served from the cache"


@pytest.mark.asyncio
async def test_invalidate_forces_a_recompute_inside_the_window():
    svc, gate, state = _gated_service()
    gate.set()

    await svc.snapshot(max_age_s=30.0)
    svc.invalidate()
    await svc.snapshot(max_age_s=30.0)

    assert state["n"] == 2, "invalidate() did not drop the cached snapshot"


# --------------------------------------------------------------------------
# Staleness — the class that produced three review rounds
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_inflight_compute_does_not_publish_after_an_invalidation():
    """A compute that began before a mutation must never reach the cache.

    It is not enough for the mutating caller to get fresh data: if the
    pre-mutation result is cached, every reader is served it for the rest of the
    TTL, which is the 30-second window where a cleared queue kept listing the
    rows it had just deleted.
    """
    svc, gate, state = _gated_service()

    first = asyncio.create_task(svc.snapshot(max_age_s=30.0))
    await _until(lambda: state["n"] == 1, "the pre-mutation compute to start")

    svc.invalidate()                            # the mutation lands mid-compute
    gate.set()
    await first

    assert svc._cache is None, (
        "a compute that predates the mutation published itself — it would be "
        "served as current for a full TTL"
    )


@pytest.mark.asyncio
async def test_every_post_mutation_caller_waits_for_the_replacement():
    """Two callers arriving after a mutation both get post-mutation data.

    The regression this pins: the stale marker used to be cleared by the first
    caller that noticed it, while the handle still pointed at the very compute
    it had flagged — so the NEXT caller read the marker as clear, took the
    ordinary coalescing branch, and was handed exactly the pre-mutation snapshot
    the marker existed to withhold. Identity-based staleness has no such step.

    Also asserts the pair share ONE replacement compute and that peak
    concurrency stays 1, so the fix cannot buy freshness by breaking
    single-flight.
    """
    svc, gate, state = _gated_service(
        lambda n: {"discarded": 148 if n == 1 else 0}   # #1 predates the mutation
    )

    first = asyncio.create_task(svc.snapshot())
    await _until(lambda: state["n"] == 1, "the pre-mutation compute to start")

    svc.invalidate()

    second = asyncio.create_task(svc.snapshot())
    third = asyncio.create_task(svc.snapshot())

    assert not await _settles(lambda: state["n"] > 1, 0.4), (
        "a second compute started while the first was still running — that "
        "breaks the single-flight contract ProbeTransitionTracker relies on"
    )

    gate.set()
    a, b, c = await first, await second, await third

    assert state["peak"] == 1, (
        f"{state['peak']} computes ran concurrently — an older compute finishing "
        "last can emit a false reverse probe transition"
    )
    assert state["n"] == 2, (
        f"expected ONE replacement compute shared by both post-mutation callers, "
        f"got {state['n']}"
    )
    assert a["discarded"] == 148, "the pre-mutation caller keeps its own result"
    assert b["discarded"] == 0, "the first post-mutation caller was served stale data"
    assert c["discarded"] == 0, (
        "the SECOND post-mutation caller was handed the pre-mutation snapshot — "
        "the exact defect identity-based staleness exists to make unrepresentable"
    )


@pytest.mark.asyncio
async def test_publish_decision_is_atomic_with_the_compute_finishing():
    """An invalidation queued before the compute lands must still suppress it.

    What this actually pins is a REDUNDANT PAIR, and the distinction was
    established by mutation rather than assumed. The service both publishes from
    inside the compute task AND marks a stale compute unconditionally (including
    one already finished). Removing either alone leaves this test green, because
    each independently closes the window; removing BOTH fails here.

    So read a green here as "at least one of the two protections is intact" —
    not as proof that the publish is in-task. The value is that it catches the
    combination, which is the state where a compute that predates a mutation
    reaches the cache and is served as current for a full window.
    """
    svc, gate, state = _gated_service()

    async def _compute_then_invalidate():
        state["n"] += 1
        await gate.wait()
        # Queue the invalidation so it is already pending when this compute
        # finishes — the ordering a done-callback publish would lose to.
        asyncio.get_running_loop().call_soon(svc.invalidate)
        return {"n": state["n"]}

    svc._compute_snapshot = _compute_then_invalidate

    task = asyncio.create_task(svc.snapshot(max_age_s=30.0))
    await _until(lambda: state["n"] == 1, "the compute to start")
    gate.set()
    await task
    await asyncio.sleep(0)          # let the queued invalidate run

    assert svc._cache is None, (
        "the snapshot was published into a cache that an already-queued "
        "invalidation then cleared — the publish must be atomic with the "
        "compute finishing, not deferred to a done-callback"
    )


@pytest.mark.asyncio
async def test_two_invalidations_during_one_compute_stay_single_flight():
    svc, gate, state = _gated_service(lambda n: {"n": n})

    first = asyncio.create_task(svc.snapshot())
    await _until(lambda: state["n"] == 1, "the first compute to start")

    svc.invalidate()
    svc.invalidate()                            # idempotent, no extra compute

    second = asyncio.create_task(svc.snapshot())
    gate.set()
    await first
    result = await second

    assert state["peak"] == 1, "single-flight broken under repeated invalidation"
    assert result["n"] > 1, "the post-mutation caller got the pre-mutation compute"


@pytest.mark.asyncio
async def test_caller_cancellation_does_not_abort_the_shared_compute():
    """One caller's timeout must not cancel the compute others are waiting on.

    The dashboard route cancels its own await when `_async_route` times out.
    `asyncio.shield` keeps the shared compute alive, so the siblings still get a
    result and the cache is still populated.
    """
    svc, gate, state = _gated_service()

    doomed = asyncio.create_task(svc.snapshot(max_age_s=30.0))
    await _until(lambda: state["n"] == 1, "the compute to start")
    sibling = asyncio.create_task(svc.snapshot(max_age_s=30.0))
    await asyncio.sleep(0)

    doomed.cancel()
    with pytest.raises(asyncio.CancelledError):
        await doomed

    gate.set()
    assert await sibling is not None, "the surviving caller lost its result"
    assert state["n"] == 1, "the cancellation triggered a redundant recompute"
    assert svc._cache is not None, "the compute completed but never published"


@pytest.mark.asyncio
async def test_a_failing_compute_propagates_and_does_not_wedge_the_service():
    """A raising compute reaches its callers, releases the handle, and retries.

    The cache must not be poisoned, and the next caller must get a real attempt
    rather than a permanently stuck in-flight handle.
    """
    svc = HealthDataService()
    calls = {"n": 0}

    async def _compute():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("probe failed")
        return {"ok": True}

    svc._compute_snapshot = _compute

    with pytest.raises(RuntimeError, match="probe failed"):
        await svc.snapshot()

    assert svc._cache is None, "a failed compute must not populate the cache"
    assert svc._inflight is None or svc._inflight.done(), "the handle was not released"
    assert await svc.snapshot() == {"ok": True}, "the service did not recover"


@pytest.mark.asyncio
async def test_cancelling_a_coalesced_caller_does_not_kill_the_shared_compute():
    """A coalesced caller must await through a shield, not the task directly.

    Awaiting a Task directly links the two: cancelling the waiter cancels the
    awaited task (`Task.cancel` cancels its `_fut_waiter`). So a dashboard
    request that hits its `_async_route` deadline would abort the shared compute
    out from under the ego context builder and the sentinel that coalesced onto
    it — one client's timeout becoming everyone's failure.

    Distinct from cancelling the caller that CREATED the compute, which exercises
    a different `shield` call; both branches need one, and a mutation removing
    only the coalescing branch's shield survived a test that cancelled the
    creator.
    """
    svc, gate, state = _gated_service()

    owner = asyncio.create_task(svc.snapshot())
    await _until(lambda: state["n"] == 1, "the compute to start")

    coalesced = asyncio.create_task(svc.snapshot())
    await _settles(lambda: False, 0.05)          # let it reach its await
    assert state["n"] == 1, "the second caller started its own compute"

    coalesced.cancel()
    with pytest.raises(asyncio.CancelledError):
        await coalesced

    gate.set()
    assert await owner is not None, (
        "cancelling a COALESCED caller killed the shared compute — the owner "
        "and every other coalesced reader lose their result"
    )
    assert state["n"] == 1, "the surviving caller was forced into a recompute"


@pytest.mark.asyncio
async def test_a_post_mutation_caller_recovers_from_a_failing_stale_compute():
    """Draining a stale compute must absorb its failure, not inherit it.

    The pre-mutation compute's exception belongs to the callers that asked for
    it. A caller arriving after the mutation is only waiting for that compute to
    clear so it can start a fresh one — inheriting an error it never asked for
    would turn one failed probe into a failure for every later reader.
    """
    svc = HealthDataService()
    gate = asyncio.Event()
    calls = {"n": 0}

    async def _compute():
        calls["n"] += 1
        n = calls["n"]
        if n == 1:
            await gate.wait()
            raise RuntimeError("pre-mutation compute failed")
        return {"n": n}

    svc._compute_snapshot = _compute

    first = asyncio.create_task(svc.snapshot())
    await _until(lambda: calls["n"] == 1, "the pre-mutation compute to start")

    svc.invalidate()
    second = asyncio.create_task(svc.snapshot())
    gate.set()

    with pytest.raises(RuntimeError, match="pre-mutation compute failed"):
        await first

    assert await second == {"n": 2}, (
        "the post-mutation caller inherited the failure of the compute it was "
        "only draining, instead of running its own"
    )


@pytest.mark.asyncio
async def test_the_cache_predicate_never_returns_none_when_invalidated_mid_read():
    """A concurrent `invalidate()` between the predicate's two cache reads.

    `snapshot()` reads `self._cache` TWICE on the fast path — once to test it
    for None, once to return it — with a `time.monotonic()` call in between.
    Every field the service owns is normally written on the loop thread, which
    makes that safe; but `invalidate_snapshot_cache()` documents a LOOP-LESS
    fallback (no `GENESIS_EVENT_LOOP` configured — the Agent Zero plugin host,
    and any embedded host) where it calls `invalidate()` directly from the
    mutating Flask request thread while a health request runs on another. A
    switch landing between those two reads then returns None, and the route's
    `dict(await ...snapshot(...))` raises TypeError on a request that had
    already committed its mutation.

    The interleaving is made deterministic rather than raced: `time.monotonic`
    is the one call the predicate makes BETWEEN the two reads, so a stub that
    invalidates from inside it reproduces exactly the window, with no timing
    dependence. This is the same double-read shape already fixed once on this
    branch at `mark_inflight_stale()` — the class, not that instance.
    """
    import genesis.observability.health_data as hd

    svc, gate, state = _gated_service()
    gate.set()

    warm = await svc.snapshot(max_age_s=30.0)
    assert svc._cache is not None, "precondition: the cache must be warm"

    real_monotonic = hd.time.monotonic
    fired: list[int] = []

    def _invalidating_monotonic():
        # Fires once, on the predicate's read — modelling the other thread.
        #
        # It performs the FIRST assignment of `invalidate()` and not the second,
        # which is the interleaving that actually bites: `invalidate()` writes
        # `_cache = None` and then `_cache_ts = 0.0` as two separate stores, so
        # a reader suspended between them sees a nulled cache alongside a still
        # fresh timestamp — the predicate passes and the return re-reads None.
        # Calling the whole of `invalidate()` here does NOT reproduce it: the
        # zeroed timestamp makes the age check fail and the caller recomputes,
        # which is why an earlier version of this test passed against the bug.
        if not fired:
            fired.append(1)
            svc._cache = None
        return real_monotonic()

    hd.time.monotonic = _invalidating_monotonic
    try:
        result = await svc.snapshot(max_age_s=30.0)
    finally:
        hd.time.monotonic = real_monotonic

    assert fired, "the stub never ran — the predicate no longer reads the clock here"
    assert result is not None, (
        "snapshot() returned None: the fast path re-read self._cache after a "
        "concurrent invalidate() nulled it, which makes the route raise in dict(None)"
    )
    assert isinstance(result, dict), f"expected a snapshot dict, got {type(result)!r}"
    assert result == warm or state["n"] == 2, (
        "the result is neither the value captured before the invalidation nor a "
        "freshly computed one — it came from somewhere it should not have"
    )
