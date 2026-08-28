"""CRUD operations for the capability map.

Stores the ego's self-model: per-domain confidence scores derived
from aggregating intervention journal, proposals, autonomy state,
procedural memory, and CC session outcomes.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

import aiosqlite

logger = logging.getLogger(__name__)


_TREND_THRESHOLD = 0.05  # 5% change threshold for trend detection


async def upsert(
    db: aiosqlite.Connection,
    *,
    domain: str,
    confidence: float,
    sample_size: int,
    trend: str = "stable",
    evidence_summary: str = "",
) -> str:
    """Insert or update a capability map entry for a domain.

    Automatically computes trend by comparing new confidence against
    the existing score (>5% change = improving/declining).
    """
    now = datetime.now(UTC).isoformat()
    cid = uuid.uuid4().hex[:16]

    # Read current confidence for trend detection
    cur = await db.execute(
        "SELECT confidence FROM capability_map WHERE domain = ?",
        (domain,),
    )
    row = await cur.fetchone()
    previous_confidence = row[0] if row else None

    # Compute trend from delta (only when we have previous data + enough samples)
    if previous_confidence is not None and sample_size >= 3:
        delta = confidence - previous_confidence
        if delta > _TREND_THRESHOLD:
            trend = "improving"
        elif delta < -_TREND_THRESHOLD:
            trend = "declining"
        else:
            trend = "stable"

    await db.execute(
        """INSERT INTO capability_map
           (id, domain, confidence, sample_size, trend, evidence_summary,
            updated_at, previous_confidence)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(domain) DO UPDATE SET
             previous_confidence = capability_map.confidence,
             confidence = excluded.confidence,
             sample_size = excluded.sample_size,
             trend = excluded.trend,
             evidence_summary = excluded.evidence_summary,
             updated_at = excluded.updated_at""",
        (cid, domain, confidence, sample_size, trend, evidence_summary,
         now, previous_confidence),
    )
    await db.commit()
    return cid


# What ``updated_at`` actually measures, and therefore what this window means.
#
# It is the time the AGGREGATOR last wrote the row — not the age of the evidence
# behind it. Those coincide for some sources and not others, because only three
# of the six read a time window at all:
#
#   windowed (30d)   : ego_proposals, cc_sessions, outcome_events
#   NOT windowed     : intervention_journal (full history, WHERE outcome_status
#                      != 'pending'), autonomy_state (lifetime counters),
#                      procedural_memory (COUNT of current non-deprecated rows)
#
# So a domain fed only by unwindowed sources is rewritten on every refresh
# forever and can never fall out of this window. That is CORRECT for two of
# them: autonomy counters and stored procedures are present-tense state, so
# "still true today" is exactly what they assert. Measured on live installs, the
# only rows in that category are high-volume ones that should never expire.
#
# The uniform, honest reading — true for all six sources — is therefore:
# ``updated_at`` is when the aggregator last VOUCHED for this row, and this
# window hides rows it stopped vouching for more than N days ago. Current-state
# sources keep vouching; event-windowed sources stop once their evidence leaves
# the window.
#
# KNOWN WART: intervention_journal is historical-event evidence with no window,
# so a journal-backed domain vouches forever on decisions of any age. That is an
# inconsistency in the SOURCE, not in this filter, and is tracked separately.
#
# Threshold. 14 days is ~28 missed refreshes: far past a domain that merely
# dipped under a noise gate for a week, well short of one whose evidence is
# months old. Measured across two live installs, observed lag clusters at 0-6
# days and then jumps to 23+ with nothing in between, so this sits in an empty
# gap rather than on a knife-edge.
#
# Anchor. ``MIN(MAX(updated_at), now)`` — the freshest row, clamped to now.
#
# Anchoring on the freshest row rather than wall-clock means that if the refresh
# job stops entirely, the whole table ages together and NOTHING is hidden:
# uniformly old, not selectively stale. A pure wall-clock window would blank the
# ego's entire self-model the moment the job broke.
#
# The clamp covers the opposite direction, which is the one that bites. MAX() is
# unbounded ABOVE, so a single row stamped ahead of real time — a clock skew, a
# bad backfill — would otherwise define the window for every other row and hide
# all of them, silently. That failure is also self-perpetuating: once the skewed
# domain stops being emitted nothing rewrites it, so the anchor never recovers.
# The same clock-skew shape has already bitten heartbeat GC in this repo.
#
# COALESCE covers the third direction: scalar ``min()`` returns NULL if ANY
# argument is NULL, and ``julianday()`` returns NULL for a value it cannot
# parse. Without it a single unparseable ``updated_at`` makes the whole
# predicate NULL and hides every row — silently, and self-perpetuatingly, which
# is precisely the failure the clamp exists to prevent. Degrade to wall-clock
# instead.
#
# Note the order: ``MAX(julianday(...))``, NOT ``julianday(MAX(...))``. ``MAX``
# over the raw column is a LEXICAL max over text, so a malformed value
# ('zzzz-...') outranks every ISO timestamp, parses to NULL, and COALESCE then
# substitutes wall-clock — hiding every uniformly-old row and collapsing the
# dead-refresh guarantee in exactly the corruption case COALESCE was added to
# tolerate. Aggregating over PARSED values skips unparseable rows instead of
# letting one of them decide the window.
#
# (A PARTIAL refresh outage is still not covered: one source failing while the
# others keep writing holds the anchor at now, and that source's domains do age
# out. Tracked separately.)
#
# Accepted residue: the anchor subquery is NOT itself sample-floored, so a row
# below the floor can in principle set the window for rows above it. Verified
# unreachable in steady state — the aggregator refuses to emit a below-floor
# domain, so such a row is never rewritten, freezes, and stops being the
# freshest as soon as any qualifying row is written (twice daily). In the
# degenerate case where NOTHING qualifies, the read returns empty regardless.
STALE_AFTER_DAYS = 14

# Minimum samples for a domain to count as evidence rather than noise.
#
# The single definition of this bar, shared by BOTH sides: the aggregator
# imports it to decide what to WRITE, and the prompt-facing reads
# (``get_prompt_rows``, ``get_weakest``) apply it to decide what to SHOW. Two
# copies would be free to drift, and a drift means reads either hide rows the
# aggregator still writes or surface rows it refuses to — so it lives here once,
# in the module both sides already depend on.
#
# The read side is not redundant with the write side: rows logged before the
# write floor existed are still in the table, and those reads are
# confidence-ordered into a top-N, so a single-sample row would otherwise
# outrank domains backed by dozens of samples. Raw reads (``get_all``,
# ``get_by_domain``) deliberately do NOT apply it.
MIN_SAMPLE_SIZE = 3


def _recency_clause(stale_after_days: int | None) -> tuple[str, list]:
    """SQL fragment + params restricting rows to the non-stale window.

    ``None`` disables the filter (returns every row); a negative value raises.

    Both sides go through ``julianday()`` so the comparison never depends on the
    stored string's shape. That matters: ``upsert`` writes
    ``datetime.now(UTC).isoformat()`` (``T``-separated, ``+00:00`` offset) while
    SQLite's own date functions render space-separated, and a lexical compare
    between those two forms is wrong rather than merely fragile —
    ``'…T12:00:00+00:00' >= '… 13:00:00'`` is lexically true because ``T`` sorts
    above a space.
    """
    if stale_after_days is None:
        return "", []
    if stale_after_days < 0:
        # Would render as the modifier '--N days', which SQLite rejects -> NULL
        # -> every row filtered -> an empty map with no error anywhere. Refuse
        # loudly instead of returning a plausible-looking nothing.
        raise ValueError(
            f"stale_after_days must be >= 0 or None, got {stale_after_days}"
        )
    # Clamped to now: MAX() is unbounded ABOVE, so without MIN() a single row
    # stamped in the future would define the window for every other row and hide
    # all of them. julianday() (not a lexical MIN) because stored values are
    # ``T``-separated with an offset while ``datetime('now')`` is space-
    # separated — a lexical compare between those two shapes is meaningless.
    return (
        " AND julianday(updated_at) >= "
        "MIN(COALESCE("
        "(SELECT MAX(julianday(updated_at)) FROM capability_map), "
        "julianday('now')), julianday('now')) - ?",
        [int(stale_after_days)],
    )


async def get_all(db: aiosqlite.Connection) -> list[dict]:
    """Return all capability map entries ordered by confidence descending.

    The literal table, unfiltered. For anything rendered into an ego prompt use
    :func:`get_prompt_rows` instead — a raw read includes rows the aggregator no
    longer vouches for and rows too thin to be evidence.

    No production caller today; every prompt-facing reader uses
    :func:`get_prompt_rows`. Kept as the plain accessor so that a future
    non-prompt consumer has one that does not carry ego-prompt policy.
    """
    cur = await db.execute(
        "SELECT domain, confidence, sample_size, trend, evidence_summary, updated_at "
        "FROM capability_map ORDER BY confidence DESC"
    )
    rows = await cur.fetchall()
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=False)) for r in rows]


async def count_all(db: aiosqlite.Connection) -> int:
    """Total rows in the map, ignoring both bars.

    Exists so a renderer can tell "the map is empty" apart from "every row was
    filtered out" — two states that must not produce the same sentence, since
    each is a false claim in the other's situation.
    """
    cur = await db.execute("SELECT COUNT(*) FROM capability_map")
    row = await cur.fetchone()
    return int(row[0]) if row else 0


async def get_prompt_rows(
    db: aiosqlite.Connection,
    *,
    stale_after_days: int | None = STALE_AFTER_DAYS,
    min_sample_size: int | None = MIN_SAMPLE_SIZE,
) -> list[dict]:
    """Capability entries fit to render into an ego prompt, confidence-DESC.

    Every row here is read by the ego as a present-tense claim about itself, so
    two bars apply. Rows the aggregator has stopped vouching for are excluded
    (see :data:`STALE_AFTER_DAYS`), as are rows below :data:`MIN_SAMPLE_SIZE`
    combined samples — the same floor the aggregator applies when writing, and
    the one :func:`get_weakest` has always applied.

    The sample floor is not redundant with the aggregator's: rows written before
    that floor existed are still in the table, and because this read is
    confidence-ordered into a top-N, a single-sample row would otherwise outrank
    domains backed by dozens of samples.

    Pass ``None`` to either bar to drop it. Note the floor here is a constant
    while the capability-improvement scanner passes an operator-tunable value to
    :func:`get_weakest`; if that knob is set below :data:`MIN_SAMPLE_SIZE` the
    scanner can target a domain this read omits. That domain still reaches the
    ego through the focused-deficiency line, which reads
    :func:`get_by_domain` and is deliberately unfiltered.
    """
    clause, params = _recency_clause(stale_after_days)
    if min_sample_size is not None:
        clause += " AND sample_size >= ?"
        params = [*params, int(min_sample_size)]
    cur = await db.execute(
        "SELECT domain, confidence, sample_size, trend, evidence_summary, updated_at "
        # sample_size DESC breaks ties: on a live install 19 domains sit at
        # exactly 1.0, so without a secondary key SQLite picks an
        # arbitrary 15 and an n=3 row can displace one with n=3276 from
        # the rendered table. The floor alone does not fix that — every
        # tied row already clears it.
        "FROM capability_map WHERE 1=1" + clause
        + " ORDER BY confidence DESC, sample_size DESC",
        params,
    )
    rows = await cur.fetchall()
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=False)) for r in rows]


async def get_weakest(
    db: aiosqlite.Connection,
    *,
    max_confidence: float = 0.5,
    min_sample_size: int = MIN_SAMPLE_SIZE,
    limit: int = 3,
    stale_after_days: int | None = STALE_AFTER_DAYS,
) -> list[dict]:
    """Return the weakest capability domains (lowest confidence first).

    Filters to domains scoring below *max_confidence* with at least
    *min_sample_size* aggregated data points — so a single low-n fluke never
    surfaces as a deficiency, and to non-stale rows (see
    :data:`STALE_AFTER_DAYS`) so a domain that stopped being produced months ago
    cannot present itself as today's weakest capability. Ordered by confidence
    ascending and capped at *limit*. Feeds the advisory capability-improvement
    scanner in the ego cadence manager; read-only, never mutates the map.
    """
    clause, params = _recency_clause(stale_after_days)
    cur = await db.execute(
        "SELECT domain, confidence, sample_size, trend, evidence_summary, updated_at "
        "FROM capability_map "
        "WHERE confidence < ? AND sample_size >= ?" + clause + " "
        # Same tiebreak rationale as get_prompt_rows: among equally-weak
        # domains, hand the scanner the better-evidenced one rather than an
        # arbitrary pick.
        "ORDER BY confidence ASC, sample_size DESC LIMIT ?",
        [max_confidence, min_sample_size, *params, limit],
    )
    rows = await cur.fetchall()
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=False)) for r in rows]


async def get_by_domain(db: aiosqlite.Connection, domain: str) -> dict | None:
    """Fetch a single domain's capability entry."""
    cur = await db.execute(
        "SELECT domain, confidence, sample_size, trend, evidence_summary, updated_at "
        "FROM capability_map WHERE domain = ?",
        (domain,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row, strict=False))
