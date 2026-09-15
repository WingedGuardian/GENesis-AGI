"""Escalate an undisposed ledger row into a follow-up once nobody can dispose it.

A ``session_ledger`` row lives ONLY on the ledger. A follow-up is durable and
system-wide, surfaced by several consumers; a ledger row is rendered by its own
session's context injection and by nothing else. So when a session dies with
open rows, its commitments become unreachable — not deleted, just invisible to
every process and every person.

CLAUDE.md already states the rule: *every open row is either getting done or
gets a disposition (done / absorbed / dropped, with the reason) — a row left
undisposed is a defect, not a backlog.* A live session enforces that on itself.
This sweep enforces it once the owning session can no longer be asked.

MEASURED on the live ledger 2026-09-06, which is why BOTH thresholds exist: 50
rows were open, 15 qualified at 5d/5d, and the oldest had been untouched 52
days. Reading them showed they are not one kind of thing — several record their
own completion and were simply never closed, several are real unbuilt work, and
two were user-world errands the owner asked for and never got (one labelled
"NEXT TASK (post-compact, user-requested)", invisible for 15 days). That last
class is the acceptance case: nothing else surfaces it, because it is not repo
work, not a follow-up, and its session is gone.

THE LINK is the follow-up's ``dedup_key``
(``ledger_escalation_link.escalation_dedup_key``), whose module docstring names
this sweep as its missing writer. The two import-free hooks already render
``-> escalated: follow_up <id>`` beside an open row, so a REVIVED session sees
and can close its own escalation. That surface is deliberately NOT counted as
coverage for the population here: an escalated row's session is by definition
quiet, so in practice nothing renders it.

WHAT THIS SWEEP NEVER DOES: write ``session_ledger``. An evidence write would
bump ``updated_at`` and read as a disposition the owner never made — the sweep
would then look like it had answered its own question.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from genesis.db.crud import follow_ups as fu_crud
from genesis.db.crud.session_charters import ledger_all, ledger_stale_open
from genesis.session_awareness.ledger_escalation_config import (
    escalate_added_by,
    knob_int,
    load_config,
    priority,
)
from genesis.session_awareness.ledger_escalation_link import (
    ESCALATION_SOURCE,
    escalation_dedup_key,
)

logger = logging.getLogger(__name__)

# A ledger row in one of these has been dispositioned — the escalation asking
# for that disposition is answered and gets completed.
_TERMINAL_LEDGER_STATUSES = frozenset({"done", "absorbed", "dropped"})

# The follow_ups statuses reconcile must still consider. `follow_ups.status`
# admits six values (schema/_tables.py); these are the four that are not
# terminal. Enumerated rather than expressed as "not completed/failed" because
# the query filters by ONE status at a time — see the reconcile read, where
# filtering after the bound starved the rows the sweep exists to close.
_NON_TERMINAL_FOLLOW_UP_STATUSES = ("pending", "scheduled", "in_progress", "blocked")

# Bound on the pending-escalations read during reconcile. Derived from capacity,
# not from history: the dedup precheck allows at most ONE escalation per ledger
# row, so the pending population can never exceed the number of unresolved
# ledger rows — and the ENTIRE session_ledger table held 181 rows on this
# install on 2026-09-06 (49 of them unresolved). 1000 is >5x the whole table.
# `get_by_source` orders created_at DESC, so a saturated read would starve the
# OLDEST escalations specifically; that is logged loudly rather than left to be
# discovered, because silent starvation here means a disposed row's follow-up
# stays pending forever.
_RECONCILE_READ_LIMIT = 1000


def _parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 stamp to an aware UTC datetime, or None."""
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _last_touched(row: dict) -> datetime | None:
    """When the ledger row was last touched — updated_at, else created_at."""
    return _parse_iso(row.get("updated_at")) or _parse_iso(row.get("created_at"))


def _session_last_active(
    sessions_dir: Path, session_id: str, *, fallback: datetime | None
) -> tuple[datetime | None, str]:
    """When the owning session last prompted, and how we know.

    ``~/.genesis/sessions/<sid>/last_prompt_time`` is rewritten on every prompt
    of every FOREGROUND session (``scripts/genesis_urgent_alerts.py``), so it is
    the liveness signal — the same file the context-injection watcher reads.

    Content is preferred over mtime, deliberately, and the two normally agree
    because one ``write_text(now.isoformat())`` sets both. They diverge after a
    RESTORE from backup, which rewrites every file with a fresh mtime while
    preserving the recorded instant. Trusting mtime there would make every dead
    session look live and silence the sweep entirely; content stays correct.
    mtime remains the fallback for an unparseable file.

    An ABSENT file means no foreground session ever wrote one — the dispatched
    case, since ``genesis_urgent_alerts`` returns early under
    ``GENESIS_CC_SESSION=1``. Those sessions are unattended by definition, so we
    fall back to the row's own age and SAY SO in the follow-up rather than
    treating "no evidence of life" as evidence of life.
    """
    path = sessions_dir / session_id / "last_prompt_time"
    try:
        # TWO guards, doing different jobs — stated separately because either
        # alone keeps the sweep alive, so neither is evidence for the other.
        #
        # errors="replace" is the load-bearing one: it stops read_text raising
        # on a truncated or partially-written file, which keeps the mtime
        # fallback below REACHABLE. That is a better answer than the absent-file
        # fallback — the file's real last-write time is evidence we would
        # otherwise discard. (Pinned by the "mtime" assertion in
        # test_a_corrupt_liveness_file_does_not_kill_the_sweep.)
        #
        # Catching ValueError beside OSError is belt-and-braces for a decode
        # error that gets past it. With errors="replace" in place I could not
        # construct an input that reaches it, so treat it as defence against an
        # unforeseen path, NOT as the thing keeping the sweep alive.
        #
        # Why either matters at all: UnicodeDecodeError is a ValueError and NOT
        # an OSError (verified), so the obvious `except OSError` lets it escape
        # this function, the candidate loop and run_sweep — and ONE corrupt file
        # from one unrelated session would then kill the whole sweep, hourly and
        # permanently, because the file persists. Fail-shut is right for a single
        # row and badly wrong for the entire population.
        recorded = _parse_iso(path.read_text(errors="replace"))
    except (OSError, ValueError):
        return fallback, "no liveness file — assumed idle since the row was created"
    if recorded is not None:
        return recorded, "last_prompt_time"
    try:
        return (
            datetime.fromtimestamp(path.stat().st_mtime, tz=UTC),
            "last_prompt_time mtime (file contents unparseable)",
        )
    except OSError:
        return fallback, "no liveness file — assumed idle since the row was created"


def _age_days(then: datetime | None, now: datetime) -> float | None:
    if then is None:
        return None
    return (now - then).total_seconds() / 86400.0


def _render_content(
    row: dict,
    *,
    now: datetime,
    untouched_days: float,
    quiet_days: float | None,
    liveness_source: str,
    sessions_dir: Path,
) -> str:
    """The follow-up body: what the row is, and the decision being asked for.

    PRIVACY / EGRESS CONSTRAINT — read before wiring this anywhere new. The row
    text is reproduced VERBATIM, which is correct for the surfaces this source
    reaches today (the follow-up cockpit, ``follow_up_list``) because those are
    internal and the same text already sits in ``session_ledger``. It is NOT
    safe to assume for an egress surface: a real row on this install carries
    plaintext credentials a session pasted into its ledger, and the morning
    report renders ``content[:200]`` into a TELEGRAM message. ``ledger_escalation``
    is deliberately absent from that report. Do NOT add it — nor to email, nor to
    any other external channel — without a redaction pass on this string first.
    """
    row_id = row["id"]
    session_id = row["session_id"]
    created = row.get("created_at") or "unknown"
    quiet_phrase = f"quiet {quiet_days:.0f}d" if quiet_days is not None else "quiet (age unknown)"
    charter = sessions_dir / session_id / "charter.md"
    return (
        f"[STALE LEDGER ITEM — undisposed {untouched_days:.0f}d · "
        f"session {session_id[:8]} · row {row_id[:8]}]\n\n"
        f"{row['text']}\n\n"
        f"This row has been open since {created} and its session is "
        f"{quiet_phrase} ({liveness_source}), so nobody is coming back to "
        f"dispose of it. It needs a decision, not necessarily work:\n\n"
        f"  the work landed —\n"
        f"    session_ledger_update('{row_id}', status='done', "
        f"evidence='<what closed it>')\n"
        f"  it was absorbed by other work —\n"
        f"    session_ledger_update('{row_id}', status='absorbed', "
        f"evidence='<what absorbed it>')\n"
        f"  it no longer needs doing —\n"
        f"    session_ledger_update('{row_id}', status='dropped', "
        f"evidence='<why>')\n\n"
        f"WHICH LANE? This sweep cannot tell a dropped user errand from a stale "
        f"internal dev item — both shapes are really in the ledger — so this "
        f"follow-up ships unclassified. Set domain=user_world if it is yours, "
        f"domain=internal if it is Genesis's.\n\n"
        f"Full charter: {charter}\n"
        f"Disposing the row auto-completes this follow-up at the next sweep."
    )


async def _reconcile(db: aiosqlite.Connection) -> tuple[int, bool]:
    """Complete escalations whose ledger row has since been disposed.

    Returns ``(reconciled, failed)``. The second element exists because a
    reconcile that could not run and a reconcile with nothing to do both return
    zero, and the caller records job SUCCESS either way — so a permanently dead
    reverse sync (escalations piling up pending for rows disposed weeks ago)
    would look identical to a healthy quiet one. This module is otherwise
    careful to be loud (``ledger_stale_open`` raises rather than truncating; the
    deferred count is returned rather than inferred), and a silent half-run
    contradicts that posture.

    Direction matters: the dedup key is a one-way hash, so a follow-up cannot
    name its own row. We therefore map from the LEDGER side, via ``ledger_all``
    — the module's existing complete-read seam, already used by
    ``repo_pulse_worker`` for exactly this "match something against the whole
    ledger" purpose. It is a keyset walk that RAISES past a tripwire rather than
    truncating, and the live table is 181 rows.
    """
    try:
        rows = await ledger_all(db)
    except Exception:
        logger.exception("ledger escalation: ledger read failed — skipping reconcile")
        return 0, True

    disposed_keys = {
        escalation_dedup_key(r["id"]): r
        for r in rows
        if r.get("status") in _TERMINAL_LEDGER_STATUSES
    }
    if not disposed_keys:
        return 0, False

    # Every NON-TERMINAL escalation, not just 'pending'. `follow_ups.status`
    # admits six values; a human who picks an escalation up moves it to
    # 'in_progress', and 'scheduled'/'blocked' are reachable too. Reconciling
    # only 'pending' would strand exactly the escalations someone engaged with —
    # and since `exists_by_dedup_key` spans all statuses, nothing would ever
    # re-create them, so the row would stay open with no live follow-up.
    # FILTER SERVER-SIDE, THEN BOUND — one status at a time, each with its own
    # limit. Fetching by source and filtering in Python put the cap BEFORE the
    # filter: `get_by_source` orders created_at DESC, so once completed
    # escalations outnumber the bound they fill it entirely and the older PENDING
    # ones — the only rows reconcile exists to close — are never seen.
    #
    # This is the SAME cap-before-filter shape as the forward pass's starvation
    # bug, recreated forty lines away while fixing it. The lesson generalises:
    # where a cap and a filter both apply, the filter goes first — and one
    # instance of a class is never the population.
    escalations: list[dict] = []
    for status in _NON_TERMINAL_FOLLOW_UP_STATUSES:
        escalations.extend(
            await fu_crud.get_by_source(
                db, ESCALATION_SOURCE, status=status, limit=_RECONCILE_READ_LIMIT
            )
        )
    if len(escalations) >= _RECONCILE_READ_LIMIT:
        logger.warning(
            "ledger escalation: reconcile read hit its %d-row bound; "
            "get_by_source orders created_at DESC, so the OLDEST escalations "
            "were not examined this run and may stay pending after their row "
            "was disposed",
            _RECONCILE_READ_LIMIT,
        )

    reconciled = 0
    for follow_up in escalations:
        row = disposed_keys.get(follow_up.get("dedup_key") or "")
        if row is None:
            continue
        try:
            await fu_crud.update_status(
                db,
                follow_up["id"],
                "completed",
                resolution_notes=(
                    f"ledger row {row['id'][:8]} disposed: {row['status']}"
                    + (f" — {row['evidence']}" if row.get("evidence") else "")
                ),
            )
            reconciled += 1
        except Exception:
            logger.exception(
                "ledger escalation: failed to complete follow-up %s",
                follow_up.get("id"),
            )
    if reconciled:
        logger.info(
            "Ledger escalation: completed %d follow-up(s) whose row was disposed",
            reconciled,
        )
    return reconciled, False


async def run_sweep(
    db: aiosqlite.Connection,
    *,
    now: datetime,
    sessions_dir: Path,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Reconcile disposed escalations, then escalate newly-undisposable rows.

    Returns counts plus the ids it DEFERRED, so a capped run reports what it did
    not reach instead of leaving that to be inferred from a total.
    """
    cfg = cfg if cfg is not None else load_config()
    stale_days = knob_int(cfg, "stale_days")
    quiet_days = knob_int(cfg, "quiet_days")
    max_per_run = knob_int(cfg, "max_per_run")
    allowed_added_by = escalate_added_by(cfg)

    # Normalise to UTC. Both sides of the staleness comparison are ISO strings
    # compared LEXICOGRAPHICALLY, and every writer stamps UTC
    # (`session_charters._now_iso`), so a non-UTC `now` would silently compare
    # local wall-clock against UTC wall-clock and be wrong by the offset with no
    # error. The one live call site already passes UTC; this makes it true for
    # every caller on every install, whatever `user_timezone()` is set to.
    now = now.astimezone(UTC)

    reconciled, reconcile_failed = await _reconcile(db)
    result: dict[str, Any] = {
        "reconciled": reconciled,
        "reconcile_failed": reconcile_failed,
        "created": 0,
        "deferred": 0,
        "deferred_ids": [],
        "skipped_active": 0,
    }

    stale_cutoff = (now - timedelta(days=stale_days)).isoformat()
    quiet_cutoff = now - timedelta(days=quiet_days)

    candidates = await ledger_stale_open(
        db, untouched_before=stale_cutoff, added_by=allowed_added_by
    )

    eligible: list[tuple[dict, float, float | None, str]] = []
    for row in candidates:
        touched = _last_touched(row)
        untouched_days = _age_days(touched, now)
        if untouched_days is None:
            # Both timestamps unparseable: we cannot say the row is stale, so we
            # do not act on it. Loud, because a row with no readable created_at
            # is itself a defect somewhere upstream.
            logger.warning(
                "ledger escalation: row %s has no parseable timestamp — skipping",
                row.get("id"),
            )
            continue
        last_active, liveness_source = _session_last_active(
            sessions_dir, row["session_id"], fallback=touched
        )
        if last_active is not None and last_active > quiet_cutoff:
            # The owning session is alive. The row is ITS to dispose, and
            # escalating would take the decision from the one party still able
            # to make it.
            result["skipped_active"] += 1
            continue
        eligible.append((row, untouched_days, _age_days(last_active, now), liveness_source))

    # FILTER, THEN SLICE — the order is load-bearing and getting it backwards
    # starves the backlog permanently. The sweep deliberately never writes
    # session_ledger, so an ALREADY-ESCALATED row keeps its open status and its
    # old timestamp: it re-qualifies on every run and, under the oldest-first
    # ordering, sits at the FRONT of `eligible` forever. Slicing first therefore
    # spends the whole per-run budget on rows that are then skipped by the dedup
    # check, and rows past the cap are never reached again.
    # MEASURED on the pre-fix code (8 rows, cap 3): created 3, 0, 0, 0, 0 across
    # five runs — while logging "they escalate on later runs" every time. The
    # class is general: any recurring sweep that caps per run AND dedups by a
    # stable key must filter before it slices.
    fresh: list[tuple[dict, float, float | None, str]] = []
    for candidate in eligible:
        try:
            if await fu_crud.exists_by_dedup_key(db, escalation_dedup_key(candidate[0]["id"])):
                continue
        except Exception:
            # Cannot tell whether it is already escalated. Skip it rather than
            # risk a duplicate; the unique index would refuse one anyway, and
            # the row re-qualifies next run.
            logger.exception(
                "ledger escalation: dedup precheck failed for row %s",
                candidate[0].get("id"),
            )
            continue
        fresh.append(candidate)

    for row, untouched_days, quiet_age, liveness_source in fresh[:max_per_run]:
        dedup_key = escalation_dedup_key(row["id"])
        try:
            await fu_crud.create(
                db,
                content=_render_content(
                    row,
                    now=now,
                    untouched_days=untouched_days,
                    quiet_days=quiet_age,
                    liveness_source=liveness_source,
                    sessions_dir=sessions_dir,
                ),
                source=ESCALATION_SOURCE,
                strategy="user_input_needed",
                reason="ledger item never disposed — no decision recorded",
                source_session=row["session_id"],
                priority=priority(cfg),
                kind="follow_up",
                # UNCLASSIFIED on purpose. The sweep genuinely cannot tell a
                # dropped user errand from a stale internal dev item — measured
                # 2026-09-06, 13 of 15 qualifying rows were internal and 2 were
                # user-world errands the owner had asked for. `create` documents
                # None as "not yet classified"; guessing would mislabel exactly
                # the class this exists to stop losing, so the follow-up asks.
                domain=None,
                dedup_key=dedup_key,
            )
            result["created"] += 1
        except aiosqlite.IntegrityError:
            # The dedup_key partial unique index fired: a concurrent sweep
            # created this escalation between our precheck and our INSERT. That
            # is the index doing its job, not a failure — the row IS escalated,
            # just not by us. Logged at debug, because an ERROR traceback here
            # would make a working race look like a broken sweep. (Found by
            # mutation: with the precheck removed this path carries EVERY
            # duplicate, and it was previously indistinguishable in the logs
            # from a genuine write failure.)
            logger.debug(
                "ledger escalation: row %s already escalated by a concurrent "
                "sweep — leaving it to that one",
                row.get("id"),
            )
        except Exception:
            # A real failure. One bad row must not abort the rest of the run.
            logger.exception("ledger escalation: failed to escalate row %s", row.get("id"))

    deferred = fresh[max_per_run:]
    if deferred:
        result["deferred"] = len(deferred)
        result["deferred_ids"] = [r["id"] for r, _, _, _ in deferred]
        logger.warning(
            "Ledger escalation: %d eligible row(s) deferred past the %d-per-run "
            "cap; they escalate on later runs (oldest first): %s",
            len(deferred),
            max_per_run,
            ", ".join(r["id"][:8] for r, _, _, _ in deferred),
        )
    if result["created"]:
        logger.info(
            "Ledger escalation: created %d follow-up(s) for undisposed rows",
            result["created"],
        )
    return result
