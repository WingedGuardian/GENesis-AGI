"""CRUD for pr_verifications — per-merged-PR post-merge verification obligations.

Issue #1718 half B. One row per merged PR, written ONLY by the repo-pulse
worker's verification lane (``repo_pulse_worker._verification_lane``): a
docs-only diff arrives already ``closed`` with the deterministic-exemption
reason; everything else arrives ``open`` and stays open until the validator
session records evidence via :func:`close_verification` when the obligation is discharged and via :func:`record_attempt` when it is not — a FAILED verification is the highest-value record here and keeps its document too. Read by the worker
CLI's ``--verification-backlog`` (the day-one reader) and, later, the Wave-3
validator.

Why its own table rather than ``follow_ups`` is recorded once, where the store
is born: the ``20260906234824_pr_verifications`` migration docstring. Short
version: follow_ups' readers (ego dispatch, morning report) surface rows as
actionable work, and these are a ledger, not work.

Subprocess writers do NOT run migrations, so writers guard on table existence
pattern) and no-op pre-migration. The migration + ``schema/_tables.py`` are
the schema authority; nothing here creates tables. ``now`` is always injected
(never wall-clock here) so behaviour is deterministic and testable.
"""

from __future__ import annotations

import uuid

import aiosqlite

STATUSES = ("open", "closed")

#: Serialised-evidence ceiling, enforced HERE because this module owns the column.
#: The CLI checks it too, so a validator gets a readable refusal before any database
#: work; this is the backstop. An adversarial audit measured 2 MiB reaching the column
#: through :func:`record_attempt` when the CLI was the only enforcement, and a second
#: caller is anticipated — the PR that shipped this names an MCP tool as deliberately
#: cut. A writer that trusts its callers to bound a column is not bounding it.
MAX_EVIDENCE_BYTES = 256 * 1024


def _check_evidence_size(evidence: str | None) -> None:
    """Raise if *evidence* would exceed :data:`MAX_EVIDENCE_BYTES` once stored.

    REFUSED rather than truncated: a cut-down evidence record still looks complete,
    which is the one failure an evidence column cannot afford. Raises rather than
    returning a status, because a caller that assembled an over-cap document has a bug
    and a silent no-op would read as a successful write.
    """
    if evidence is None:
        return
    size = len(evidence.encode("utf-8"))
    if size > MAX_EVIDENCE_BYTES:
        raise ValueError(
            f"evidence is {size} bytes, over the {MAX_EVIDENCE_BYTES}-byte cap. "
            f"Refused rather than truncated — a cut evidence record still reads as "
            f"complete."
        )


#: The four verdicts a validator session returns (owner standing ruling,
#: 2026-09-26). Enforced HERE rather than by a CHECK constraint: SQLite cannot
#: ALTER a CHECK, so a vocabulary this young — invented before the validator had
#: run once — would cost a full table rebuild to widen. A raise also gives the
#: caller (an LLM session) a message naming the legal values instead of
#: ``IntegrityError: CHECK constraint failed``.
VERDICTS = (
    "pass-mechanical",
    "pass-with-measured-gaps",
    "fail-intent",
    "cannot-verify",
)

#: The verdicts that DISCHARGE the obligation and therefore close the row. The
#: other two record an attempt and leave it OPEN, because the work is not done.
PASS_VERDICTS = ("pass-mechanical", "pass-with-measured-gaps")

# Per-CONNECTION-TARGET cache: only the TRUE result is cached — a missing table
# (pre-migration window) is re-checked every call so a subprocess writer
# self-heals the moment the server migration lands.
#
# Keyed by the connection's DB path rather than a bare module flag (the sibling
# repo_pulse crud's shape): a process-global TRUE cached against one database
# answers for every database that process later opens, and the lie surfaces as
# an OperationalError inside the lane's try/except — a silent incomplete run.
# Today's entry points touch one DB per process, so this is prophylactic; it
# costs one dict lookup and removes a trap rather than documenting it.


async def _tables_available(db: aiosqlite.Connection) -> bool:
    """Does the table exist? Asked EVERY time, deliberately uncached.

    There was a per-path cache here and it never populated: `_db_key` read
    `_conn_path` / `_path` off the connection, and MEASURED against the
    installed aiosqlite (0.22.1) neither attribute exists — so the key was
    always None, nothing was ever added, and the module docstring's claim of a
    cache was false in every execution (audit, PR #1836). The version before
    that fell back to `id(db)`, which DID cache and was worse: CPython reuses
    the id of a closed object, so a later connection to a DIFFERENT database
    could be told the tables were present without consulting `sqlite_master`.

    Rather than key it on something that works, the mechanism is gone. It was
    never load-bearing — it saved one `sqlite_master` COUNT on a short-lived
    worker connection — and a cache that has demonstrably been wrong in both of
    its implementations is not worth a third attempt. Correctness is unchanged:
    both prior versions failed toward always checking, which is what this does.
    """
    cursor = await db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name = 'pr_verifications'"
    )
    row = await cursor.fetchone()
    return bool(row and row[0] == 1)


async def tables_available(db: aiosqlite.Connection) -> bool:
    """Public existence check (see repo_pulse.tables_available for the pattern)."""
    return await _tables_available(db)


async def exists(db: aiosqlite.Connection, *, repo: str, pr_number: int) -> bool:
    """True when this merged PR already has a verification row, in ANY status.

    The lane's cheap pre-check: an already-recorded PR must not cost a
    changed-files API call on window re-coverage. False pre-migration —
    the caller then no-ops via :func:`open_verification` anyway.
    """
    if not await _tables_available(db):
        return False
    cursor = await db.execute(
        "SELECT 1 FROM pr_verifications WHERE repo = ? AND pr_number = ? LIMIT 1",
        (repo, pr_number),
    )
    return await cursor.fetchone() is not None


async def open_verification(
    db: aiosqlite.Connection,
    *,
    repo: str,
    pr_number: int,
    pr_title: str | None,
    merged_at: str,
    now: str,
    closed_reason: str | None = None,
    commit: bool = True,
) -> str:
    """Record one merged PR's verification obligation. Returns an outcome word.

    ``closed_reason`` set → the row is born CLOSED (the deterministic docs-only
    exemption; ``closed_at`` = ``now``). Otherwise it is born ``open``.

    INSERT OR IGNORE against the (repo, pr_number) unique index — the schema is
    the dedup, so a concurrent writer or a re-covered window can never
    duplicate; the precheck in :func:`exists` is an API-cost optimization, not
    the guard. Outcomes, explicit rather than a tri-state bool:

    - ``"created"`` — the row landed.
    - ``"exists"``  — a row for this (repo, pr_number) already existed; nothing
      changed (whatever its status — a closed row is a decision already made).
    - ``"unavailable"`` — pre-migration window; nothing written. The caller
      must NOT count this PR as recorded (dedup makes the retry idempotent).
    """
    if not await _tables_available(db):
        return "unavailable"
    status = "closed" if closed_reason else "open"
    cursor = await db.execute(
        "INSERT OR IGNORE INTO pr_verifications "
        "(id, repo, pr_number, pr_title, merged_at, status, closed_reason, "
        "closed_at, evidence, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
        (
            uuid.uuid4().hex,
            repo,
            pr_number,
            pr_title,
            merged_at,
            status,
            closed_reason,
            now if closed_reason else None,
            now,
        ),
    )
    if commit:
        await db.commit()
    return "created" if cursor.rowcount else "exists"


async def close_verification(
    db: aiosqlite.Connection,
    *,
    repo: str,
    pr_number: int,
    verdict: str,
    reason: str,
    evidence: str | None,
    now: str,
) -> bool:
    """Close an OPEN obligation with its verification record (the validator's
    write). True iff a row changed — a closed row never flips back or gets its
    reason overwritten, so two validators cannot fight over one PR.

    ``verdict`` MUST be in :data:`PASS_VERDICTS`, and it is set in the SAME
    UPDATE that moves ``status``. Both halves of that matter:

    * **Same statement**, because ``status`` and ``verdict`` together carry one
      meaning and two statements leave a window where they disagree. An
      adversarial review MEASURED the earlier shape — this function not touching
      ``verdict`` at all — producing ``closed`` + ``verdict IS NULL`` on every
      call, which collapsed THREE distinct meanings onto one state (docs-only
      path exemption, pre-verdict legacy row, and validator-closed) with only
      free text to tell them apart. A cross-column invariant cannot be held by
      two writers that do not read each other's column, so it is held by one.
    * **PASS only**, because ``fail-intent`` and ``cannot-verify`` do not
      discharge the obligation — they belong to :func:`record_attempt`, which
      leaves the row open. One writer per state transition.

    ``attempt_count`` is incremented here too, so it counts every attempt rather
    than only the failed ones: without this a cleanly verified PR would read
    ``attempt_count = 0``, indistinguishable from never attempted.

    ``last_attempt_note`` is CLEARED and ``evidence`` is REPLACED. A row that failed
    or parked before finally passing would otherwise carry a verdict and a sentence
    explaining why it could not be verified, side by side, and a reader would have to
    know which column wins.

    Be exact about what that costs, because an earlier draft of this docstring was not
    and claimed the superseded reason stayed recoverable: **it does not.** Closing a
    row discards the previous attempt's note AND its evidence document. What survives
    is ``attempt_count`` — how many attempts the row cost, which is the escalation
    signal and the only part not derivable from the final state. That is the accepted
    price of a last-wins record: the row always describes its LATEST state, and nothing
    here keeps attempt history. If per-attempt history is ever wanted it needs a child
    table, which the shipping PR considered and rejected against the New-Store Gate.
    """
    if verdict not in PASS_VERDICTS:
        raise ValueError(
            f"close_verification accepts only {list(PASS_VERDICTS)}, got {verdict!r}. "
            f"'fail-intent' and 'cannot-verify' do not discharge the obligation — "
            f"record them with record_attempt(), which leaves the row OPEN."
        )
    _check_evidence_size(evidence)
    if not reason or not reason.strip():
        # A closed row with no reason is an unverifiable claim in permanent
        # record — the row would say "handled" and carry nothing. The schema
        # permits it; this writer does not, because the eventual closer is a
        # validator SESSION (an LLM caller), which is exactly the caller that
        # would pass an empty string. Raise rather than return False: a caller
        # that omitted its reason has a bug, not a no-op.
        raise ValueError("close_verification requires a non-empty reason")
    if not await _tables_available(db):
        return False
    if not await _verdict_columns_available(db):
        return False
    cursor = await db.execute(
        "UPDATE pr_verifications SET status = 'closed', closed_reason = ?, "
        "closed_at = ?, evidence = ?, verdict = ?, last_attempt_at = ?, "
        "last_attempt_note = NULL, attempt_count = attempt_count + 1 "
        "WHERE repo = ? AND pr_number = ? AND status = 'open'",
        (reason, now, evidence, verdict, now, repo, pr_number),
    )
    await db.commit()
    return bool(cursor.rowcount)


async def _verdict_columns_available(db: aiosqlite.Connection) -> bool:
    """Do the verdict/attempt columns exist? Asked EVERY time, uncached.

    Uncached for the reason ``_tables_available`` is: two caching attempts there
    were wrong and the mechanism was deleted rather than fixed.

    LOAD-BEARING, not belt-and-braces. The window where the TABLE exists and
    these COLUMNS do not is structural on every install that already ran the
    original migration: the numbered runner catches up at the next restart, and
    until then a subprocess writer is looking at a ten-column table. Without this
    guard that writer raises ``no such column`` inside a caller's ``except`` and
    the run reports success having written nothing.
    """
    if not await _tables_available(db):
        return False
    cursor = await db.execute("PRAGMA table_info(pr_verifications)")
    cols = {row[1] for row in await cursor.fetchall()}
    return {"verdict", "attempt_count", "last_attempt_at", "last_attempt_note"} <= cols


async def record_attempt(
    db: aiosqlite.Connection,
    *,
    repo: str,
    pr_number: int,
    verdict: str,
    note: str,
    now: str,
    evidence: str | None = None,
) -> str:
    """Record a NON-closing validation attempt. ``"recorded" | "missing" | "unavailable"``.

    The row STAYS OPEN — ``status`` is deliberately absent from the SET list, so
    the "keep it open" rule is enforced by the SQL rather than by every caller
    remembering it. ``WHERE status = 'open'`` means a closed row can never be
    re-annotated, the same already-decided rule :func:`close_verification`
    carries.

    A validator that could not reach a verdict, or reached ``fail-intent``, has
    not discharged the obligation: the row must remain in the backlog so it is
    re-validated once the fix lands or the precondition becomes reachable. What
    this adds is that the next validator can see an attempt was MADE and why it
    did not finish, instead of re-deriving it.

    ``note`` is required and non-empty for the same reason ``reason`` is on the
    closer: "attempted but could not be completed" with no stated cause is
    precisely the unverifiable claim that guard exists to refuse.

    ``evidence`` REPLACES the stored document, and ``None`` clears it. Every attempt
    field describes the LATEST attempt, so an attempt made without a document leaves
    the row with none rather than showing an earlier attempt's document under this
    attempt's verdict. Capped at :data:`MAX_EVIDENCE_BYTES`; over the cap raises.
    """
    if verdict not in VERDICTS:
        raise ValueError(
            f"record_attempt: verdict must be one of {list(VERDICTS)}, got {verdict!r}"
        )
    if verdict in PASS_VERDICTS:
        raise ValueError(
            f"record_attempt refuses {verdict!r}: a PASS discharges the obligation and "
            f"must go through close_verification, so exactly one writer moves the row "
            f"to 'closed'."
        )
    _check_evidence_size(evidence)
    if not note or not note.strip():
        raise ValueError(
            "record_attempt requires a non-empty note — the whole point of the row "
            "staying open is that the next validator learns WHY it could not finish."
        )
    if not await _verdict_columns_available(db):
        return "unavailable"
    # The evidence document is stored on a NON-closing attempt too. Without this a
    # failed verification discarded everything the validator assembled — claims,
    # tiers, measurements, controls — and kept only the one-line note. A fail-intent
    # is exactly the case where that detail is worth most, because someone has to
    # act on it.
    #
    # It is REPLACED, even with NULL — never COALESCEd. An earlier version kept the
    # prior document when this attempt passed none, and a review measured what that
    # does: a second attempt recording 'cannot-verify' without a document left the
    # FIRST attempt's measured-failure document displayed under the new verdict,
    # unlabelled. The row's rule is that every attempt field describes the LATEST
    # attempt; a missing document is shown as missing, not disguised by an older one.
    cursor = await db.execute(
        "UPDATE pr_verifications SET verdict = ?, last_attempt_at = ?, "
        "last_attempt_note = ?, attempt_count = attempt_count + 1, "
        "evidence = ? "
        "WHERE repo = ? AND pr_number = ? AND status = 'open'",
        (verdict, now, note, evidence, repo, pr_number),
    )
    await db.commit()
    return "recorded" if cursor.rowcount else "missing"


async def open_repos_for_pr(db: aiosqlite.Connection, *, pr_number: int) -> list[str]:
    """Repo slugs holding an OPEN row for this PR number. Empty pre-migration.

    The EXACT-key question, asked exactly. A caller that needs "which repo holds
    open PR N" must not answer it by pulling the whole open set and filtering in
    Python: at saturation the target falls outside the window and the caller
    reports a confident FALSE "no such row" — the truncated-listing failure, in a
    writer. ``(repo, pr_number)`` is the row identity, so guessing the slug is
    how one repo's obligation gets closed with another's evidence.

    Row-factory agnostic (indexes ``row[0]``), like :func:`counts`. Note this is
    not an index seek: there is no index with ``pr_number`` leading, so it scans
    the open set via ``idx_prv_status`` — still bounded, and vastly cheaper than
    materialising 2000 rows.
    """
    if not await _tables_available(db):
        return []
    cursor = await db.execute(
        "SELECT repo FROM pr_verifications WHERE pr_number = ? AND status = 'open' ORDER BY repo",
        (int(pr_number),),
    )
    return [row[0] for row in await cursor.fetchall()]


async def list_for_pr(db: aiosqlite.Connection, *, pr_number: int) -> list[dict]:
    """Every row for one PR, OPEN or CLOSED, oldest merge first.

    Exists because :func:`list_closed` cannot answer the question a scoped read asks.
    An adversarial audit measured the gap: :func:`record_attempt` persists the evidence
    document onto an OPEN row, and nothing could display it — the backlog reader prints
    only the note and the attempt count, and every other reader filters
    ``status='closed'``. So half of the attempt record was written and unreadable, which
    is the same write-only-column defect the closed-row reader was added to fix.

    Scoped by an exact ``pr_number``, served by ``idx_prv_repo_pr``, and deliberately
    UNLIMITED: the key is (repo, pr_number), so the result is one row per repository
    holding that number — single digits on any real install, and a LIMIT here could
    hide the very row the caller is asking about.
    """
    if not await _tables_available(db):
        return []
    cursor = await db.execute(
        "SELECT * FROM pr_verifications WHERE pr_number = ? ORDER BY merged_at ASC",
        (pr_number,),
    )
    return [dict(row) for row in await cursor.fetchall()]


async def list_closed(
    db: aiosqlite.Connection, *, limit: int = 200, pr_number: int | None = None
) -> list[dict]:
    """Closed obligations, most recently closed FIRST. Empty pre-migration.

    A separate function rather than a flag on :func:`list_open` because the
    ordering key differs and that function's "oldest merge first, because the
    point is what waited longest" is meaningless once a row is discharged: what
    a reader wants from closed rows is what was decided most recently.

    ``pr_number`` filters IN SQL, and that is the point of the parameter rather
    than a convenience. A caller that pages the most recent N and then filters in
    Python reports a confident FALSE ABSENCE the moment the target sits past the
    page — MEASURED on an earlier draft of the CLI reader: with 511 closed rows it
    printed "no closed rows for PR #1" about a row that was closed
    ``pass-mechanical``, and attributed it to an empty or pre-migration table. The
    sibling :func:`open_repos_for_pr` docstring forbids exactly this shape; the
    reader broke the rule a hundred lines from where it is written down.

    Assumes a Row factory.
    """
    if not await _tables_available(db):
        return []
    lim = max(1, min(int(limit), 2000))
    if pr_number is not None:
        cursor = await db.execute(
            "SELECT * FROM pr_verifications WHERE status = 'closed' AND pr_number = ? "
            "ORDER BY closed_at DESC, repo DESC LIMIT ?",
            (int(pr_number), lim),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM pr_verifications WHERE status = 'closed' "
            "ORDER BY closed_at DESC, pr_number DESC LIMIT ?",
            (lim,),
        )
    return [dict(r) for r in await cursor.fetchall()]


async def list_open(db: aiosqlite.Connection, *, limit: int = 500) -> list[dict]:
    """Open obligations — FAILED first, then NEVER-ATTEMPTED, then PARKED;
    oldest merge first within each.

    ``fail-intent`` leads because it is the one open state that is a finding: it
    goes to the user, and an open row is its only durable record, so a capped
    window must never be able to drop it behind untouched work (Codex, round 3).

    Oldest-first within each group because the backlog's point is what has
    waited longest. The group split is newer and exists because this store now
    has a PARKED class: ``record_attempt`` leaves a row open with a verdict set,
    and a ``cannot-verify`` row whose precondition is unreachable on this install
    cannot be discharged here at all. Those rows are typically the OLDEST, so
    under a pure ``merged_at`` sort they collect at the head of a capped window
    and push never-attempted work out of it — head-of-line blocking of this
    ledger's only reader, degrading as the parked set grows. An adversarial
    review caught this; the class did not exist before the attempt record did,
    so the ordering is part of that change rather than an unrelated fix.

    The verdict is the discriminator and it is exact on an open row: a PASS
    verdict closes the row, so an OPEN row carries a verdict only after a recorded
    non-closing attempt — ``fail-intent`` or ``cannot-verify``.

    Assumes a Row factory. Empty pre-migration — and on a pre-migration database
    the ordering degrades to plain oldest-first rather than failing, because the
    column it keys on does not exist yet.
    """
    if not await _tables_available(db):
        return []
    lim = max(1, min(int(limit), 2000))
    order = (
        "CASE WHEN verdict = 'fail-intent' THEN 0 WHEN verdict IS NULL THEN 1 "
        "ELSE 2 END ASC, merged_at ASC"
        if await _verdict_columns_available(db)
        else "merged_at ASC"
    )
    cursor = await db.execute(
        f"SELECT * FROM pr_verifications WHERE status = 'open' ORDER BY {order} LIMIT ?",
        (lim,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def counts(db: aiosqlite.Connection) -> dict:
    """Status histogram, e.g. ``{"open": 12, "closed": 40}``. Empty pre-migration."""
    out: dict = {}
    if not await _tables_available(db):
        return out
    cursor = await db.execute("SELECT status, COUNT(*) FROM pr_verifications GROUP BY status")
    for row in await cursor.fetchall():
        out[row[0]] = row[1]
    return out


async def prune_closed(
    db: aiosqlite.Connection,
    *,
    older_than_days: int = 180,
    now: str,
) -> int:
    """Delete CLOSED rows older than *older_than_days* (by closed_at). Retention
    for the unbounded store (wired into scripts/prune_repo_pulse.py → the
    disk-hygiene timer).

    OPEN rows are never pruned — an open row IS the obligation, and deleting it
    would silently forgive an unverified merge. The 180-day window on closed
    rows keeps recent gap-detection ("no row within the retention window means
    the lane never recorded that PR" — the lane fails its run rather than
    advancing the cursor past PRs it could not record, so a gap is a signal
    rather than routine) while bounding growth at ~11 merges/day ≈ 2k retained closed rows; flagged
    as a reviewable number, not derived from a hard budget. ``now`` injected.

    Rejects a sub-1-day retention window, mirroring ``prune_merge_journal``
    (crud/entities.py) which names this exact class: with ``older_than_days <= 0``
    the cutoff lands at or in the FUTURE relative to ``now`` (subtracting a
    negative pushes it forward), so ``closed_at < cutoff`` would match EVERY
    closed row. The guard lives HERE rather than only at the CLI because the
    caller that gets it wrong is the one that never thought about it — the CLI
    validates too, for a readable error instead of a traceback.
    """
    if older_than_days < 1:
        raise ValueError(
            f"prune_closed: retention window must be >= 1 day, got "
            f"{older_than_days!r}; a sub-1 window sets the cutoff at/after now and "
            f"would delete EVERY closed verification row, destroying the "
            f"gap-detection window that makes a missing row a signal."
        )
    if not await _tables_available(db):
        return 0
    from datetime import datetime, timedelta

    cutoff = (datetime.fromisoformat(now) - timedelta(days=older_than_days)).isoformat()
    cursor = await db.execute(
        "DELETE FROM pr_verifications WHERE status = 'closed' AND closed_at < ?",
        (cutoff,),
    )
    await db.commit()
    return cursor.rowcount or 0
