"""zero_drop_status / zero_drop_ack — the read and disposition surfaces.

The detector answers "what work has fallen through the cracks?" by
enumeration. These are the two things that makes it worth having: a way to
READ the enumeration, and a way to DISPOSITION a row that is meant to sit
there.

The disposition half is not optional. The design deliberately has no
branch-name denylist — a backup or scratch branch is acknowledged with a
written reason instead, so the judgement is a record somebody can read rather
than a rule nobody can see. Without an ack surface that decision has no
expression, and a handful of permanent, undispositionable findings escalate
and stay in every alert forever, which is precisely how a signal dies.

The read half carries three things no count can be trusted without: the
detector's own last-run age (a stale zero is not a measured zero), which
classes the last run actually SWEPT (a frozen class reports leftovers), and
whether any leg is currently blind.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)

# Beyond this, a "0 open findings" answer is not evidence of a clean board —
# it is evidence of an old board. Derived from the sweep cadence, not guessed:
# session boundaries sweep hourly and disk-hygiene provides a DAILY floor, so
# anything past a day plus a margin means even the floor did not run.
STALE_AFTER_S = 26 * 3600


def _freshness(last_run: dict, *, now: datetime) -> dict:
    computed_at = last_run.get("computed_at")
    if not computed_at:
        return {
            "computed_at": None,
            "age_seconds": None,
            "stale": True,
            "verdict": "NEVER RUN — a zero here is unverified, not clean",
        }
    try:
        # read_last_run() parses UNVALIDATED json, so computed_at may be any
        # type (TypeError) or a naive timestamp (also TypeError, on the
        # subtraction against an aware `now`). Both mean the same thing here —
        # we cannot date the board — and both must report that rather than
        # raise out of a read-only status tool.
        parsed = datetime.fromisoformat(computed_at)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        age = (now - parsed).total_seconds()
    except (ValueError, TypeError):
        return {
            "computed_at": computed_at,
            "age_seconds": None,
            "stale": True,
            "verdict": "last-run timestamp unreadable — treat counts as unverified",
        }
    stale = age > STALE_AFTER_S
    return {
        "computed_at": computed_at,
        "age_seconds": int(age),
        "stale": stale,
        "verdict": (
            f"UNVERIFIED — last swept {int(age // 3600)}h ago; counts are leftovers"
            if stale
            else "fresh"
        ),
    }


async def _impl_zero_drop_status(db, *, now: datetime, limit: int | None = None) -> dict:
    from genesis.db.crud import zero_drop as zd
    from genesis.session_awareness.zero_drop_worker import read_last_run

    counts = await zd.counts_by_status(db)
    findings = await zd.list_findings(db, statuses=("open", "acked"), limit=limit)
    last_run = read_last_run()
    degraded = last_run.get("degraded") or {}

    return {
        "status": "ok",
        # Every count with its denominator and its provenance: the counts are
        # of the STORE, and the coverage line says which classes the last sweep
        # actually looked at.
        "counts_by_status": counts,
        "open": counts.get("open", 0),
        "acked": counts.get("acked", 0),
        "listed": len(findings),
        "listed_of": counts.get("open", 0) + counts.get("acked", 0),
        "detector": {
            **_freshness(last_run, now=now),
            "coverage": last_run.get("coverage"),
            "frozen_classes": last_run.get("frozen_classes") or [],
            "degraded": degraded,
            "blind": bool(degraded),
            "mode": last_run.get("mode"),
            "duration_s": last_run.get("duration_s"),
        },
        "stages": last_run.get("stages") or {},
        "findings": [
            {
                "class": r["class"],
                "branch": r["branch"],
                "status": r["status"],
                "tip_sha": r["tip_sha"],
                "ahead_count": r["ahead_count"],
                "worktree_path": r["worktree_path"],
                "consecutive_runs": r["consecutive_runs"],
                "escalated": bool(r["escalated_at"]),
                "first_seen_at": r["first_seen_at"],
                "last_seen_at": r["last_seen_at"],
                "ack_reason": r["ack_reason"],
                "details": r["details"],
            }
            for r in findings
        ],
    }


async def _impl_zero_drop_ack(db, *, class_: str, branch: str, reason: str, now: str) -> dict:
    from genesis.db.crud import zero_drop as zd

    if not (reason or "").strip():
        # An unexplained suppression is indistinguishable from a forgotten one.
        return {"status": "error", "message": "reason is required — an ack is a record"}
    if class_ not in zd.CLASSES:
        return {
            "status": "error",
            "message": f"unknown class {class_!r}; valid: {', '.join(zd.CLASSES)}",
        }
    row = await zd.ack(db, class_=class_, branch=branch, reason=reason.strip(), now=now)
    if row is None:
        return {
            "status": "not_found",
            "message": (
                f"no open/acked {class_} finding for {branch!r} — it may already be "
                "resolved, or the branch name may not match the finding's identity"
            ),
        }
    return {
        "status": "ok",
        "class": row["class"],
        "branch": row["branch"],
        "acked_tip_sha": row["acked_tip_sha"],
        "reason": row["ack_reason"],
        "note": (
            "This ack is keyed to the tip it was granted at — it expires by itself "
            "the moment new work lands on this branch."
        ),
    }


def _db_or_none():
    import genesis.mcp.health_mcp as health_mcp_mod

    _service = health_mcp_mod._service
    return _service._db if _service is not None else None


@mcp.tool()
async def zero_drop_status(limit: int | None = None) -> dict:
    """Stranded work: what exists but is in no pipeline, enumerated.

    Returns open + acknowledged findings (local branches with no PR, pushed
    branches with no PR, worktrees holding uncommitted work), each with how
    many consecutive sweeps it has survived and whether it has escalated.

    Read the ``detector`` block before trusting a zero. It carries the age of
    the last sweep (a stale board's zero is unverified, not clean), which
    classes that sweep actually covered, and whether any leg is currently
    blind — a detector with a failing leg keeps reporting its last numbers.

    ``limit`` pages the listing; ``listed_of`` is always the full total.
    """
    db = _db_or_none()
    if db is None:
        return {"status": "unavailable", "message": "DB not initialized"}
    return await _impl_zero_drop_status(db, now=datetime.now(UTC), limit=limit)


@mcp.tool()
async def zero_drop_ack(class_: str, branch: str, reason: str) -> dict:
    """Acknowledge a stranded-work finding — suppression with a reason.

    For work that is MEANT to sit there: a backup branch, a scratch experiment,
    a worktree deliberately parked. There is no denylist by design, so this is
    how such a decision gets recorded instead of hidden.

    The ack is keyed to the branch tip it was granted against and expires by
    itself the moment new work lands there — it says "I looked at this as it
    stood", never "mute this forever". ``reason`` is required.

    ``class_`` is one of ``unpushed_branch``, ``pushed_no_pr``,
    ``dirty_worktree``; ``branch`` is the finding's branch as
    ``zero_drop_status`` reports it.
    """
    db = _db_or_none()
    if db is None:
        return {"status": "unavailable", "message": "DB not initialized"}
    return await _impl_zero_drop_ack(
        db, class_=class_, branch=branch, reason=reason, now=datetime.now(UTC).isoformat()
    )
