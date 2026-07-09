"""Loop-closure funnel aggregations — READ-ONLY.

Per-artifact "is the self-learning loop closed?" funnel —
captured → surfaced/invoked → actuated → measured → leak — computed entirely
from existing tables. No writes, no schema changes. Powers the
``loop_closure_status`` MCP tool (the self-learning health surface, which
subsumes ``self_improvement_status``).

The point is honest accounting: a thing that is *captured* but never *acted on*
or *measured* is a leak — work/learning that fell through the cracks. These
queries count exactly that, per artifact, so the operator (and Genesis) can see
where the loop is open.

"Stale" thresholds are passed in by the caller (ISO cutoff) so these functions
stay deterministic and unit-testable — no wall-clock inside the query layer.
All ``created_at`` writers use ``datetime.now(UTC).isoformat()`` (fixed-width,
same zone), so the lexicographic ``created_at < ?`` compare == chronological.
"""

from __future__ import annotations

import json

import aiosqlite

# Canonical staleness cutoff: a discovered item still un-actuated after this
# long is counted as a leak ("work goes here to die"). Shared by the
# loop_closure_status MCP tool and the J-9 noise dimension — callers derive
# their ISO ``stale_before`` cutoff from it (the query layer itself stays
# deterministic; no wall-clock in here). 14d mirrors the morning report's
# follow-up staleness rule.
STALE_DAYS = 14

# Session statuses where a skill's outcome is DETERMINABLE at all (so a
# success-rate is computable) — the full terminal set, not just failures.
# 'active'/'checkpointed'/'expired' are not terminal-for-outcome. NB:
# ``learning/skills/effectiveness.py`` uses ``status == 'failed'`` as its
# *failure* signal; here we use both terminals to mean "outcome is knowable".
_TERMINAL_SESSION_STATUS = ("completed", "failed")


async def _scalar(db: aiosqlite.Connection, sql: str, params: tuple = ()) -> int:
    cur = await db.execute(sql, params)
    row = await cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


async def _group_counts(db: aiosqlite.Connection, sql: str) -> dict[str, int]:
    cur = await db.execute(sql)
    return {(r[0] or "∅"): r[1] for r in await cur.fetchall()}


def _loop_label(total: int, *, flowing: int, leaked: int) -> str:
    """Data-derived loop status (never hardcoded):

    EMPTY   — nothing captured yet
    OPEN    — captured but nothing is flowing through (acted on / measured)
    PARTIAL — some flows, some leaks
    CLOSED  — everything captured has flowed through, no leak
    """
    if total == 0:
        return "EMPTY"
    if flowing == 0:
        return "OPEN"
    return "PARTIAL" if leaked > 0 else "CLOSED"


async def procedure_funnel(db: aiosqlite.Connection) -> dict:
    """Procedures are outcome-graded. ``procedural_memory`` now carries TWO
    distinct usage counters:

    - ``surfaced_count`` — a contextual hook put the procedure into a model's
      context (the proactive memory hook on a prompt, or the PreToolUse advisor
      on a tool call). Passive exposure. NOT read by the promoter.
    - ``invocation_count`` — the procedure was explicitly recalled/fired via the
      ``procedure_recall`` MCP tool. Active actuation; the promoter's read-signal.

    So the honest funnel is captured → **surfaced → invoked** → measured. A
    procedure that has been neither surfaced nor invoked has never reached a
    model at all — that is the real leak (golden-dormant that never gets to
    mature). ``reached`` (surfaced OR invoked) is the loop's "flowing" signal."""
    total = await _scalar(db, "SELECT COUNT(*) FROM procedural_memory")
    surfaced = await _scalar(
        db, "SELECT COUNT(*) FROM procedural_memory WHERE surfaced_count > 0"
    )
    invoked = await _scalar(
        db, "SELECT COUNT(*) FROM procedural_memory WHERE invocation_count > 0"
    )
    reached = await _scalar(
        db,
        "SELECT COUNT(*) FROM procedural_memory "
        "WHERE surfaced_count > 0 OR invocation_count > 0",
    )
    measured = await _scalar(
        db,
        "SELECT COUNT(*) FROM procedural_memory WHERE success_count + failure_count > 0",
    )
    deprecated = await _scalar(
        db, "SELECT COUNT(*) FROM procedural_memory WHERE deprecated = 1"
    )
    by_tier = await _group_counts(
        db, "SELECT activation_tier, COUNT(*) FROM procedural_memory GROUP BY activation_tier"
    )
    leak_never_reached = total - reached
    return {
        "artifact": "procedure",
        "captured": total,
        # NOTE: surfaced and invoked OVERLAP (a procedure can be both). Do not
        # sum them — use ``reached`` (the de-duped union) for total reach.
        "surfaced": surfaced,   # surfaced_count > 0 (contextual hooks)
        "invoked": invoked,     # invocation_count > 0 (explicit procedure_recall)
        "reached": reached,     # surfaced OR invoked, de-duped — the flow signal
        "measured": measured,
        "by_tier": by_tier,
        "deprecated": deprecated,
        "leak_never_reached": leak_never_reached,
        "loop": _loop_label(total, flowing=reached, leaked=leak_never_reached),
    }


async def skill_funnel(db: aiosqlite.Connection) -> dict:
    """Skills are file-based (``SKILL.md``) and — by deliberate design — **NOT
    outcome-graded**: there is no skill equivalent of the procedure promoter
    (skills have no per-invocation execution outcome). This funnel therefore
    reports the HONEST state of the skill feedback signal rather than a fake
    "closed" one. It is the LC2-honest observability slice; it writes nothing.

    - ``captured``      — every skill in the library (``list_available_skills``).
    - ``instrumented``  — library skills appearing in ≥1 ``cc_sessions.skill_tags``.
      That tag is set today only for background sessions (task + reflection);
      foreground sessions (the bulk of usage) tag nothing, so most skills are
      uninstrumented.
    - ``measured``      — instrumented skills with ≥1 **terminal-status**
      (completed/failed) session, so a success-rate is computable. A skill used
      only in *successful* sessions IS measured (rate = 1.0) — ``measured`` means
      "outcome is knowable", NOT "has a failure".
    - ``leak_uninstrumented`` — ``captured − instrumented``: skills with no usage
      signal at all. This is the real, large leak — the missing afferent nerve
      (foreground usage capture + a graded outcome source). Closing it is the
      separately-documented **LC2-maturity** build, not this read-only slice.

    Tags are matched by exact membership against the library, so a non-skill tag
    (e.g. a reflection pseudo-label like ``strategic-reflection``, or a ``profile``
    value like ``research`` that happens to collide with a skill name) does not
    inflate ``instrumented`` unless it is genuinely a library skill in the list.
    """
    from genesis.learning.skills import wiring

    # list_available_skills() is synchronous filesystem I/O (iterdir over 2 dirs).
    # Acceptable on the loop here: the library is small (~30 entries), paths are
    # local, and this tool is operator-invoked (no hot path). Would need
    # run_in_executor only if the skills dir moved to slow/network storage.
    library = set(wiring.list_available_skills())
    captured = len(library)

    # Unlike the sibling funnels (pure SQL-side COUNT via ``_scalar``), skill
    # usage lives inside the ``metadata`` JSON blob, so we fetch the matching
    # rows and intersect in Python. The ``LIKE`` bounds this to rows that carry
    # skill_tags (background sessions only today) — a small set; no metadata
    # index exists, but the scan stays cheap at this scale.
    cur = await db.execute(
        "SELECT metadata, status FROM cc_sessions WHERE metadata LIKE '%skill_tags%'"
    )
    instrumented: set[str] = set()
    measured: set[str] = set()
    for raw, status in await cur.fetchall():
        if not raw:
            continue
        try:
            tags = json.loads(raw).get("skill_tags", [])
        except (json.JSONDecodeError, TypeError, AttributeError):
            continue
        if not isinstance(tags, list):
            continue
        hit = library.intersection(tags)
        if not hit:
            continue
        instrumented |= hit
        if status in _TERMINAL_SESSION_STATUS:
            measured |= hit

    leak_uninstrumented = captured - len(instrumented)
    return {
        "artifact": "skill",
        "captured": captured,
        "instrumented": len(instrumented),
        "measured": len(measured),
        "graded": False,  # skills are NOT outcome-graded (file-based, by design)
        "leak_uninstrumented": leak_uninstrumented,
        "loop": _loop_label(
            captured, flowing=len(instrumented), leaked=leak_uninstrumented
        ),
        "note": (
            "skills are file-based and NOT outcome-graded; instrumented = appears "
            "in a session's skill_tags (only background sessions tag today), "
            "measured = outcome computable (terminal-status session). "
            "leak_uninstrumented = no usage signal at all (foreground usage "
            "uncaptured + no graded outcome source) — closing it is LC2-maturity."
        ),
    }


async def observation_funnel(db: aiosqlite.Connection, *, stale_before: str) -> dict:
    """Observations: actuation signal = ``influenced_action``. Leak = unresolved,
    un-actuated, and aged out."""
    total = await _scalar(db, "SELECT COUNT(*) FROM observations")
    surfaced = await _scalar(
        db, "SELECT COUNT(*) FROM observations WHERE surfaced_count > 0"
    )
    actuated = await _scalar(
        db, "SELECT COUNT(*) FROM observations WHERE influenced_action = 1"
    )
    resolved = await _scalar(db, "SELECT COUNT(*) FROM observations WHERE resolved = 1")
    leak_stale = await _scalar(
        db,
        "SELECT COUNT(*) FROM observations "
        "WHERE resolved = 0 AND influenced_action = 0 AND created_at < ?",
        (stale_before,),
    )
    return {
        "artifact": "observation",
        "captured": total,
        "surfaced": surfaced,
        "actuated": actuated,
        "resolved": resolved,
        "leak_stale_unactuated": leak_stale,
        "loop": _loop_label(total, flowing=actuated, leaked=leak_stale),
    }


# Reflection-OUTPUT observation types — the actuation-bearing reflection
# artifacts. EXPLICIT allow-list, deliberately NOT a ``LIKE '%reflection%'``:
# that would wrongly pull in ``quarantined_reflection`` (gatekept failures, not
# actuation). ``learning`` is excluded too — it is shared with the learning
# pipeline (``learning/pipeline.py``), so it isn't reflection-exclusive.
_REFLECTION_OBS_TYPES = (
    "light_reflection",
    "micro_reflection",
    "reflection_output",
    "reflection_summary",
    "reflection_observation",
)


async def reflection_funnel(db: aiosqlite.Connection) -> dict:
    """Reflections actuate as OBSERVATIONS, not via ``reflection_corpus``.

    ``reflection_corpus`` is the raw reflection-LLM transcript log; its
    ``used_in_optimization`` column was reserved for a prompt-optimization
    pipeline that was never built (zero writers in the codebase) — so a 0 there
    is NOT a leak, it's an unbuilt arc. The real actuation flows through the
    reflection-output observations (``_REFLECTION_OBS_TYPES``), measured by
    ``influenced_action``.

    This is a focused SUBSET view of rows already counted in
    ``observation_funnel`` (they live in both), so it intentionally emits **no**
    ``leak_`` key — the staleness/leak of those rows is owned by
    ``observation_funnel`` and must not be double-counted in the umbrella's
    open-seams. ``leaked`` here is internal to the loop label only.
    """
    placeholders = ",".join("?" for _ in _REFLECTION_OBS_TYPES)
    captured = await _scalar(
        db,
        f"SELECT COUNT(*) FROM observations WHERE type IN ({placeholders})",
        _REFLECTION_OBS_TYPES,
    )
    actuated = await _scalar(
        db,
        f"SELECT COUNT(*) FROM observations "
        f"WHERE type IN ({placeholders}) AND influenced_action = 1",
        _REFLECTION_OBS_TYPES,
    )
    # Raw transcript capture log — context only, NOT the actuation signal.
    corpus_captured = await _scalar(db, "SELECT COUNT(*) FROM reflection_corpus")
    corpus_parsed = await _scalar(
        db, "SELECT COUNT(*) FROM reflection_corpus WHERE parsed_ok = 1"
    )
    leaked = captured - actuated  # loop-label math only; never exposed as leak_
    return {
        "artifact": "reflection",
        "captured": captured,
        "actuated": actuated,
        "corpus_captured": corpus_captured,
        "corpus_parsed": corpus_parsed,
        "optimization_pipeline": "not_built",
        "loop": _loop_label(captured, flowing=actuated, leaked=leaked),
        "note": (
            "actuation measured via reflection-output observations (subset of "
            "observation_funnel — not additive); reflection_corpus."
            "used_in_optimization is reserved for an unbuilt optimization "
            "pipeline, not a leak"
        ),
    }


async def followup_funnel(db: aiosqlite.Connection, *, stale_before: str) -> dict:
    """Follow-ups: actuated = past the queue (``scheduled``/``in_progress``/
    ``completed``). Leak = ``pending`` past the stale cutoff (the graveyard)."""
    by_status = await _group_counts(
        db, "SELECT status, COUNT(*) FROM follow_ups GROUP BY status"
    )
    total = sum(by_status.values())
    actuated = (
        by_status.get("scheduled", 0)
        + by_status.get("in_progress", 0)
        + by_status.get("completed", 0)
    )
    pending_stale = await _scalar(
        db,
        "SELECT COUNT(*) FROM follow_ups WHERE status = 'pending' AND created_at < ?",
        (stale_before,),
    )
    return {
        "artifact": "follow_up",
        "captured": total,
        "by_status": by_status,
        "actuated": actuated,
        "leak_pending_stale": pending_stale,
        "loop": _loop_label(total, flowing=actuated, leaked=pending_stale),
    }


async def proposal_funnel(db: aiosqlite.Connection, *, stale_before: str) -> dict:
    """Ego proposals: actuated = sanctioned for action (``approved``/``executed``).
    Leak = ``pending`` past the stale cutoff (approval never came)."""
    by_status = await _group_counts(
        db, "SELECT status, COUNT(*) FROM ego_proposals GROUP BY status"
    )
    total = sum(by_status.values())
    actuated = by_status.get("approved", 0) + by_status.get("executed", 0)
    pending_stale = await _scalar(
        db,
        "SELECT COUNT(*) FROM ego_proposals WHERE status = 'pending' AND created_at < ?",
        (stale_before,),
    )
    return {
        "artifact": "ego_proposal",
        "captured": total,
        "by_status": by_status,
        "actuated": actuated,
        "leak_pending_stale": pending_stale,
        "loop": _loop_label(total, flowing=actuated, leaked=pending_stale),
    }
