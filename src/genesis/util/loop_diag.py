"""Best-effort event-loop default-executor diagnostics.

``asyncio.to_thread()`` dispatches work onto the running loop's default
``ThreadPoolExecutor``. When background ``to_thread`` work (awareness-tick git
ops, Qdrant recall calls, embedding backfills) saturates that pool, a queued
call stalls while looking like a "slow await" — and the loop itself is idle, so
the loop-lag sampler sees nothing. That is the failure mode the loop-lag
sampler cannot distinguish on its own: *executor* starvation vs *loop*
starvation behind a recall 503.

:func:`default_executor_pending` reads the executor's pending-work depth and
worker counts so a 503 (or a lag episode) can be attributed to executor
saturation. It touches private CPython attributes
(``loop._default_executor`` and ``ThreadPoolExecutor._work_queue`` /
``_threads`` / ``_max_workers``) — stable across CPython 3.12 but wrapped in a
blanket guard so any shape change, or a not-yet-created executor (lazily built
by the first ``to_thread``), returns ``None`` instead of raising into a caller.
This is diagnostic-only: it must never affect control flow.
"""

from __future__ import annotations

import asyncio


def default_executor_pending() -> dict[str, int] | None:
    """Return the running loop's default-executor pressure, or ``None``.

    ``{"pending": <queued calls>, "workers": <live threads>,
    "max_workers": <cap>}``. ``pending`` is the count of submitted calls not yet
    picked up by a worker — a sustained non-zero value means every worker is
    busy and ``to_thread`` work is backing up (the saturation signal). Returns
    ``None`` outside a running loop, before the executor exists, or on any
    attribute-shape change (best-effort; never raises).
    """
    try:
        loop = asyncio.get_running_loop()
        executor = loop._default_executor  # type: ignore[attr-defined]
        if executor is None:
            return None
        work_queue = executor._work_queue  # type: ignore[attr-defined]
        return {
            "pending": work_queue.qsize(),
            "workers": len(executor._threads),  # type: ignore[attr-defined]
            "max_workers": executor._max_workers,  # type: ignore[attr-defined]
        }
    except Exception:
        return None
