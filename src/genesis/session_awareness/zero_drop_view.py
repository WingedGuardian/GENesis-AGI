"""The accounting view — one answer to "what fell through the cracks?".

The detector (``zero_drop_worker``) answers that question for GIT STATE. This
widens it past git without building a second store: every part below is
DERIVED, on demand, from a store that already owns the data. Nothing here
persists anything.

Why a view and not another table: the failure this whole subsystem exists to
prevent is a confident zero. Five partial surfaces each reporting their own
slice is how a gap survives — each one is correct and none of them is the
answer. So the parts are assembled together, and every count carries the
denominator that makes it checkable.

THREE RULES, each of which a surface here would otherwise get wrong:

1. **Every count carries its denominator.** ``open`` alone is not a
   measurement; ``open of tracked`` is. ``zero_drop.counts_by_status`` says the
   same thing in its own docstring — "the denominator every surface must
   render".

2. **A part that cannot be read says so, and does not take the view with it.**
   Each part is assembled inside its own guard, so a failing store degrades ONE
   part to ``{"status": "unavailable", ...}`` rather than blanking the board.
   The morning report's ground-truth section uses the same per-line shape for
   the same reason.

3. **A stale source is not a measured zero.** The detector's own reader
   contract (``zero_drop_worker.read_last_run``) is explicit that an empty
   result means "has not run", never "nothing is stranded". The PR cache is
   held to the same standard here: past its TTL it reports ``stale`` and NO
   count, because a dead worker's snapshot presented as a number is exactly the
   false-clean this subsystem exists to prevent.

COUNTS ONLY, deliberately. Item-level staleness is Beads' lane (owner ruling,
2026-09-07: the split is by AXIS, not layer), and the carve-out is also what
keeps untrusted repository text out of a surface that reaches Telegram — see
``build_view``'s note on the gaps part.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# A part that could not be read. Callers render this rather than a zero — the
# distinction between "measured none" and "could not measure" is the entire
# point of the view.
STATUS_OK = "ok"
STATUS_UNAVAILABLE = "unavailable"
STATUS_STALE = "stale"
# A container part whose own read succeeded but which holds a failed child. A
# consumer that trusts the part-level status must not read `ok` over three
# `unavailable`s.
STATUS_DEGRADED = "degraded"

# Which follow-up statuses count as work still outstanding. Derived from the
# store's OWN vocabulary (`db/crud/follow_ups.py` `_VALID_STATUS`) minus the one
# terminal state, NOT from the single status that happens to be most common.
#
# `pending` alone was the first version of this and it was wrong: `failed` and
# `blocked` are the textbook stranded item this board exists to name, and they
# would have rendered as zero. MEASURED on a live install at the time of
# writing: 187 pending, 5 in_progress, 0 failed, 0 blocked — so the collapsed
# definition under-counted by 5 of 192 the day it was written, and by an
# unbounded amount the first time anything fails.
FOLLOW_UP_OPEN_STATUSES = ("pending", "scheduled", "in_progress", "failed", "blocked")


def _unavailable(reason: str) -> dict:
    """A part that failed to assemble, said out loud."""
    return {"status": STATUS_UNAVAILABLE, "reason": reason}


def _with_child_status(part: dict) -> dict:
    """Downgrade a CONTAINER part to `degraded` when any child failed.

    Without this a container reports `ok` while holding three `unavailable`
    children, and a consumer that trusts the part-level status — the natural
    reading, and what the single-value parts do — renders a failure as a
    success. The container's own read succeeding says nothing about what is
    inside it.
    """
    failed = sorted(
        k
        for k, v in part.items()
        if k != "status" and isinstance(v, dict) and v.get("status") == STATUS_UNAVAILABLE
    )
    if failed:
        part["status"] = STATUS_DEGRADED
        part["degraded_children"] = failed
    return part


async def _gaps(db, *, now: datetime, findings_limit: int | None) -> dict:
    """Stranded git state, delegated whole to the detector's own read surface.

    Deliberately NOT re-derived from the CRUD. ``_impl_zero_drop_status``
    already assembles counts, the ``listed``/``listed_of`` denominator, the
    freshness verdict, coverage, frozen classes and the blind flag — and it
    applies the neutralisation rules that decide which untrusted repository
    text may be rendered (``branch`` verbatim because it is the ack key,
    ``worktree_path`` and ``degraded`` neutralised because they are display).
    A second assembly would drift from those rules, and the rule it drifted on
    would be a security one.
    """
    from genesis.mcp.health.zero_drop_tools import _impl_zero_drop_status

    board = await _impl_zero_drop_status(db, now=now, limit=findings_limit)
    return {"status": STATUS_OK, **board}


def _pr_pipeline(*, now: datetime) -> dict:
    """Open PRs, from the repo-pulse cache, with its age ALWAYS rendered.

    The cache is written by the repo-pulse worker at session boundaries under a
    debounce, so an idle box never refreshes it. Reading it without a TTL would
    report a dead worker's snapshot as a live count.

    The TTL derivation is lifted from ``scripts/surface_open_prs.py``, which is
    the only existing reader: two debounce intervals, floored at a day, so a
    large ``min_interval_minutes`` cannot expire the cache BEFORE the worker is
    even permitted to refresh it.
    """
    from genesis.session_awareness import pr_watch, repo_pulse
    from genesis.session_awareness import repo_pulse_config as pulse_cfg

    path = repo_pulse.open_prs_cache_path()
    if not path.exists():
        return _unavailable("no cache yet — the pulse worker has not run here")

    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        return _unavailable(f"cache unreadable: {type(exc).__name__}")
    if not isinstance(data, dict):
        return _unavailable("cache is not an object")

    computed = pr_watch._parse_ts(data.get("computed_at"))
    if computed is None:
        return _unavailable("cache carries no readable computed_at")
    age_s = int((now - computed).total_seconds())

    cfg = pulse_cfg.load_config()
    ttl_s = max(86400, pulse_cfg.knob_int(cfg, "min_interval_minutes") * 60 * 2)
    if age_s > ttl_s:
        # A count is withheld ON PURPOSE. Rendering the stale number beside the
        # age would still be read as a count by anyone skimming.
        return {
            "status": STATUS_STALE,
            "computed_at": data.get("computed_at"),
            "age_seconds": age_s,
            "verdict": f"cache is {age_s // 3600}h old (TTL {ttl_s // 3600}h) — the pulse worker is not running",
        }

    prs = data.get("prs")
    if not isinstance(prs, list):
        return _unavailable("cache has no readable pr list")

    limit_hit = bool(data.get("limit_hit"))
    return {
        "status": STATUS_OK,
        "computed_at": data.get("computed_at"),
        "age_seconds": age_s,
        "open_prs": len(prs),
        # A capped fetch makes the count a FLOOR, not a measurement. Say which
        # one this is rather than leaving a reader to assume.
        "count_is_floor": limit_hit,
        "verdict": (
            f"at least {len(prs)} open (the listing was capped)"
            if limit_hit
            else f"{len(prs)} open"
        ),
    }


async def _items_by_store(db) -> dict:
    """Full COUNTs per store, each with its denominator.

    Counts only — never row CONTENT. Ledger rows on a live install have carried
    plaintext credentials a session pasted in, which is why
    ``ledger_escalation`` is kept out of the morning report entirely. A count
    cannot leak; a rendered row can.
    """
    from genesis.db.crud import follow_ups as fu_crud
    from genesis.db.crud import session_charters as sc_crud
    from genesis.db.crud import zero_drop as zd_crud

    out: dict[str, Any] = {"status": STATUS_OK}

    try:
        counts = await zd_crud.counts_by_status(db)
        tracked = counts.get("open", 0) + counts.get("acked", 0)
        out["stranded_work"] = {
            "open": counts.get("open", 0),
            "acked": counts.get("acked", 0),
            "tracked": tracked,
            "by_status": counts,
        }
    except Exception as exc:
        logger.warning("zero-drop view: stranded-work counts failed", exc_info=True)
        out["stranded_work"] = _unavailable(type(exc).__name__)

    try:
        ledger = await sc_crud.ledger_counts_all(db)
        unresolved = ledger.get("open", 0) + ledger.get("in_progress", 0)
        out["ledger"] = {
            "unresolved": unresolved,
            "total": sum(ledger.values()),
            "by_status": ledger,
        }
    except Exception as exc:
        logger.warning("zero-drop view: ledger counts failed", exc_info=True)
        out["ledger"] = _unavailable(type(exc).__name__)

    try:
        fu = await fu_crud.get_summary_counts(db, include_tabled=False)
        out["follow_ups"] = {
            "unresolved": sum(fu.get(s, 0) for s in FOLLOW_UP_OPEN_STATUSES),
            "total": sum(fu.values()),
            "by_status": fu,
        }
    except Exception as exc:
        logger.warning("zero-drop view: follow-up counts failed", exc_info=True)
        out["follow_ups"] = _unavailable(type(exc).__name__)

    return _with_child_status(out)


async def _owner_pending(db) -> dict:
    """What is waiting on the OWNER specifically, not on the system.

    Separated from ``items_by_store`` because the question it answers is
    different: those counts say how much work exists, this one says how much of
    it cannot move until a person acts.

    **The one DECLARED exemption from the denominator rule.** Every count in
    ``items_by_store`` is a sample of a population and is meaningless without
    it. These two are not: the population of "things waiting on you" IS the
    count, and pairing it with an all-time total ("5 of 213 approval requests
    ever raised") would size it against a number nobody acts on. The rule
    exists so a figure can be checked, not as a format — so the exemption is
    stated here rather than left as an inconsistency, and
    ``test_every_part_obeys_the_denominator_rule_or_declares_an_exemption``
    enforces that this is the only one.
    """
    from genesis.db.crud import approval_requests as approval_crud
    from genesis.db.crud import ego as ego_crud
    from genesis.ego.types import partition_informational

    out: dict[str, Any] = {"status": STATUS_OK, "denominator_exempt": True}

    try:
        out["approval_requests"] = len(await approval_crud.list_pending(db))
    except Exception as exc:
        logger.warning("zero-drop view: approval counts failed", exc_info=True)
        out["approval_requests"] = _unavailable(type(exc).__name__)

    try:
        # Partitioned the same way the morning report partitions it. Informational
        # eval rows (j9/gauntlet) are not approval work, and counting them here
        # would make this board and the morning report answer the same question
        # with two different numbers on the same data — the precise drift the
        # single-assembler rule exists to prevent.
        approval_work, _informational = partition_informational(
            await ego_crud.list_pending_proposals(db)
        )
        out["ego_proposals"] = len(approval_work)
    except Exception as exc:
        logger.warning("zero-drop view: ego proposal counts failed", exc_info=True)
        out["ego_proposals"] = _unavailable(type(exc).__name__)

    return _with_child_status(out)


def _roadmap() -> dict:
    """What this view does NOT yet cover, stated rather than implied.

    A board that lists five parts reads as complete. Naming the absent legs is
    what stops the next reader treating this as the whole answer — the same
    reason every count above carries a denominator.
    """
    return {
        "status": STATUS_OK,
        "covered": [
            "stranded git state (branches, worktrees) — from the detector",
            "open PR pipeline — from the repo-pulse cache",
            "store counts: stranded work, ledger rows, follow-ups",
            "owner-pending: approval requests, ego proposals",
        ],
        "not_covered": [
            "item-level staleness — Beads' lane by owner ruling; this view "
            "counts stores rather than judging individual items",
            "tasks and dispatched sessions — already aggregated by unified_work",
            "cross-store reconciliation (is this ledger row the same work as "
            "that PR?) — repo-pulse owns the matching, and it is not counted here",
        ],
    }


async def build_view(db, *, now: datetime | None = None, findings_limit: int | None = 20) -> dict:
    """The five-part accounting view. Derived on demand; nothing is stored.

    Both accounting surfaces — the dashboard tab and the morning report's
    ground-truth line — call THIS, never a second assembly, so they cannot
    disagree about what the board says.

    The MCP ``zero_drop_status`` tool is deliberately NOT a caller: it is the
    DETECTOR's own read surface, a narrower thing, and this view delegates its
    gaps part to that same implementation rather than the reverse. So the
    relationship is one-way (view -> detector board), not a third assembly.

    ``findings_limit`` pages the gaps listing only. The counts beside it are
    full COUNTs, so a paged listing still renders "n of N" rather than
    presenting the page size as the total.
    """
    now = now or datetime.now(UTC)

    async def _guard(name, coro):
        try:
            return await coro
        except Exception as exc:
            logger.warning("zero-drop view: %s part failed", name, exc_info=True)
            return _unavailable(f"{type(exc).__name__}: {exc}"[:200])

    def _guard_sync(name, fn):
        try:
            return fn()
        except Exception as exc:
            logger.warning("zero-drop view: %s part failed", name, exc_info=True)
            return _unavailable(f"{type(exc).__name__}: {exc}"[:200])

    return {
        "computed_at": now.isoformat(),
        "gaps": await _guard("gaps", _gaps(db, now=now, findings_limit=findings_limit)),
        "pr_pipeline": _guard_sync("pr_pipeline", lambda: _pr_pipeline(now=now)),
        "items_by_store": await _guard("items_by_store", _items_by_store(db)),
        "owner_pending": await _guard("owner_pending", _owner_pending(db)),
        "roadmap": _roadmap(),
    }
