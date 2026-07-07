"""Shared job-health guard for surplus job bodies.

Two layers, used by different job shapes:

- :func:`job_guard` — full decorator for the uniform enqueue-gate jobs
  (``gates.py``): swallow-all try/except with ``logger.exception`` +
  job-health recording. Jobs with pause checks, event emissions, or
  not-wired guards do NOT use the decorator — that per-job variance stays
  in their bodies by design (see the 2026-07-07 refactor decision: a
  fully-parametrized decorator merely relocates variance into arg
  cocktails).
- :func:`record_success` / :func:`record_failure` — swallow-safe
  job-health recording helpers for every other job body, replacing the
  hand-rolled ``try: GenesisRuntime.instance().record_job_* except: pass``
  blocks without touching the surrounding control flow.

The ``genesis.runtime`` import is function-scope and resolved at call
time — this is both the import-cycle breaker and the tests' patch seam
(``patch("genesis.runtime.GenesisRuntime")``); do not hoist it.
"""

from __future__ import annotations

import functools
import logging
from typing import Any

#: Sentinel a decorated job body returns to signal "intentionally did
#: nothing" — the guard then records neither success nor failure, so
#: disabled-by-config paths stay invisible to job_health exactly as their
#: pre-decorator early ``return`` did.
SKIP: Any = object()


def record_success(job_id: str) -> None:
    """Record a job-health success; never raises (health tracking must not
    break the job that is being tracked)."""
    try:
        from genesis.runtime import GenesisRuntime
        GenesisRuntime.instance().record_job_success(job_id)
    except Exception:
        pass


def record_failure(job_id: str, error: str) -> None:
    """Record a job-health failure; never raises."""
    try:
        from genesis.runtime import GenesisRuntime
        GenesisRuntime.instance().record_job_failure(job_id, error)
    except Exception:
        pass


def job_guard(job_id: str, fail_log: str):
    """Wrap an async job body in the uniform surplus job protocol.

    On normal return: record success (unless the body returned
    :data:`SKIP`) and propagate the body's return value. On exception:
    ``logger.exception(fail_log)`` under the body's own module logger
    (log-parity with the pre-decorator inline blocks), record the
    failure, and swallow — APScheduler job callables must not raise.
    """
    def deco(fn):
        logger = logging.getLogger(fn.__module__)

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                result = await fn(*args, **kwargs)
                if result is SKIP:
                    return None
                record_success(job_id)
                return result
            except Exception as exc:
                logger.exception(fail_log)
                record_failure(job_id, str(exc))
                return None
        return wrapper
    return deco
