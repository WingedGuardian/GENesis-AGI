"""Refresh orchestrator for the infrastructure body schema.

One entry point — ``refresh()`` — used by the boot task, the daily scheduler
job, and the MCP tool. Single-flight (module lock) with a short-circuit for
back-to-back calls; every stage is failure-contained so a collector, LLM, or
render problem can never break boot or the scheduler.

Stage order: collect → merge → drift → persist facts → annotate → render →
project (shared mount + CLAUDE.md block). Facts are persisted BEFORE the LLM
stage so an annotation outage still leaves a current profile on disk.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from genesis.infra_profile import claude_md, store
from genesis.infra_profile.annotate import regenerate_annotations
from genesis.infra_profile.collectors import CONTAINER_COLLECTORS
from genesis.infra_profile.collectors.host import collect_host
from genesis.infra_profile.diff import compute_drift, emit_drift_observations
from genesis.infra_profile.hashing import section_hash
from genesis.infra_profile.paths import DOC_PATH, SHARED_DOC_PATH
from genesis.infra_profile.render import headline_facts, render_document
from genesis.infra_profile.types import STATUS_OK, SectionResult
from genesis.util.atomic import atomic_write_text

logger = logging.getLogger(__name__)

_LOCK = asyncio.Lock()

# Refresh short-circuit window. Checked against the persisted profile's
# collected_at (wall clock) so it holds ACROSS processes — the MCP server and
# genesis-server each run this module, and a per-process monotonic would let
# an MCP refresh=true re-collect right after the server's daily run.
_MIN_REFRESH_INTERVAL = 300.0


def _recently_refreshed(profile: dict[str, Any]) -> bool:
    collected_at = profile.get("collected_at")
    if not collected_at:
        return False
    try:
        collected = datetime.fromisoformat(collected_at)
    except (TypeError, ValueError):
        return False
    return (datetime.now(UTC) - collected).total_seconds() < _MIN_REFRESH_INTERVAL


@contextmanager
def _cross_process_lock():
    """flock on a sidecar file — the module Lock only covers THIS process.

    The genesis-health MCP server is a separate process from genesis-server;
    without this, an MCP-triggered refresh could interleave writes with the
    daily job (architect review 2026-07-11, finding 1). Non-blocking: the
    loser skips its refresh rather than queueing a redundant one.
    """
    from genesis.infra_profile.paths import PROFILE_DIR

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = PROFILE_DIR / ".refresh.lock"
    with open(lock_path, "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _merge_section(
    result: SectionResult,
    previous: dict[str, Any] | None,
    now: str,
) -> dict[str, Any]:
    """Build the persisted section dict; failed collectors keep prior facts."""
    if result.status == STATUS_OK:
        new_hash = section_hash(result.facts)
        prev_hash = (previous or {}).get("hash")
        changed_at = (previous or {}).get("facts_changed_at")
        if new_hash != prev_hash:
            changed_at = now
        return {
            "plane": result.plane,
            "status": STATUS_OK,
            "error": None,
            "collected_at": now,
            "hash": new_hash,
            "facts_changed_at": changed_at,
            "facts": result.facts,
            "metrics": result.metrics,
        }
    if previous and previous.get("facts"):
        # Keep prior facts + hash — no phantom drift, doc notes the failure.
        kept = dict(previous)
        kept["status"] = result.status
        kept["error"] = result.error
        kept["collected_at"] = now
        return kept
    return {
        "plane": result.plane,
        "status": result.status,
        "error": result.error,
        "collected_at": now,
        "hash": None,
        "facts_changed_at": None,
        "facts": {},
        "metrics": {},
    }


async def refresh(
    reason: str,
    *,
    db=None,
    router=None,
    event_bus=None,
    guardian_remote=None,
    force: bool = False,
) -> dict[str, Any]:
    """Collect, diff, persist, annotate, render. Returns the new profile.

    Never raises for stage failures — each stage degrades independently and
    logs. Callers that want the runtime's dependencies use
    ``refresh_from_runtime``.
    """
    async with _LOCK:
        if not force:
            existing = store.load_profile()
            if _recently_refreshed(existing):
                logger.debug(
                    "infra_profile: refresh short-circuited (<%ss)",
                    _MIN_REFRESH_INTERVAL,
                )
                return existing

        with _cross_process_lock() as acquired:
            if not acquired:
                logger.info(
                    "infra_profile: another process is refreshing — skipping (%s)",
                    reason,
                )
                return store.load_profile()
            return await _refresh_locked(
                reason,
                db=db,
                router=router,
                event_bus=event_bus,
                guardian_remote=guardian_remote,
            )


async def _refresh_locked(
    reason: str,
    *,
    db,
    router,
    event_bus,
    guardian_remote,
) -> dict[str, Any]:
    """The refresh body; caller holds both the module lock and the flock."""
    now = datetime.now(UTC).isoformat()
    previous = store.load_profile()
    prev_sections = previous.get("sections", {})

    # ── collect (both planes concurrently) ──────────────────────────
    results = await asyncio.gather(
        *(collector() for collector in CONTAINER_COLLECTORS),
        collect_host(guardian_remote),
        return_exceptions=True,
    )
    host_result = results[-1]
    section_results: list[SectionResult] = []
    for collector, result in zip(CONTAINER_COLLECTORS, results[:-1], strict=True):
        if isinstance(result, SectionResult):
            section_results.append(result)
        else:
            name = collector.__name__.removeprefix("collect_")
            logger.error(
                "infra_profile: collector %s raised",
                name,
                exc_info=result,
            )
            section_results.append(SectionResult.failed(name, repr(result)))

    if isinstance(host_result, tuple):
        host_available, host_reason, host_sections = host_result
    else:
        logger.error("infra_profile: host collector raised", exc_info=host_result)
        host_available, host_reason = False, repr(host_result)
        host_sections = []
    section_results.extend(host_sections)

    profile: dict[str, Any] = {
        "schema_version": store.SCHEMA_VERSION,
        "collected_at": now,
        "refresh_reason": reason,
        "planes": {
            "container": {"available": True},
            "host": {"available": host_available, "reason": host_reason},
        },
        "sections": {
            r.name: _merge_section(r, prev_sections.get(r.name), now) for r in section_results
        },
    }

    # ── drift (before persist so a crash can't swallow a diff) ─────
    drift = compute_drift(previous, profile)
    if drift:
        logger.info(
            "infra_profile: drift in %s",
            [d["section"] for d in drift],
        )
        await emit_drift_observations(db, drift, event_bus)

    # ── persist facts (collected_at doubles as the short-circuit clock) ──
    store.save_profile(profile)

    # ── annotate (LLM; failure keeps old annotations) ────────────────
    annotations = store.load_annotations()
    try:
        summary = ", ".join(f"{k}={v}" for k, v in headline_facts(profile).items())
        annotations = await regenerate_annotations(
            profile=profile,
            annotations=annotations,
            router=router,
            summary=summary,
        )
        store.save_annotations(annotations)
    except Exception:
        logger.warning("infra_profile: annotation stage failed", exc_info=True)

    # ── render + project ─────────────────────────────────────────────
    try:
        doc = render_document(profile, annotations)
        atomic_write_text(DOC_PATH, doc)
        atomic_write_text(SHARED_DOC_PATH, doc)
    except Exception:
        logger.warning("infra_profile: render stage failed", exc_info=True)

    try:
        claude_md.update_block(profile)
    except Exception:
        logger.warning("infra_profile: CLAUDE.md block update failed", exc_info=True)

    return profile


async def refresh_from_runtime(reason: str, *, force: bool = False) -> dict[str, Any]:
    """``refresh()`` with dependencies pulled from the live runtime, if any."""
    db = router = event_bus = guardian_remote = None
    try:
        from genesis.runtime import GenesisRuntime

        rt = GenesisRuntime.instance()
        db = rt.db
        router = rt.router
        event_bus = rt.event_bus
        guardian_remote = getattr(rt, "_guardian_remote", None)
    except Exception:
        logger.debug("infra_profile: no live runtime — collecting facts only")
    return await refresh(
        reason,
        db=db,
        router=router,
        event_bus=event_bus,
        guardian_remote=guardian_remote,
        force=force,
    )
