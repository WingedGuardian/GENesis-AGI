"""CRUD for ``pending_issue_posts`` — the Contributor Work-Log hold store.

A row is written when the ``contributor_issue_propose`` MCP tool holds a
sanitized issue draft awaiting owner approval (paired with an
``approval_requests`` row). The resolution watcher drains ``status='held'``
rows: on approval it posts the issue to GitHub and marks 'posted'; on
rejection/timeout it marks 'rejected'/'expired'; in the default ``propose_only``
lever mode an approved hold is shadow-observed once and marked ``'dry_run'``
(TERMINAL — never posted; flipping the lever to ``live`` must not retro-post it).
``mark_posted``/``mark_rejected``/``mark_dry_run`` all gate on
``WHERE status='held'`` so a row can leave 'held' exactly once (double-post
guard, alongside the ``request_id`` UNIQUE constraint).
"""

from __future__ import annotations

import aiosqlite


async def create(
    db: aiosqlite.Connection,
    *,
    id: str,
    request_id: str,
    repo: str,
    title: str,
    body: str,
    source: str,
    cell_domain: str,
    cell_verb: str,
    cell_risk_class: str,
    held_at: str,
    mode: str,
    labels: str | None = None,
    source_ref: str | None = None,
) -> str:
    await db.execute(
        """INSERT INTO pending_issue_posts
             (id, request_id, repo, title, body, labels, source, source_ref,
              cell_domain, cell_verb, cell_risk_class, held_at, mode, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'held')""",
        (
            id,
            request_id,
            repo,
            title,
            body,
            labels,
            source,
            source_ref,
            cell_domain,
            cell_verb,
            cell_risk_class,
            held_at,
            mode,
        ),
    )
    await db.commit()
    return id


async def get_by_id(db: aiosqlite.Connection, id: str) -> dict | None:
    cursor = await db.execute("SELECT * FROM pending_issue_posts WHERE id = ?", (id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_by_request(db: aiosqlite.Connection, request_id: str) -> dict | None:
    cursor = await db.execute(
        "SELECT * FROM pending_issue_posts WHERE request_id = ?", (request_id,)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def list_held(db: aiosqlite.Connection) -> list[dict]:
    """All rows still awaiting resolution — the watcher's work list."""
    cursor = await db.execute(
        "SELECT * FROM pending_issue_posts WHERE status = 'held' ORDER BY held_at"
    )
    return [dict(r) for r in await cursor.fetchall()]


async def mark_posted(
    db: aiosqlite.Connection,
    id: str,
    *,
    issue_number: int,
    issue_url: str,
    posted_at: str,
    adopted: bool = False,
) -> bool:
    """Transition held → posted, recording the created issue. Returns False if
    the row already left 'held' (double-post guard: only one caller can flip a
    given hold).

    ``adopted`` records provenance for the WS-A close-loop. ``False`` (default) =
    Genesis CREATED this issue — an authoritative close-link, so a PR closing it may
    auto-resolve the originating follow_up. ``True`` = Genesis merely ADOPTED a
    pre-existing open issue it did NOT author (an external coincidental-title issue),
    so its later closure must NOT auto-resolve the follow_up. Only created
    (``adopted=0``) rows feed ``posted_index_for_repo`` (a Genesis crash-recovery
    adopt of an issue Genesis itself authored is classified ``adopted=0`` by the
    drain — it is still authoritative)."""
    cursor = await db.execute(
        "UPDATE pending_issue_posts "
        "SET status = 'posted', issue_number = ?, issue_url = ?, posted_at = ?, adopted = ? "
        "WHERE id = ? AND status = 'held'",
        (issue_number, issue_url, posted_at, int(adopted), id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def count_posted_since(db: aiosqlite.Connection, *, since: str) -> int:
    """Number of issues actually POSTED to GitHub at/after ``since`` (ISO-UTC ts) —
    the poster drain's rolling-window rate-limit count (cautious-rollout cap).

    Counts ``status='posted'`` only, so ``dry_run`` holds (propose_only) never
    consume a slot and the count is reconstructed from the durable table (survives
    a mid-window restart — an in-memory counter would not). Mirrors
    ``autonomous_email_sends.count_for_cell_since``. Global (not repo-scoped): the
    cautious-rollout intent is total owner exposure, and this install posts to one
    repo — global generalizes cleanly to a multi-repo future."""
    cursor = await db.execute(
        "SELECT COUNT(*) FROM pending_issue_posts WHERE status = 'posted' AND posted_at >= ?",
        (since,),
    )
    row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def list_dedup_active(db: aiosqlite.Connection, repo: str) -> list[dict]:
    """Rows in *repo* that should block a duplicate proposal: still awaiting
    owner review ('held') or already live ('posted'). ``dry_run`` deliberately
    does NOT block — a dry-run hold is re-proposed under 'live' mode to actually
    post it; ``rejected``/``expired`` also don't block (an item may be
    re-drafted). Returns id/title/source_ref/status for the caller to normalize
    and compare (bounded small by the ``max_held`` backpressure knob).

    The ``repo`` comparison is ``COLLATE NOCASE`` (mirroring
    ``posted_index_for_repo``): the proposer stores the repo lowercased while the
    dedup call passes the raw config slug, so a case-sensitive match would miss
    every existing row and silently bypass ``max_held`` backpressure."""
    cursor = await db.execute(
        "SELECT id, title, source_ref, status FROM pending_issue_posts "
        "WHERE repo = ? COLLATE NOCASE AND status IN ('held', 'posted')",
        (repo,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def posted_index_for_repo(db: aiosqlite.Connection, repo: str) -> dict[int, str]:
    """Map ``issue_number → source_ref`` (the follow_up id) for POSTED,
    follow_up-sourced issues in *repo* — the WS-A close-loop join.

    A merged contributor PR's closed issue number resolves the follow_up that
    spawned the issue. Only 'posted' rows carry an ``issue_number``; the map is
    scoped to ``source='follow_up'`` rows (the authoritative filter — write-time
    does not enforce that only follow_up rows carry a ``source_ref``, so a stray
    ``source_ref`` on a Cat-1 ``codebase`` row must not leak in). Cat-1 codebase
    issues have ``source_ref`` NULL and are excluded by both the source and the
    NOT-NULL guard. There is no UNIQUE index on (repo, issue_number); a duplicate
    issue_number would be a data anomaly, so the FIRST row (by rowid) wins rather
    than corrupting the map.

    ``adopted = 0`` excludes issues Genesis ADOPTED but did not author (external
    coincidental-title issues): a PR closing such an issue must not auto-resolve the
    follow_up. Genesis-CREATED issues (including a crash-recovery adopt of an issue
    Genesis authored, classified adopted=0 by the drain) remain authoritative. Legacy
    rows posted before this column existed default to 0 (treated as created) — an
    accepted, bounded residue since adoption is rare and the column is net-new.

    The ``repo`` comparison is ``COLLATE NOCASE``: the stored repo is
    config-derived while the caller's repo is gh-canonical (``gh repo view``), and
    a casing divergence would otherwise silently no-op the whole close-loop.
    Requires ``db.row_factory = aiosqlite.Row``.
    """
    cursor = await db.execute(
        "SELECT issue_number, source_ref FROM pending_issue_posts "
        "WHERE repo = ? COLLATE NOCASE AND status = 'posted' "
        "AND source = 'follow_up' "
        "AND issue_number IS NOT NULL AND source_ref IS NOT NULL "
        "AND adopted = 0 "
        "ORDER BY rowid",
        (repo,),
    )
    index: dict[int, str] = {}
    for row in await cursor.fetchall():
        number = int(row["issue_number"])
        if number not in index:
            index[number] = str(row["source_ref"])
    return index


async def mark_dry_run(db: aiosqlite.Connection, id: str, *, dry_run_at: str) -> bool:
    """Transition held → dry_run (propose_only: approved but not posted). TERMINAL
    — a later post/reject must not override it, so a lever flip to 'live' never
    retro-posts a dry-run hold. Returns False if the row already left 'held'."""
    cursor = await db.execute(
        "UPDATE pending_issue_posts SET status = 'dry_run', dry_run_at = ? "
        "WHERE id = ? AND status = 'held'",
        (dry_run_at, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def mark_rejected(
    db: aiosqlite.Connection, id: str, *, rejected_at: str, expired: bool = False
) -> bool:
    """Transition held → rejected (or expired). Returns False if not still held."""
    status = "expired" if expired else "rejected"
    cursor = await db.execute(
        "UPDATE pending_issue_posts SET status = ?, rejected_at = ? "
        "WHERE id = ? AND status = 'held'",
        (status, rejected_at, id),
    )
    await db.commit()
    return cursor.rowcount > 0


def _iso_days_before(now_iso: str, days: int) -> str:
    """Return the ISO8601 timestamp *days* before ``now_iso``."""
    from datetime import datetime, timedelta

    return (datetime.fromisoformat(now_iso) - timedelta(days=days)).isoformat()


async def prune_terminal(db: aiosqlite.Connection, *, older_than_days: int = 30, now: str) -> int:
    """Delete TERMINAL rows (posted / rejected / expired / dry_run) whose terminal
    timestamp is older than *older_than_days*. ``held`` rows are NEVER pruned —
    they await the owner indefinitely. ``now`` is injected (never wall-clock) so
    the cutover is deterministic and testable. Wired into ``disk_hygiene.sh``.
    Returns the number of rows deleted.

    Close-loop protection: a ``'posted'`` row whose ``source_ref`` is a STILL-OPEN
    follow_up is retained regardless of age — the WS-A close-loop reads it to map
    ``issue_number → follow_up`` when a contributor later closes the issue. Pruning
    it (an issue can stay open past *older_than_days* before a close lands) would
    orphan the follow_up so it never resolves. "Open" mirrors the exact predicate
    the close-loop resolves against (``get_open_followups`` / ``absorb_followup``):
    ``kind='follow_up' AND status IN ('pending', 'in_progress')``. A ``'posted'``
    row with ``source_ref`` NULL (codebase) OR whose follow_up is resolved/tabled
    stays prunable; non-posted terminal rows are always prunable. An ``adopted``
    posted row is ALSO prunable on the normal schedule — it is excluded from
    ``posted_index_for_repo``, so it can never resolve a follow_up and retaining it
    past its age would only waste storage (the ``adopted = 0`` guard below)."""
    cutoff = _iso_days_before(now, older_than_days)
    cursor = await db.execute(
        "DELETE FROM pending_issue_posts "
        "WHERE status IN ('posted', 'rejected', 'expired', 'dry_run') "
        "AND COALESCE(posted_at, rejected_at, dry_run_at, held_at) < ? "
        "AND NOT (status = 'posted' AND adopted = 0 AND source_ref IS NOT NULL AND EXISTS ("
        "  SELECT 1 FROM follow_ups f "
        "  WHERE f.id = pending_issue_posts.source_ref "
        "  AND f.kind = 'follow_up' "
        "  AND f.status IN ('pending', 'in_progress')"
        "))",
        (cutoff,),
    )
    await db.commit()
    return cursor.rowcount if cursor.rowcount is not None else 0
