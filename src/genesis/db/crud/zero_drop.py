"""CRUD for ``zero_drop_findings`` — standing stranded-work conditions.

The zero-drop mandate: "what work has fallen through the cracks?" must be
answered by a reconciler that ENUMERATES, never by a session that remembers.
An enumeration is only trustworthy if the same condition seen twice is the
same row twice — that identity, and the recurrence counting built on it, is
what this store holds.

Why a NEW store (New-Store Gate justification, each candidate rejected on a
semantic mismatch, not on taste):

- ``observations`` is prose content with ``skip_if_duplicate`` DROPPING a
  repeat. A detector needs the opposite: a repeat is the signal (it is the
  third consecutive run that makes a stranded branch worth escalating), and
  there is no per-row acknowledgement concept to key on a SHA.
- ``alert_events`` holds exactly one open row per alert id — it models "is
  this alarm ringing", not "which N branches are stranded right now".
- ``reflex_signals`` is the closest SHAPE (fingerprint UNIQUE +
  occurrence_count + first/last_seen_at + muted/dismissed statuses) and this
  schema deliberately adapts it, but that table owns the self-bug-repair
  lifecycle (verdicts, repair attempts); borrowing it would put detector rows
  into a queue that another subsystem drains.
- ``repo_pulse_annotations`` is per-match-EVENT grain (one row per PR↔item
  match), not per-standing-CONDITION grain.

So: a small, detector-owned table. The detector is the SOLE writer of its own
findings and never mutates an authoritative work store (no auto-push, no
auto-PR, no ledger/follow-up writes) — those never-dos are requirements, not
commentary.

Identity is ``(class, branch)``, NOT the tip SHA. A SHA-keyed row would become
a new row on every commit, resetting ``consecutive_runs`` and silently
disarming escalation. The SHA is carried as EVIDENCE (``tip_sha``) and as the
EXPIRY KEY for an acknowledgement (``acked_tip_sha``): an ack means "I looked
at this branch AS IT WAS", so it expires the moment the branch moves.

Retention: only RESOLVED rows are pruned (``prune_zero_drop``, 45d, wired into
``scripts/disk_hygiene.sh``). An ``acked`` row is live SUPPRESSION state —
deleting one would silently un-suppress the finding on the next sweep, so acked
rows are never pruned regardless of age. Open rows are bounded by the branch
count of one install (~150 refs), so there is no unbounded growth to GC.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

import aiosqlite

logger = logging.getLogger(__name__)

CLASSES = ("unpushed_branch", "pushed_no_pr", "dirty_worktree")
STATUSES = ("open", "acked", "resolved")

# Columns a re-sighting always refreshes: current evidence about the SAME
# standing condition. Identity columns (class/branch) and lifecycle columns
# (status/counters/ack) are handled explicitly by the reconciliation below.
_EVIDENCE_COLUMNS = ("tip_sha", "ahead_count", "worktree_path", "details")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _details_json(details: dict | None) -> str | None:
    """Serialize the evidence blob, saying so LOUDLY if it cannot be.

    A silent ``return None`` here would drop the evidence for a finding while
    the finding itself still records fine — so a reader would see a condition
    with no explanation and no hint that there had been one.
    """
    if not details:
        return None
    try:
        return json.dumps(details, default=str)
    except (TypeError, ValueError) as exc:
        logger.warning("zero_drop details not serializable, storing a marker", exc_info=True)
        return json.dumps({"details_unserializable": f"{type(exc).__name__}: {exc}"[:200]})


async def get(db: aiosqlite.Connection, *, class_: str, branch: str) -> dict | None:
    cursor = await db.execute(
        "SELECT * FROM zero_drop_findings WHERE class = ? AND branch = ?",
        (class_, branch),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def apply_sweep(
    db: aiosqlite.Connection,
    *,
    class_: str,
    present: list[dict],
    run_id: str,
    now: str | None = None,
    escalation_k: int = 3,
    held: set[str] | None = None,
) -> dict:
    """Reconcile ONE finding class against a COMPLETE sweep of it. Atomic.

    ``present`` is every finding of ``class_`` this run saw, each a dict with
    ``branch`` plus any of :data:`_EVIDENCE_COLUMNS` (``details`` as a dict).
    Anything of this class NOT in ``present`` is resolved — which is exactly
    why the caller must only pass a class whose sweep COMPLETED. A degraded
    leg (a failed git call, a capped PR listing) must skip the class entirely:
    a partial sweep that resolved the branches it never looked at would
    manufacture a clean board, the one failure mode this detector exists to
    prevent. There is no half-applied middle state — one commit at the end.

    Lifecycle, per already-known row:

    ``held`` names identities the sweep SAW but could not report on this run —
    an age gate filtered them, or their worktree was unreadable. They are
    neither present nor gone, so they are left exactly as they are. Absence
    from ``present`` otherwise means "gone", and conflating the two destroys
    real state (see the hold branch below for the measured case).

    - ``resolved`` → REOPEN: a new episode. ``consecutive_runs`` restarts at 1,
      ``reopen_count`` increments, ``first_seen_at`` re-stamps, escalation
      clears, and the ack fields clear with them — a reopened row is a fresh
      condition, and a stale ``ack_reason`` sitting on it would read as
      "somebody already decided about this".
    - ``acked`` with a MOVED tip → the ack expires: back to ``open`` with the
      ack fields cleared. A lingering ``ack_reason`` on an open row reads as
      "still suppressed", so nothing is kept.
    - ``acked`` with the SAME tip → stays acked (suppression holds), but
      ``consecutive_runs`` still counts: the counter is a fact about the
      CONDITION, not about whether we chose to look at it.
    - ``open`` → ``consecutive_runs`` increments; at ``escalation_k`` the row
      stamps ``escalated_at`` once. Escalation is VISIBILITY only — nothing
      here pushes, opens, closes or unclaims anything.

    Returns per-run counts: ``{"new", "recurring", "reopened", "expired_acks",
    "escalated", "resolved", "still_acked", "held"}``, plus
    ``duplicate_identities`` only when a collision actually occurred.
    """
    ts = now or _now()
    held = set(held or ())
    counts = dict.fromkeys(
        (
            "new",
            "recurring",
            "reopened",
            "expired_acks",
            "escalated",
            "resolved",
            "still_acked",
            "held",
        ),
        0,
    )

    cursor = await db.execute(
        "SELECT * FROM zero_drop_findings WHERE class = ?",
        (class_,),
    )
    known = {row["branch"]: dict(row) for row in await cursor.fetchall()}

    seen: set[str] = set()
    for finding in present:
        branch = finding.get("branch")
        if not branch:
            continue  # a nameless finding has no identity; never invent one
        if branch in seen:
            # A duplicate identity in ONE sweep. The INSERT below would hit
            # UNIQUE(class, branch), and an IntegrityError out of here aborts
            # the whole sweep — no heartbeat, no run record, no observation, and
            # the only symptom two days later is an overdue pulse. First
            # sighting wins; the collision is counted, never silent.
            counts["duplicate_identities"] = counts.get("duplicate_identities", 0) + 1
            continue
        seen.add(branch)
        evidence = (
            finding.get("tip_sha"),
            finding.get("ahead_count"),
            finding.get("worktree_path"),
            _details_json(finding.get("details")),
        )
        prior = known.get(branch)

        if prior is None:
            await db.execute(
                """INSERT INTO zero_drop_findings
                     (id, class, branch, tip_sha, ahead_count, worktree_path,
                      status, first_seen_at, last_seen_at, consecutive_runs,
                      last_run_id, details, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, 1, ?, ?, ?, ?)""",
                (
                    uuid.uuid4().hex,
                    class_,
                    branch,
                    evidence[0],
                    evidence[1],
                    evidence[2],
                    ts,
                    ts,
                    run_id,
                    evidence[3],
                    ts,
                    ts,
                ),
            )
            counts["new"] += 1
            continue

        status = prior["status"]
        runs = int(prior["consecutive_runs"] or 0)

        if status == "resolved":
            await db.execute(
                """UPDATE zero_drop_findings
                      SET tip_sha = ?, ahead_count = ?, worktree_path = ?, details = ?,
                          status = 'open', first_seen_at = ?, last_seen_at = ?,
                          consecutive_runs = 1, reopen_count = reopen_count + 1,
                          escalated_at = NULL, resolved_at = NULL,
                          ack_reason = NULL, acked_at = NULL, acked_tip_sha = NULL,
                          last_run_id = ?, updated_at = ?
                    WHERE id = ?""",
                (*evidence, ts, ts, run_id, ts, prior["id"]),
            )
            counts["reopened"] += 1
            continue

        runs += 1
        ack_expired = status == "acked" and prior["acked_tip_sha"] != evidence[0]

        if ack_expired:
            await db.execute(
                """UPDATE zero_drop_findings
                      SET tip_sha = ?, ahead_count = ?, worktree_path = ?, details = ?,
                          status = 'open', consecutive_runs = ?,
                          ack_reason = NULL, acked_at = NULL, acked_tip_sha = NULL,
                          last_seen_at = ?, last_run_id = ?, updated_at = ?
                    WHERE id = ?""",
                (*evidence, runs, ts, run_id, ts, prior["id"]),
            )
            counts["expired_acks"] += 1
            status = "open"
        else:
            await db.execute(
                """UPDATE zero_drop_findings
                      SET tip_sha = ?, ahead_count = ?, worktree_path = ?, details = ?,
                          consecutive_runs = ?, last_seen_at = ?, last_run_id = ?,
                          updated_at = ?
                    WHERE id = ?""",
                (*evidence, runs, ts, run_id, ts, prior["id"]),
            )
            if status == "acked":
                counts["still_acked"] += 1
            else:
                counts["recurring"] += 1

        if status == "open" and runs >= escalation_k and not prior["escalated_at"]:
            await db.execute(
                "UPDATE zero_drop_findings SET escalated_at = ?, updated_at = ? WHERE id = ?",
                (ts, ts, prior["id"]),
            )
            counts["escalated"] += 1

    for branch, prior in known.items():
        if branch in seen or prior["status"] == "resolved":
            continue
        if branch in held:
            # Swept, still there, just not REPORTABLE this run (under an age
            # gate, or its worktree could not be read). Absence-from-`present`
            # means three different things and only one of them is "gone" —
            # conflating them destroyed real state: MEASURED on this build, one
            # edit inside an acknowledged worktree pushed it under the 6h gate
            # for a single sweep, which resolved the row and threw away a
            # written acknowledgement the branch had never invalidated. An open
            # row fared no better: it resolved and reopened as a new episode,
            # resetting its recurrence count while nothing about the condition
            # had changed.
            counts["held"] += 1
            continue
        await db.execute(
            """UPDATE zero_drop_findings
                  SET status = 'resolved', resolved_at = ?, escalated_at = NULL,
                      last_run_id = ?, updated_at = ?
                WHERE id = ?""",
            (ts, run_id, ts, prior["id"]),
        )
        counts["resolved"] += 1

    await db.commit()
    return counts


async def ack(
    db: aiosqlite.Connection,
    *,
    class_: str,
    branch: str,
    reason: str,
    now: str | None = None,
) -> dict | None:
    """Acknowledge one finding — suppression, keyed on the tip it was seen at.

    Records ``acked_tip_sha`` from the row's CURRENT ``tip_sha``, so the next
    sweep re-opens the finding the moment the branch moves: an ack is a
    statement about the work as it stood, never a permanent mute. A ``reason``
    is mandatory by signature — an unexplained suppression is indistinguishable
    from a forgotten one. Returns the updated row, or None if the finding is
    unknown or already resolved (there is nothing to suppress).
    """
    ts = now or _now()
    row = await get(db, class_=class_, branch=branch)
    if row is None or row["status"] == "resolved":
        return None
    await db.execute(
        """UPDATE zero_drop_findings
              SET status = 'acked', ack_reason = ?, acked_at = ?, acked_tip_sha = ?,
                  updated_at = ?
            WHERE id = ?""",
        (reason, ts, row["tip_sha"], ts, row["id"]),
    )
    await db.commit()
    return await get(db, class_=class_, branch=branch)


async def list_findings(
    db: aiosqlite.Connection,
    *,
    statuses: tuple[str, ...] = ("open", "acked"),
    class_: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Findings in ``statuses``, escalated first, then longest-standing.

    ``limit`` PAGES a listing (the caller renders "n of N" from
    :func:`counts_by_status`) — it never stands in for the total.
    """
    if not statuses:
        return []
    params: list = list(statuses)
    sql = f"SELECT * FROM zero_drop_findings WHERE status IN ({','.join('?' * len(statuses))})"
    if class_:
        sql += " AND class = ?"
        params.append(class_)
    sql += " ORDER BY (escalated_at IS NULL), consecutive_runs DESC, first_seen_at"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    cursor = await db.execute(sql, tuple(params))
    return [dict(r) for r in await cursor.fetchall()]


async def counts_by_status(db: aiosqlite.Connection) -> dict[str, int]:
    """Full COUNTs per status — the denominator every surface must render."""
    cursor = await db.execute(
        "SELECT status, COUNT(*) AS n FROM zero_drop_findings GROUP BY status"
    )
    counts = dict.fromkeys(STATUSES, 0)
    for row in await cursor.fetchall():
        counts[row["status"]] = int(row["n"])
    return counts


async def prune_zero_drop(
    db: aiosqlite.Connection,
    *,
    older_than_days: int = 45,
    now: str | None = None,
) -> int:
    """Delete RESOLVED findings older than the window. Returns rows deleted.

    Resolved rows only, by design: an ``acked`` row is live suppression state
    and deleting one would silently un-suppress the finding on the next sweep
    (the ack would have to be made again, from scratch, by whoever noticed).
    Open rows are the working set. See the module docstring's Retention note.
    """
    ts = datetime.fromisoformat(now) if now else datetime.now(UTC)
    cutoff = (ts - timedelta(days=older_than_days)).isoformat()
    # datetime() on both sides, not a lexicographic string compare: rows are
    # written with whatever offset their writer used, and '2026-01-01T15:00+00:00'
    # sorts ahead of the actually-later '2026-01-01T11:30-04:00'. A retention
    # sweep that mis-sorts deletes rows it should keep.
    cursor = await db.execute(
        "DELETE FROM zero_drop_findings "
        "WHERE status = 'resolved' "
        "AND datetime(COALESCE(resolved_at, last_seen_at)) < datetime(?)",
        (cutoff,),
    )
    await db.commit()
    return cursor.rowcount or 0
