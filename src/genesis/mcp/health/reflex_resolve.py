"""reflex_signal_resolve MCP tool — manually resolve or dismiss a reflex signal.

The afferent nerve ingests signals but, until the diagnose/fix lanes ship, a
signal has no way to LEAVE the ``new`` lane — it sits on the dashboard forever.
This tool is the human exit: it moves a signal to a terminal status and, for a
dismissal, records the judgment as a taste-corpus verdict (the spec's "a signal
the user dismisses is a verdict, not just deleted").

Three dispositions, mapped to the schema's constrained vocabularies:

- ``fixed``      → status ``resolved``. The bug was real and is fixed — often
  out-of-band (a normal PR), which the verdict vocabulary has no value for, so
  NO verdict row is written (it isn't a card judgment, just a lifecycle close).
- ``not_a_bug``  → status ``dismissed_notbug``  + verdict ``dismiss_notbug``.
- ``wont_fix``   → status ``dismissed_wontfix`` + verdict ``dismiss_wontfix``.

Idempotent: a signal already in a terminal status is a no-op (no double verdict).
Guarded: the status transition keys on the observed current status, so a
concurrent change makes this a conflict rather than a clobber.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)

# disposition → (terminal status, verdict value | None). None = no verdict row
# (a lifecycle resolution, not a card judgment).
_DISPOSITIONS: dict[str, tuple[str, str | None]] = {
    "fixed": ("resolved", None),
    "not_a_bug": ("dismissed_notbug", "dismiss_notbug"),
    "wont_fix": ("dismissed_wontfix", "dismiss_wontfix"),
}

# Fields snapshotted into the verdict's context (the taste-corpus example input).
_SNAPSHOT_FIELDS = (
    "fingerprint",
    "class_key",
    "task_name",
    "subsystem",
    "error_type",
    "status",
    "occurrence_count",
    "last_error_message",
)


async def _record_verdict(
    db, signal: dict, disposition: str, verdict: str, resolved_by: str, rationale: str, now: str
) -> tuple[str | None, bool]:
    """Write one taste-corpus verdict for a signal. Returns ``(verdict_id, failed)``.

    Never raises: a verdict-store failure is logged and reported as ``failed=True``
    (the caller surfaces ``partial``), so a signal's terminal transition is never
    unwound and the tool never crashes on a corpus-write blip."""
    from genesis.db.crud import reflex_verdicts as verdicts_crud

    context = {k: signal.get(k) for k in _SNAPSHOT_FIELDS}
    context["disposition"] = disposition
    context["rationale"] = rationale
    try:
        verdict_id = await verdicts_crud.record(
            db,
            signal_id=signal["id"],
            verdict_point="diagnose_card",
            verdict=verdict,
            resolved_by=resolved_by,
            context_snapshot=context,
            now=now,
        )
        return verdict_id, False
    except Exception:
        logger.error(
            "reflex_signal_resolve: verdict write failed for %s (status transition stands)",
            signal["id"],
            exc_info=True,
        )
        return None, True


def _result(
    status: str,
    signal_id: str,
    new_status: str,
    verdict: str | None,
    verdict_id: str | None,
    rationale: str,
    failed: bool,
) -> dict:
    out = {
        "status": status,
        "signal_id": signal_id,
        "new_status": new_status,
        "verdict": verdict,
        "verdict_id": verdict_id,
        "rationale": rationale,
    }
    if failed:
        out["verdict_write_failed"] = True
        out["message"] = "signal resolved, but the taste-corpus verdict write failed (logged)"
    return out


async def _impl_reflex_signal_resolve(
    db,
    *,
    signal_id: str,
    disposition: str,
    rationale: str,
    now: str,
    resolved_by: str = "user",
) -> dict:
    from genesis.db.crud import reflex_signals as signals_crud
    from genesis.db.crud import reflex_verdicts as verdicts_crud

    if disposition not in _DISPOSITIONS:
        return {
            "status": "error",
            "message": f"unknown disposition {disposition!r} (fixed | not_a_bug | wont_fix)",
        }
    if not rationale or not rationale.strip():
        return {"status": "error", "message": "rationale is required"}

    signal = await signals_crud.get_by_id(db, signal_id)
    if signal is None:
        return {"status": "error", "message": f"signal {signal_id!r} not found"}

    current = signal["status"]
    to_status, verdict = _DISPOSITIONS[disposition]

    # Already CONSCIOUSLY closed (resolved/dismissed/merged) → no-op — EXCEPT the
    # partial-write recovery case: a prior call moved the status but its verdict
    # write failed (returned 'partial'), so the corpus row is missing. If this
    # retry matches that disposition and the expected verdict truly isn't recorded,
    # REPAIR it here — a transient DB blip must not cause permanent taste-corpus
    # loss with no way back through the API. TERMINAL_DISPOSED (not _REOPENABLE):
    # failure/expiry signals stay resolvable.
    if current in signals_crud.TERMINAL_DISPOSED:
        if verdict is not None and current == to_status:
            existing = await verdicts_crud.list_for_signal(db, signal_id)
            already_recorded = any(
                v.get("verdict_point") == "diagnose_card" and v.get("verdict") == verdict
                for v in existing
            )
            if not already_recorded:
                verdict_id, failed = await _record_verdict(
                    db, signal, disposition, verdict, resolved_by, rationale, now
                )
                return _result(
                    "partial" if failed else "repaired",
                    signal_id,
                    current,
                    verdict,
                    verdict_id,
                    rationale,
                    failed,
                )
        return {
            "status": "noop",
            "message": f"signal already disposed ({current})",
            "signal_id": signal_id,
            "signal_status": current,
        }

    # Transition FIRST (guarded on the observed status) so a lost race can't
    # leave an orphan verdict describing a dismissal that never took effect.
    ok = await signals_crud.set_status(
        db, signal_id=signal_id, expected_from=current, to=to_status, now=now
    )
    if not ok:
        return {
            "status": "conflict",
            "message": f"signal status changed under us (was {current}); not resolved",
            "signal_id": signal_id,
        }

    verdict_id: str | None = None
    failed = False
    if verdict is not None:
        verdict_id, failed = await _record_verdict(
            db, signal, disposition, verdict, resolved_by, rationale, now
        )
    return _result(
        "partial" if failed else "ok", signal_id, to_status, verdict, verdict_id, rationale, failed
    )


@mcp.tool()
async def reflex_signal_resolve(signal_id: str, disposition: str, rationale: str) -> dict:
    """Resolve or dismiss a reflex signal, recording a taste-corpus verdict.

    disposition:
      - ``fixed``     — real bug, now fixed (often by a normal PR) → status
        ``resolved`` (no verdict row; not a card judgment).
      - ``not_a_bug`` — not a genuine defect → dismissed, verdict ``dismiss_notbug``.
      - ``wont_fix``  — real but won't fix now → dismissed, verdict ``dismiss_wontfix``.

    ``rationale`` (required) is recorded and, for dismissals, stored in the
    taste-corpus context. Idempotent on already-terminal signals.
    """
    import genesis.mcp.health_mcp as health_mcp_mod

    _service = health_mcp_mod._service
    if _service is None or _service._db is None:
        return {"status": "unavailable", "message": "DB not initialized"}

    return await _impl_reflex_signal_resolve(
        _service._db,
        signal_id=signal_id,
        disposition=disposition,
        rationale=rationale,
        now=datetime.now(UTC).isoformat(),
    )
