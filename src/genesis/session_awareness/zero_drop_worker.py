"""Detached zero-drop detector — the sweep loop.

Spawned fire-and-forget at SessionStart boundaries (and once a day by
disk-hygiene, so a box that starts no sessions still sweeps). It enumerates
local branches, their remote presence, full PR history and every worktree's
uncommitted state, classifies what is STRANDED, and reconciles that against
the findings store.

Why a SIBLING worker and not a repo-pulse lane: the pulse worker holds
ledger-absorb authority (``ledger_update``/follow-up completion) on live
connections. Mounting a detector inside it would put a read-only observer
inside a process that can rewrite the work stores it observes. This one can
write exactly one table plus its own state files, and that is the whole of its
authority.

The never-do list, in force by construction — the detector NEVER pushes,
fetches, opens/closes/reopens a PR, deletes a branch, unclaims work, or writes
to the ledger, follow-ups, tasks or observations-as-work. Escalation
(``consecutive_runs >= k``) sets a visibility flag and nothing else.

Discipline (repo_pulse_worker / ledger_worker lineage):

- Own short-lived DB connections; the server's SerializedConnection is never
  touched. Failures are recorded, never raised — nothing is attached to read a
  detached process's exit status.
- Global flock (``detector.lock``): the loser exits immediately.
- Debounce under the lock; a debounced worker exits silently.
- **A degraded leg freezes its classes.** If the PR listing fails or caps, or
  ls-remote fails, the branch classes are not reconciled AT ALL that run — not
  partially. Only a class swept COMPLETELY may resolve findings, because
  resolving a branch the sweep never looked at is how a detector manufactures a
  clean board, which is the exact failure it exists to prevent. A degraded run
  is also not a counted run: recurrence and escalation stand still.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from genesis.db.crud import zero_drop as zd_crud
from genesis.env import genesis_db_path, genesis_home, repo_root
from genesis.session_awareness.repo_pulse_gh import list_all_prs
from genesis.session_awareness.zero_drop import (
    CLASS_DIRTY,
    CLASS_PUSHED_NO_PR,
    CLASS_UNPUSHED,
    classify_branches,
    classify_worktrees,
    worktree_identity,
)
from genesis.session_awareness.zero_drop_config import (
    alert_priority,
    effective_mode,
    knob_int,
    load_config,
)
from genesis.session_awareness.zero_drop_git import (
    list_local_branches,
    list_remote_branch_names,
    list_worktrees,
    worktree_status,
)

LOCK_FILENAME = "detector.lock"
LAST_RUN_FILENAME = "last_run.json"
ALERT_SOURCE = "zero_drop_detector"
BLIND_SOURCE = "zero_drop_detector_blind"
ALERT_TYPE = "infrastructure_alert"
HEARTBEAT_SUBSYSTEM = "zero_drop"

BRANCH_CLASSES = (CLASS_UNPUSHED, CLASS_PUSHED_NO_PR)
ALL_CLASSES = (*BRANCH_CLASSES, CLASS_DIRTY)

logger = logging.getLogger(__name__)


def _zero_drop_root() -> Path:
    # genesis_home() honors GENESIS_HOME so a relocated install keeps its lock
    # and last-run state together, and every reader resolves the same directory.
    return genesis_home() / "zero_drop"


def last_run_path() -> Path:
    return _zero_drop_root() / LAST_RUN_FILENAME


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _atomic_write_json(path: Path, data: dict) -> None:
    """Replace *path* with *data*, or leave the previous contents intact.

    ``os.replace`` is the atomic half; the ``fsync`` is the durable half (a
    rename can land before the bytes on a crash, leaving a zero-length record
    that ``read_last_run`` would report as "the detector never ran"). The temp
    file is removed on any failure so a repeatedly-failing write cannot litter
    the state directory.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, default=str).encode()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:  # closes fd even on error
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, str(path))
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read_last_run() -> dict:
    """The previous run's stage accounting, or ``{}`` if there is none.

    The reader's contract is that an EMPTY result means "the detector has not
    run", never "nothing is stranded" — every surface must render the
    ``computed_at`` age beside any zero.
    """
    try:
        data = json.loads(last_run_path().read_text())
        if isinstance(data, dict):
            return data
    except FileNotFoundError:
        pass  # never run here — the honest empty state, not a fault
    except Exception:
        logger.warning("zero_drop last_run.json unreadable", exc_info=True)
    return {}


def _within_minutes(ts: str | None, minutes: int) -> bool:
    if not ts:
        return False
    try:
        then = datetime.fromisoformat(ts)
    except ValueError:
        return False
    return (datetime.now(UTC) - then).total_seconds() < minutes * 60


DEFAULT_BASE_REF = "origin/main"

# One GitHub round-trip. MEASURED 2026-09-05: the full 1651-PR listing returned
# in ~6s, so this is 5x headroom; the failure mode is a hung call sitting on the
# detector flock (the raw-subprocess carve-out in the timeout policy).
_GH_TIMEOUT_S = 60


def _gh_runner(repo_path: str):
    """A gh runner whose cwd is the repository being SWEPT.

    ``repo_pulse_gh``'s default runner pins cwd to ``genesis.env.repo_root()``,
    which is right for the pulse and wrong here the moment ``--repo-path``
    points elsewhere: the branches would come from one repository and the PR
    history from another, and every branch would read "no PR". Binding the
    runner to the same path keeps the two halves of the join talking about the
    same repo.
    """

    async def _run(argv: list[str]) -> tuple[int, str, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=repo_path,
            )
        except Exception as exc:
            return 127, "", f"gh spawn failed: {exc}"
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_GH_TIMEOUT_S)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return 124, "", f"gh call timed out after {_GH_TIMEOUT_S}s"
        return (
            proc.returncode or 0,
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"),
        )

    return _run


async def _resolve_base_ref(root: str, runner=None) -> str | None:
    """The ref every branch's ahead-count is measured against, or None.

    Read LOCALLY from ``refs/remotes/origin/HEAD`` (no network) so a fork whose
    default branch is not ``main`` measures against its own.

    Returns None on failure rather than the fallback itself: a wrong base
    inflates every ahead-count, so the caller has to be able to SAY it guessed
    — and it cannot, if "resolved to origin/main" and "fell back to
    origin/main" arrive as the same string. The first version of this returned
    the fallback, so every healthy run on a main-branch repo filed a fallback
    note that had not happened.
    """
    from genesis.session_awareness.zero_drop_git import REF_SWEEP_TIMEOUT_S, default_runner

    run = runner or default_runner
    rc, out, _err = await run(
        ["git", "-C", root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        REF_SWEEP_TIMEOUT_S,
    )
    name = out.strip()
    return name if rc == 0 and name else None


async def _observe_worktrees(root: str, *, runner=None) -> dict:
    """Per-worktree dirty state + the newest mtime among the dirty paths.

    Sequential by design. MEASURED 2026-09-05: 161 worktrees in 12.3s with 0
    errors — irrelevant for a detached process on a 60-minute debounce, and
    concurrency here would buy ~9s at the cost of a new failure mode (fd
    pressure, scheduling) on a swapless box.

    Returns ``{"observations", "errors", "held", "prunable"}``. The two kinds
    of "not observed" are kept apart deliberately:

    - **prunable** — the worktree's directory is gone and only the
      registration survives (MEASURED on git 2.43). That is not a failed read,
      it is an absent worktree, and an absent directory holds no uncommitted
      work. Skipped and counted.
    - **unreadable** — the status call failed for any other reason (permission,
      a stalled mount). That worktree's identity is QUARANTINED into ``held``
      so the reconciler leaves its finding exactly as it is. The class still
      reconciles everything it DID read: freezing all 161 worktrees because one
      was unreadable is a self-inflicted blind spot, and on this install the
      margin for that was a single worktree.

    Mtime caveat, stated because it decides a fail direction: git reports an
    untracked DIRECTORY as one entry (``?? dir/``), and a directory's mtime
    does not move when a file inside it is edited. So active work inside a
    long-untracked tree can read as old — which FLAGS it. That is the safe
    direction for a detector; the unsafe one would be reading it as new and
    dropping the finding.
    """
    listing = await list_worktrees(root, runner=runner)
    if "error" in listing:
        # The enumeration itself failed — there is no per-item granularity to
        # fall back to, so the whole class freezes.
        return {"observations": [], "errors": [str(listing["error"])], "held": None, "prunable": 0}

    observations: list[dict] = []
    errors: list[str] = []
    held: set[str] = set()
    prunable = 0
    for wt in listing["worktrees"]:
        if wt.get("prunable"):
            prunable += 1
            continue
        status = await worktree_status(wt["path"], runner=runner)
        if "error" in status:
            errors.append(f"{wt['path']}: {status['error']}")
            held.add(worktree_identity(wt))
            continue
        entries = status["entries"]
        newest = None
        for _xy, rel in entries:
            try:
                mtime = datetime.fromtimestamp(os.stat(os.path.join(wt["path"], rel)).st_mtime, UTC)
            except OSError:
                continue  # a deleted path has no mtime; other entries still date it
            if newest is None or mtime > newest:
                newest = mtime
        observations.append({**wt, "entries": entries, "newest_mtime": newest})
    return {
        "observations": observations,
        "errors": errors,
        "held": held,
        "prunable": prunable,
    }


async def _emit_heartbeat(db, *, detail: str) -> None:
    """Durable liveness pulse so a DEAD detector is visible as dead.

    A detector that stops running answers "what fell through the cracks?" with
    a stale zero — the failure mode with no natural symptom. ``subsystem_stale``
    reads these rows (``events`` is the durable half of the heartbeat probe; the
    in-memory ring is only a freshness bonus an out-of-process worker cannot
    reach), and ``zero_drop`` is registered in ``HEARTBEAT_EXPECTED`` and in
    ``_NO_BOOT_PULSE_SUBSYSTEMS`` — a detached worker emits no bootstrap pulse,
    so without that membership a fresh boot would false-flag ``never_started``.
    """
    try:
        from genesis.db.crud import events as events_crud

        await events_crud.insert(
            db,
            subsystem=HEARTBEAT_SUBSYSTEM,
            severity="info",
            event_type="heartbeat",
            message=f"zero_drop sweep: {detail}",
        )
        await db.commit()
    except Exception:
        # A missing pulse degrades the health surface, never the sweep — but a
        # silent one turns "the detector died" into "the detector is fine".
        logger.warning("zero_drop heartbeat write failed", exc_info=True)


async def _maintain_alert(
    db, *, cfg: dict, findings: list[dict], total: int, coverage: str
) -> str:
    """Keep exactly ONE observation describing the current board (or none).

    Clone of the follow-up watchdog's alert shape: an offender-set content hash
    so a CHANGED set supersedes rather than dedupes against the old text, and
    an auto-resolve when the board comes clean. ``max_listed`` caps the NAMES
    rendered inline, never the count — the total is always stated beside them,
    so the cap is a display selection with a denominator, not a silent trim.

    ``coverage`` names which classes this run actually swept. The count comes
    from the whole store, so a run that froze a class is reporting numbers it
    did not measure this time; saying so is the difference between a count and
    a claim.

    Order matters: CREATE first, then supersede everything except the new hash.
    The reverse (supersede, then create) leaves a window in which every prior
    alert is resolved and the replacement does not exist yet — and if the
    create then fails, that window is where the board stays until the next
    sweep an hour later: findings in the store, nothing on any surface.
    """
    import hashlib

    from genesis.db.crud import observations

    now_iso = _now()
    if not findings:
        try:
            await observations.resolve_by_source_and_type(
                db,
                source=ALERT_SOURCE,
                type=ALERT_TYPE,
                resolved_at=now_iso,
                resolution_notes="zero-drop board is clean",
            )
        except Exception:
            logger.warning("zero_drop alert resolve failed", exc_info=True)
            return "resolve_failed"
        return "resolved"

    offender_key = ",".join(sorted(f"{f['class']}:{f['branch']}" for f in findings))
    content_hash = hashlib.sha256(f"zero_drop:{offender_key}".encode()).hexdigest()
    try:
        max_listed = knob_int(cfg, "max_listed")
        listed = findings[:max_listed]
        rows = " | ".join(
            f"{f['branch']} · {f['class']}"
            + (f" · +{f['ahead_count']}" if f.get("ahead_count") else "")
            for f in listed
        )
        more = len(findings) - len(listed)
        escalated = sum(1 for f in findings if f.get("escalated"))
        content = (
            f"{total} stranded-work finding(s) open [{coverage}] ({escalated} escalated "
            f"past the recurrence threshold): work that exists but is in no pipeline. "
            f"Disposition each one — land it, or acknowledge it with a reason via "
            f"zero_drop_ack (the ack expires the moment the branch moves). A PR comment "
            f"or a plan-file bullet is not a disposition. [{rows}"
            + (f" | (+{more} more of {total})]" if more else "]")
        )
        created = await observations.create(
            db,
            id=str(uuid.uuid4()),
            source=ALERT_SOURCE,
            type=ALERT_TYPE,
            content=content,
            priority=alert_priority(cfg),
            created_at=now_iso,
            content_hash=content_hash,
            skip_if_duplicate=True,
        )
        await observations.supersede_except_hash(
            db,
            source=ALERT_SOURCE,
            type=ALERT_TYPE,
            keep_content_hash=content_hash,
            resolved_at=now_iso,
            resolution_notes="superseded by a new zero-drop board state",
        )
    except Exception:
        logger.warning("zero_drop alert write failed", exc_info=True)
        return "alert_failed"
    return "created" if created else "unchanged"


async def _maintain_blind_alert(db, *, degraded: dict) -> str:
    """Announce a BLIND detector — separately from what it found.

    This is the failure the rest of the design guards against arriving through
    the other door. A dead detector is caught by the heartbeat. A LIVE detector
    with a permanently failing leg is not: it keeps pulsing, keeps writing a
    run record, and keeps the board exactly as it was — so an expired ``gh``
    token freezes the branch classes forever while every health surface reads
    green and the last alert says whatever it said last week. That is the
    stale, confident, wrong zero this subsystem exists to prevent, reached
    without anything ever reporting an error.

    Emitted in ``observe`` mode as well as ``alert``: the mode lever governs
    egress about FINDINGS, and a broken instrument is not a finding. Hashed on
    the set of blind legs, so recovery resolves it and a change of legs
    supersedes rather than dedupes.
    """
    import hashlib

    from genesis.db.crud import observations

    now_iso = _now()
    if not degraded:
        try:
            await observations.resolve_by_source_and_type(
                db,
                source=BLIND_SOURCE,
                type=ALERT_TYPE,
                resolved_at=now_iso,
                resolution_notes="all zero-drop legs are reading again",
            )
        except Exception:
            logger.warning("zero_drop blind-alert resolve failed", exc_info=True)
            return "resolve_failed"
        return "resolved"

    legs = ",".join(sorted(degraded))
    content_hash = hashlib.sha256(f"zero_drop_blind:{legs}".encode()).hexdigest()
    try:
        content = (
            f"The zero-drop detector is BLIND on: {legs}. Findings in those classes are "
            f"FROZEN — nothing new is detected there and nothing already found is "
            f"resolved, so the board is not a measurement until this clears. "
            f"Cause: {json.dumps(degraded, default=str)[:400]}"
        )
        created = await observations.create(
            db,
            id=str(uuid.uuid4()),
            source=BLIND_SOURCE,
            type=ALERT_TYPE,
            content=content,
            priority="high",
            created_at=now_iso,
            content_hash=content_hash,
            skip_if_duplicate=True,
        )
        await observations.supersede_except_hash(
            db,
            source=BLIND_SOURCE,
            type=ALERT_TYPE,
            keep_content_hash=content_hash,
            resolved_at=now_iso,
            resolution_notes="superseded by a new zero-drop blindness state",
        )
    except Exception:
        logger.warning("zero_drop blind-alert write failed", exc_info=True)
        return "alert_failed"
    return "created" if created else "unchanged"


async def run_zero_drop_worker(
    *,
    trigger: str = "manual",
    force: bool = False,
    db_path: Path | str | None = None,
    repo_path: str | None = None,
) -> dict:
    """One detector sweep. Returns the outcome dict, never raises."""
    try:
        return await _run(
            trigger=trigger,
            force=force,
            db_path=db_path or genesis_db_path(),
            repo_path=repo_path or str(repo_root()),
        )
    except Exception as exc:  # noqa: BLE001 — detached: report, never raise
        return {"status": "failed", "detail": f"{type(exc).__name__}: {exc}"}


async def _run(*, trigger: str, force: bool, db_path: Path | str, repo_path: str) -> dict:
    if os.environ.get("GENESIS_ZERO_DROP_DISABLED") == "1":
        return {"status": "skipped_disabled"}
    mode = effective_mode()
    if mode == "off":
        return {"status": "skipped_off"}

    root = _zero_drop_root()
    root.mkdir(parents=True, exist_ok=True)
    lock_fh = (root / LOCK_FILENAME).open("w")
    try:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return {"status": "lock_busy"}
        return await _run_locked(
            trigger=trigger,
            force=force,
            db_path=db_path,
            repo_path=repo_path,
            mode=mode,
        )
    finally:
        lock_fh.close()


async def _run_locked(
    *, trigger: str, force: bool, db_path: Path | str, repo_path: str, mode: str
) -> dict:
    cfg = load_config()
    prior = read_last_run()
    if not force and _within_minutes(
        prior.get("computed_at"), knob_int(cfg, "min_interval_minutes")
    ):
        return {"status": "debounced"}

    run_id = uuid.uuid4().hex
    started = time.monotonic()
    now_dt = datetime.now(UTC)
    now_iso = now_dt.isoformat()
    degraded: dict[str, str] = {}
    # Stage counts are NAMESPACED PER LEG, never merged into one flat dict.
    # The two legs share key names (both have a `too_young`), so a flat merge
    # silently overwrote the branch count with the worktree one and the
    # accounting stopped summing to its denominator — MEASURED on the live
    # acceptance replay 2026-09-05: 202 counted against 209 refs, seven
    # branches suppressed with no stage to account for them. A suppression
    # audit that does not add up is the failure this whole record exists to
    # make impossible.
    stages: dict[str, dict[str, int]] = {}
    notes: list[str] = []

    # ── Branch legs: for-each-ref + ls-remote + full PR history ──────────────
    resolved_base = await _resolve_base_ref(repo_path)
    base_ref = resolved_base or DEFAULT_BASE_REF
    if resolved_base is None:
        notes.append(f"base_ref_unresolved_using={DEFAULT_BASE_REF}")
    branch_findings: dict[str, list[dict]] | None = None
    branch_held: set[str] = set()
    local = await list_local_branches(repo_path, base=base_ref)
    remote = await list_remote_branch_names(repo_path)
    # The PR listing MUST resolve its slug from the same repository the branches
    # came from. gh's default runner pins cwd to genesis.env.repo_root(), so a
    # --repo-path pointing anywhere else would join this repo's branches against
    # a DIFFERENT repo's PR history — every branch reads "no PR" and the entire
    # board becomes false positives, silently and plausibly.
    prs = await list_all_prs(limit=knob_int(cfg, "max_prs"), runner=_gh_runner(repo_path))
    if "error" in local:
        degraded["branches"] = f"for-each-ref: {local['error']}"
    elif "error" in remote:
        degraded["branches"] = f"ls-remote: {remote['error']}"
    elif "error" in prs:
        degraded["branches"] = f"pr history: {prs['error']}"
    elif prs.get("limit_hit"):
        # A capped history turns a merged branch into a false "stranded"
        # finding, so the classes FREEZE rather than run on a partial join.
        degraded["branches"] = f"pr history capped at {knob_int(cfg, 'max_prs')} (limit_hit)"
    else:
        classified = classify_branches(
            local["branches"],
            remote_names=remote["names"],
            prs=prs["prs"],
            now=now_dt,
            min_age_hours=knob_int(cfg, "branch_min_age_hours"),
        )
        branch_findings = classified["findings"]
        branch_held = classified["held"]
        stages["branches"] = {
            **classified["stages"],
            "prs_scanned": len(prs["prs"]),
            "held_by_age_gate": len(branch_held),
        }

    # ── Worktree leg: independent of the branch legs ─────────────────────────
    dirty_findings: list[dict] | None = None
    dirty_held: set[str] = set()
    observed = await _observe_worktrees(repo_path)
    if observed["held"] is None:
        # The enumeration itself failed — no per-item granularity to fall back
        # on, so the whole class freezes.
        degraded["worktrees"] = "; ".join(observed["errors"])[:300]
    else:
        classified_wt = classify_worktrees(
            observed["observations"],
            now=now_dt,
            min_age_hours=knob_int(cfg, "worktree_min_age_hours"),
        )
        dirty_findings = classified_wt["findings"]
        # Age-gated AND unreadable worktrees are both held: the class still
        # reconciles what it read, and neither kind can resolve a finding.
        dirty_held = classified_wt["held"] | observed["held"]
        stages["worktrees"] = {
            **classified_wt["stages"],
            "prunable_skipped": observed["prunable"],
            "unreadable": len(observed["errors"]),
            "held_total": len(dirty_held),
        }
        if observed["errors"]:
            degraded["worktrees"] = (
                f"{len(observed['errors'])} of "
                f"{stages['worktrees']['worktrees_total'] + len(observed['errors'])} "
                f"worktrees unreadable (their findings are held, the rest reconciled): "
                + "; ".join(observed["errors"])[:200]
            )

    # ── Reconcile only the classes whose sweep COMPLETED ─────────────────────
    escalation_k = knob_int(cfg, "escalation_k")
    applied: dict[str, dict] = {}
    open_rows: list[dict] = []
    counts: dict[str, int] = {}
    alert_state = "skipped"
    blind_state = "skipped"
    from genesis.db.connection import get_raw_db

    async def _apply(db, cls: str, present: list[dict], held: set[str]) -> None:
        """Reconcile ONE class, isolated. A class that raises degrades ITSELF.

        Without this isolation a single failure — an unexpected DB error, a
        constraint nobody anticipated — propagated out of the sweep and took
        the other two classes, the heartbeat and the run record with it, and
        the only symptom was an overdue pulse two days later.
        """
        try:
            applied[cls] = await zd_crud.apply_sweep(
                db,
                class_=cls,
                present=present,
                run_id=run_id,
                now=now_iso,
                escalation_k=escalation_k,
                held=held,
            )
        except Exception as exc:  # noqa: BLE001 — one class, not the sweep
            logger.warning("zero_drop reconcile failed for class %s", cls, exc_info=True)
            degraded[cls] = f"reconcile failed: {type(exc).__name__}: {exc}"[:200]

    async with get_raw_db(str(db_path)) as db:
        if branch_findings is not None:
            for cls in BRANCH_CLASSES:
                await _apply(db, cls, branch_findings[cls], branch_held)
        if dirty_findings is not None:
            await _apply(db, CLASS_DIRTY, dirty_findings, dirty_held)

        counts = await zd_crud.counts_by_status(db)
        open_rows = await zd_crud.list_findings(db, statuses=("open",))
        # The counts come from the WHOLE store, so a run that froze a class is
        # reporting rows it did not measure this time. Every surface that shows
        # the number shows what the number covers.
        frozen = [c for c in ALL_CLASSES if c not in applied]
        coverage = "all classes swept" if not frozen else f"FROZEN: {','.join(frozen)}"
        if mode == "alert":
            alert_state = await _maintain_alert(
                db,
                cfg=cfg,
                findings=[
                    {
                        "class": r["class"],
                        "branch": r["branch"],
                        "ahead_count": r["ahead_count"],
                        "escalated": bool(r["escalated_at"]),
                    }
                    for r in open_rows
                ],
                total=len(open_rows),
                coverage=coverage,
            )
            if alert_state in ("alert_failed", "resolve_failed"):
                degraded["alert"] = alert_state
        # Blindness is reported in EVERY running mode: the lever governs egress
        # about findings, and a broken instrument is not a finding.
        blind_state = await _maintain_blind_alert(db, degraded=degraded)
        await _emit_heartbeat(
            db,
            detail=(
                f"trigger={trigger} open={len(open_rows)} coverage={coverage} "
                f"degraded={','.join(sorted(degraded)) or 'none'}"
            ),
        )

    duration_s = round(time.monotonic() - started, 2)
    status = "degraded" if degraded else "ok"
    record = {
        "version": 1,
        "run_id": run_id,
        "computed_at": now_iso,
        "trigger": trigger,
        "mode": mode,
        "status": status,
        "duration_s": duration_s,
        "base_ref": base_ref,
        "repo_path": repo_path,
        # Stage counts are the SUPPRESSION AUDIT: every ref is counted in
        # exactly one terminal stage, so what the run hid can be added up.
        "stages": stages,
        "degraded": degraded,
        "notes": notes,
        "applied": applied,
        "counts_by_status": counts,
        "open_findings": len(open_rows),
        # Which classes this run actually swept. `open_findings` counts the
        # whole store, so without this a reader cannot tell a measurement from
        # a leftover.
        "coverage": coverage,
        "frozen_classes": frozen,
        "alert": alert_state,
        "blind_alert": blind_state,
    }
    _atomic_write_json(last_run_path(), record)
    return {
        "status": status,
        **{k: record[k] for k in ("open_findings", "coverage", "degraded", "applied")},
    }
