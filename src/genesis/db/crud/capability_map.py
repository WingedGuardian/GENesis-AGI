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
# Anchor. The freshest row that is USABLE — parseable, date-shaped, and not
# in the future. There is no MIN()/clamp in the SQL: future rows are
# EXCLUDED inside the subquery's WHERE rather than clamped after the fact.
# (An earlier round did clamp; filtering first replaced it, for the reason
# spelled out below. This comment described the old shape for two rounds.)
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
# The anchor is the newest row that is both PARSEABLE and NOT IN THE FUTURE,
# and each half of that is load-bearing:
#
#   · ``MAX(julianday(...))``, not ``julianday(MAX(...))``. ``MAX`` over the raw
#     column is a LEXICAL max over text, so a malformed value ('zzzz-...')
#     outranks every ISO timestamp and parses to NULL. Aggregating over PARSED
#     values skips unparseable rows instead of letting one decide the window.
#   · ``WHERE julianday(updated_at) <= julianday('now')`` — excluded BEFORE the
#     max, not clamped after. A future-dated row is perfectly parseable, so it
#     wins the max; clamping the result to now then leaves a uniformly-old table
#     entirely outside the window. Filtering first means a skewed row is ignored
#     rather than promoted to anchor.
#
# Both failure modes are individually survivable and together were not: each
# guard was correct alone, and the combination still hid every valid row.
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


# A row's timestamp is USABLE only if it LOOKS LIKE A DATE, PARSES, and is NOT
# IN THE FUTURE.
#
# TWO of those three are independently load-bearing. ``IS NOT NULL`` is NOT:
# ``julianday(x) <= julianday('now')`` is itself NULL — and therefore never
# true — whenever ``julianday(x)`` is NULL, so the comparison already excludes
# every row the NULL check would have. MEASURED: dropping the conjunct changes
# the verdict on 0 of 22 adversarial values, and the mutation survives the
# suite. It is kept because it states the intent at the point of use and costs
# nothing, not because removing it would change behaviour.
#
# (Recorded because the opposite was asserted here for a round, reasoning from
# "julianday() can return NULL" — true — to "so this conjunct is needed" —
# which does not follow, and was never measured.)
#
# Defined once and applied in BOTH places that need it -- choosing the anchor,
# and choosing the rows returned. Applying it to only one of the two is not a
# weaker version of the same protection, it is a distinct defect: with the
# predicate on the anchor alone, a future-dated row is excluded from defining
# the window but still satisfies `>= anchor - N`, so the corrupt row is the one
# thing the read renders as current. It then feeds `get_weakest`, which drives
# the capability-improvement scanner -- and because a row nothing rewrites can
# never age, it would do so indefinitely.
#
# That is exactly what happened: an earlier round added the future-row guard to
# the subquery and left the outer predicate alone. Hence a shared constant
# rather than two hand-kept-in-sync copies.
#
# WHY THE SHAPE GATE. "It parses" is a far weaker claim than it reads as:
# `julianday()` accepts SQLite's whole time-string grammar, not just ISO dates.
# Measured, these all parse and are all <= now, so the other two halves admit
# every one of them:
#
#   'now' / 'NOW'      -> resolved against the WALL CLOCK
#   '12:00'            -> a bare time (2000-01-01)
#   '2460000' / '0'    -> a raw Julian day NUMBER
#
# The first is the one that bites, and it defeats the central guarantee of this
# whole design. The anchor is `MAX(updated_at)` rather than wall-clock so that a
# dead refresh job ages the table UNIFORMLY and hides nothing. A row storing
# 'now' re-dates itself on every read, so it is permanently the freshest: it
# wins MAX() forever, pins the window to wall-clock, and every real row falls
# outside it. Reproduced -- three 60-day-old rows plus one 'now' row returned
# ONLY the corrupt row. Neither existing guard sees it: it is not in the future,
# and it parses.
#
# The GLOB requires a leading YYYY-MM-DD, which no member of that grammar has.
# It does NOT make the NULL check redundant -- '2026-13-45' is date-SHAPED and
# still parses to NULL -- and it rejects no legitimate stored format (verified
# against isoformat() with and without offset, SQLite's space-separated
# rendering, and a bare date).
#
# Not reachable from application code today: `upsert` generates `updated_at`
# itself and no caller supplies it. This predicate exists precisely to tolerate
# values application code did not write -- a bad backfill, a hand-edit, a
# restore -- so a gap in the direction it claims to cover is worth closing on
# its own terms rather than on a reachability argument.
_USABLE_TIMESTAMP = (
    "updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*' "
    "AND julianday(updated_at) IS NOT NULL "
    "AND julianday(updated_at) <= julianday('now')"
)


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
    # ``MAX(julianday(...))``, not ``julianday(MAX(...))``.
    #
    # The motivating input is NOT a malformed value — the GLOB in the same
    # subquery's WHERE already excludes those. It is a table holding MIXED
    # timestamp FORMATS, which is exactly what a backfill or a restore produces
    # and what this predicate exists to tolerate. ``MAX`` over the raw column is
    # a LEXICAL max over text, and across formats that picks the wrong row:
    #
    #   '2026-08-20T01:00:00+00:00' vs '2026-08-20 05:00:00'
    #     MAX(julianday(...)) -> 2461272.708  (the 05:00 row, correct)
    #     julianday(MAX(...)) -> 2461272.542  (the 01:00 row, because 'T' > ' ')
    #
    # Same failure across mixed UTC offsets. The window then shifts by hours.
    #
    # The COALESCE fallback is belt-and-braces, not an active guard: the
    # subquery's WHERE is textually the same predicate as the outer one, so it
    # yields NULL only when no row passes the outer predicate either, and the
    # read is empty regardless. Kept so the two cannot diverge silently in
    # future without the fallback already being there.
    return (
        f" AND {_USABLE_TIMESTAMP}"
        " AND julianday(updated_at) >= "
        "COALESCE((SELECT MAX(julianday(updated_at)) FROM capability_map "
        f"          WHERE {_USABLE_TIMESTAMP}), "
        "         julianday('now')) - ?",
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


async def count_unusable(db: aiosqlite.Connection) -> int:
    """Rows whose ``updated_at`` the window cannot classify.

    Exists because the filter's failure mode is otherwise SILENT and
    PERMANENT: a malformed row is excluded from both the anchor and the result
    set, and nothing ever rewrites it (the aggregator only re-emits domains
    that clear its gates), so it disappears from the self-model forever with no
    signal anywhere. "Never hide broken things" applies to a filter as much as
    to a crash.
    """
    cur = await db.execute(
        f"SELECT COUNT(*) FROM capability_map WHERE NOT ({_USABLE_TIMESTAMP})"
    )
    row = await cur.fetchone()
    return int(row[0]) if row else 0


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
