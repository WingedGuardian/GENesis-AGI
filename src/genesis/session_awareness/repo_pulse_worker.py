"""Detached repo-pulse worker (session-manager PR-4a) — the run loop.

Spawned by the SessionStart hook (fire-and-forget; zero impact on the
hook's 5s budget — one gh round-trip alone exceeds it). Enumerates PRs
merged since the GLOBAL cursor, reconciles prior proposals against the
current ledger, matches new PRs against OPEN ledger rows across ALL
sessions (exact marker tier + fuzzy Haiku tier), and records annotations.
The ONLY live-ledger write is the exact tier's marker-triggered absorb in
``live`` mode — an UPDATE through ``session_charters.ledger_update`` with
PR evidence, reversible via ``session_ledger_update``. Fuzzy results are
proposals in every mode.

Discipline (ledger_worker lineage):

- Own short-lived DB connections; the server's SerializedConnection is
  never touched. All failures are recorded, never raised — nothing is
  attached to read a detached process's exit status.
- Worker-owned GLOBAL cursor (``~/.genesis/repo_pulse/cursor.json``):
  ``last_merged_at`` advances ONLY after an ok run's rows commit
  (monotonic max under the flock), so failed/timeout/pre-migration runs
  self-heal by re-covering their window — the annotation unique index and
  the per-pair re-absorb guard absorb the re-coverage. ``last_run_ts``
  updates on every RECORDED outcome and drives the debounce.
- Global flock (``pulse.lock``): the loser records ``lock_busy`` and
  exits, cursor-safe. Debounce is checked under the lock; a debounced
  worker exits silently with NO run row (debounced rows would swamp the
  run-table denominator).
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from genesis.db.crud import repo_pulse as pulse_crud
from genesis.db.crud.session_charters import ledger_all, ledger_update
from genesis.env import genesis_db_path
from genesis.session_awareness.headless import run_headless_json
from genesis.session_awareness.repo_pulse import (
    PROMPT_VERSION,
    PULSE_MODEL,
    PULSE_TIMEOUT_S,
    build_fuzzy_prompt,
    match_exact,
    parse_matches,
)
from genesis.session_awareness.repo_pulse_config import (
    effective_mode,
    knob_int,
    load_config,
)
from genesis.session_awareness.repo_pulse_gh import list_merged_prs

CURSOR_FILENAME = "cursor.json"
LOCK_FILENAME = "pulse.lock"

OPEN_STATUSES = ("open", "in_progress")
# A proposal still unresolved after this long is noise, not signal — the
# reconcile sweep retires it so precision math stays about decisions made.
STALE_PROPOSAL_DAYS = 30


def _pulse_root() -> Path:
    return Path.home() / ".genesis" / "repo_pulse"


def _now_dt() -> datetime:
    return datetime.now(UTC)


def _now() -> str:
    return _now_dt().isoformat()


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        os.write(fd, json.dumps(data).encode())
    finally:
        os.close(fd)
    os.replace(tmp, str(path))


def _read_cursor(root: Path) -> dict:
    try:
        data = json.loads((root / CURSOR_FILENAME).read_text())
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"last_merged_at": None, "last_run_ts": None, "runs": 0}


def _write_cursor(root: Path, prior: dict, *, merged_at: str | None) -> None:
    """Update the cursor after a RECORDED run.

    ``last_merged_at`` holds gh's own mergedAt strings (one consistent
    format, so lexicographic max == chronological max — never mix a
    locally-formatted timestamp in). It advances monotonically and only
    when ``merged_at`` is passed (ok runs); failed/no_new_prs runs update
    only ``last_run_ts`` (the debounce basis) + the run counter. ``prior``
    was read under the flock, so max() against it is race-free.
    """
    last = prior.get("last_merged_at")
    if merged_at is not None:
        last = max(str(last), merged_at) if last else merged_at
    _atomic_write_json(
        root / CURSOR_FILENAME,
        {
            "last_merged_at": last,
            "last_run_ts": _now(),
            "runs": int(prior.get("runs") or 0) + 1,
        },
    )


def _parse_iso(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except Exception:
        return None


def _within_minutes(ts: str | None, minutes: int) -> bool:
    if not ts:
        return False
    dt = _parse_iso(ts)
    return dt is not None and (_now_dt() - dt) < timedelta(minutes=minutes)


def _since_date(cursor_merged_at: str | None, lookback_days: int) -> str:
    """gh search date (YYYY-MM-DD, date-granular). With a cursor: its date —
    re-covering up to a day behind is by design (the exact ISO filter runs
    client-side). Without: lookback_days back, never all history."""
    if cursor_merged_at:
        dt = _parse_iso(cursor_merged_at)
        if dt is not None:
            return dt.date().isoformat()
    return (_now_dt() - timedelta(days=lookback_days)).date().isoformat()


def _evidence_names_pr(evidence: str | None, pr_number: int) -> bool:
    """Attribution guard: 'confirmed' requires the absorbing evidence to name
    the SAME PR — an item absorbed for a different PR must not inflate the
    fuzzy tier's precision."""
    return bool(evidence) and re.search(rf"#\s*{int(pr_number)}\b", evidence) is not None


async def _record_telemetry(db_path: Path | str, status: str, detail: str) -> bool:
    """Best-effort call_site_last_run row for the neural monitor."""
    try:
        from genesis.observability.call_site_recorder import record_last_run_detached

        return await record_last_run_detached(
            str(db_path),
            "repo_pulse",
            provider="cc",
            model_id=PULSE_MODEL,
            response_text=f"status={status}|{detail}"[:200],
            success=status in ("ok", "no_new_prs"),
        )
    except Exception:
        return False


async def _record_run(db_path: Path | str, **kwargs) -> bool:
    """One short-lived RW connection: run row + annotations, single commit.

    Returns False when the write demonstrably did not land (pre-migration
    tables, locked DB) — the caller must then leave the cursor alone.
    """
    import aiosqlite

    try:
        async with aiosqlite.connect(str(db_path), timeout=10) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            return await pulse_crud.record_run(db, **kwargs)
    except Exception:
        return False


async def _load_open_items(db_path: Path | str) -> list[dict]:
    """Open/in_progress ledger rows across ALL sessions, newest first.

    Best-effort: on any failure the run proceeds with zero items (recorded
    honestly as n_open_items=0 — the exact tier then has nothing to match,
    which is degraded, not wrong)."""
    import aiosqlite

    try:
        async with aiosqlite.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5) as db:
            db.row_factory = aiosqlite.Row
            rows = await ledger_all(db)
    except Exception:
        return []
    open_rows = [r for r in rows if r.get("status") in OPEN_STATUSES]
    open_rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return open_rows


async def _reconcile(db_path: Path | str, now_iso: str) -> dict[str, int]:
    """Sweep 'proposed' annotations against the current ledger state.

    Resolutions ARE the fuzzy tier's precision measurement:
    absorbed-with-same-PR-evidence → confirmed; absorbed otherwise →
    superseded (attribution guard); dropped → rejected; done → superseded
    (shipped, not attributed); still-open past STALE_PROPOSAL_DAYS →
    superseded (stale). Best-effort — a failed sweep never blocks the run.
    """
    import aiosqlite

    counts: dict[str, int] = {}
    try:
        async with aiosqlite.connect(str(db_path), timeout=10) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            db.row_factory = aiosqlite.Row
            proposed = await pulse_crud.list_annotations(db, status="proposed")
            if not proposed:
                return counts
            ledger = {r["id"]: r for r in await ledger_all(db)}
            for ann in proposed:
                resolution = _resolution_for(ann, ledger.get(ann["item_id"]), now_iso)
                if resolution is None:
                    continue
                status, ref = resolution
                if await pulse_crud.resolve_annotation(
                    db, ann["id"], status=status, resolved_at=now_iso, resolution_ref=ref
                ):
                    counts[status] = counts.get(status, 0) + 1
    except Exception:
        return counts
    return counts


def _resolution_for(ann: dict, item: dict | None, now_iso: str) -> tuple[str, str] | None:
    if item is None:
        return "superseded", "item_missing"
    status = item.get("status")
    if status == "absorbed":
        if _evidence_names_pr(item.get("evidence"), ann["pr_number"]):
            return "confirmed", f"absorbed with PR #{ann['pr_number']} evidence"
        return "superseded", "absorbed_via_other_evidence"
    if status == "dropped":
        return "rejected", "item_dropped"
    if status == "done":
        return "superseded", "done_not_attributed"
    # open / in_progress — leave live proposals alone until they go stale
    observed = _parse_iso(ann.get("observed_at") or "")
    now_dt = _parse_iso(now_iso)
    if observed and now_dt and (now_dt - observed) > timedelta(days=STALE_PROPOSAL_DAYS):
        return "superseded", f"stale_{STALE_PROPOSAL_DAYS}d"
    return None


def _annotation(tier: str, status: str, item: dict, pr: dict, **over) -> dict:
    ann = {
        "id": uuid.uuid4().hex,
        "observed_at": _now(),
        "tier": tier,
        "item_id": item["id"],
        "item_session_id": item.get("session_id"),
        "item_text": str(item.get("text") or "")[:300],
        "pr_number": pr["number"],
        "pr_title": str(pr.get("title") or "")[:200],
        "pr_merged_at": pr.get("mergedAt"),
        "confidence": None,
        "rationale": None,
        "status": status,
    }
    ann.update(over)
    return ann


async def run_pulse_worker(
    *,
    trigger: str = "manual",
    force: bool = False,
    claude_path: str = "claude",
    db_path: Path | str | None = None,
    lookback_days: int | None = None,
) -> dict:
    """One pulse run. Returns the outcome dict, never raises."""
    try:
        return await _run(
            trigger=trigger,
            force=force,
            claude_path=claude_path,
            db_path=db_path or genesis_db_path(),
            lookback_days=lookback_days,
        )
    except Exception as exc:  # noqa: BLE001 — detached: record, never raise
        return {"status": "failed", "detail": f"{type(exc).__name__}: {exc}"}


async def _run(
    *,
    trigger: str,
    force: bool,
    claude_path: str,
    db_path: Path | str,
    lookback_days: int | None,
) -> dict:
    if os.environ.get("GENESIS_REPO_PULSE_DISABLED") == "1":
        return {"status": "skipped_disabled"}
    mode = effective_mode()
    if mode == "off":
        # No run row, no lock, cursor untouched — indistinguishable from
        # the feature not existing (the hook-side kill switch is the
        # cheaper lever; this one catches settings flips).
        return {"status": "skipped_off"}

    root = _pulse_root()
    root.mkdir(parents=True, exist_ok=True)
    started_at = _now()
    t0 = time.monotonic()

    lock_fh = (root / LOCK_FILENAME).open("w")
    try:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            await _record_run(
                db_path,
                run_id=uuid.uuid4().hex,
                started_at=started_at,
                finished_at=_now(),
                trigger=trigger,
                repo=None,
                cursor_before=None,
                cursor_after=None,
                status="lock_busy",
                mode=mode,
            )
            return {"status": "lock_busy"}
        return await _run_locked(
            trigger=trigger,
            force=force,
            claude_path=claude_path,
            db_path=db_path,
            lookback_days=lookback_days,
            root=root,
            started_at=started_at,
            t0=t0,
            mode=mode,
        )
    finally:
        lock_fh.close()


async def _run_locked(
    *,
    trigger: str,
    force: bool,
    claude_path: str,
    db_path: Path | str,
    lookback_days: int | None,
    root: Path,
    started_at: str,
    t0: float,
    mode: str,
) -> dict:
    run_id = uuid.uuid4().hex
    cfg = load_config()
    cursor = _read_cursor(root)

    if not force and _within_minutes(
        cursor.get("last_run_ts"), knob_int(cfg, "min_interval_minutes")
    ):
        # Silent by design: a debounced boundary is the COMMON case (every
        # session start within the interval) — rows for it would swamp the
        # run-table denominator. --force bypasses for manual/E2E runs.
        return {"status": "debounced"}

    cursor_before = cursor.get("last_merged_at")
    now_iso = _now()
    detail_notes: list[str] = []

    reconciled = await _reconcile(db_path, now_iso)
    if reconciled:
        detail_notes.append(
            "reconciled " + ", ".join(f"{k}={v}" for k, v in sorted(reconciled.items()))
        )

    def _base_row(**over) -> dict:
        row = dict(
            run_id=run_id,
            started_at=started_at,
            finished_at=_now(),
            trigger=trigger,
            repo=None,
            cursor_before=cursor_before,
            cursor_after=cursor_before,
            mode=mode,
            latency_ms=int((time.monotonic() - t0) * 1000),
            detail="; ".join(detail_notes) or None,
        )
        row.update(over)
        return row

    since = _since_date(cursor_before, lookback_days or knob_int(cfg, "lookback_days"))
    listing = await list_merged_prs(since_date=since, limit=knob_int(cfg, "max_prs"))
    if "error" in listing:
        detail_notes.append(str(listing["error"])[:300])
        recorded = await _record_run(db_path, **_base_row(status="failed"))
        if recorded:
            _write_cursor(root, cursor, merged_at=None)
        await _record_telemetry(db_path, "failed", str(listing["error"])[:120])
        return {"status": "failed", "detail": listing["error"]}

    repo = listing["repo"]
    if listing["limit_hit"]:
        # LOUD: GitHub search can't sort by mergedAt ascending, so a capped
        # window may have dropped older PRs — visible on the run row, and
        # the un-advanced tail re-covers next run via the date-granular
        # search once the cursor reaches it.
        detail_notes.append("limit_hit")
    prs = [
        p for p in listing["prs"] if not cursor_before or str(p["mergedAt"]) > str(cursor_before)
    ]
    if not prs:
        recorded = await _record_run(db_path, **_base_row(status="no_new_prs", repo=repo, n_prs=0))
        if recorded:
            _write_cursor(root, cursor, merged_at=None)
        await _record_telemetry(db_path, "no_new_prs", f"repo={repo}")
        return {"status": "no_new_prs"}
    new_max_merged = max(str(p["mergedAt"]) for p in prs)

    open_items = await _load_open_items(db_path)

    annotations: list[dict] = []
    absorbed: list[str] = []
    exact_pairs: set[tuple[str, int]] = set()
    n_exact = 0
    exact_matches = match_exact(prs, open_items)
    if exact_matches:
        import aiosqlite

        async with aiosqlite.connect(str(db_path), timeout=10) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            db.row_factory = aiosqlite.Row
            for m in exact_matches:
                item, pr, via = m["item"], m["pr"], m["via"]
                if await pulse_crud.annotation_exists(db, "exact", item["id"], pr["number"]):
                    # Re-absorb guard: this (item, pr) pair was already acted
                    # on in a prior run — a deliberately reopened item must
                    # not be re-absorbed by the same PR on window re-coverage.
                    continue
                exact_pairs.add((item["id"], pr["number"]))
                n_exact += 1
                if via == "marker" and mode == "live":
                    evidence = (
                        f"PR #{pr['number']}: {str(pr.get('title') or '')[:120]} "
                        f"(merged {pr.get('mergedAt')}) [repo-pulse exact]"
                    )
                    ok = await ledger_update(db, item["id"], status="absorbed", evidence=evidence)
                    if ok:
                        absorbed.append(item["id"])
                        annotations.append(
                            _annotation("exact", "applied", item, pr, rationale="ledger-marker")
                        )
                        continue
                    annotations.append(
                        _annotation(
                            "exact",
                            "proposed",
                            item,
                            pr,
                            rationale="ledger-marker (ledger update missed)",
                        )
                    )
                elif via == "marker":
                    annotations.append(
                        _annotation(
                            "exact",
                            "proposed",
                            item,
                            pr,
                            rationale="ledger-marker (propose_only)",
                        )
                    )
                else:
                    annotations.append(
                        _annotation("exact", "proposed", item, pr, rationale="bare-hex")
                    )

    remaining = [i for i in open_items if i["id"] not in absorbed]
    n_fuzzy = 0
    fuzzy_ran = False
    if remaining:
        fuzzy_ran = True
        prompt, inc_items, inc_prs = build_fuzzy_prompt(
            remaining[: knob_int(cfg, "max_items")], prs
        )
        result = await run_headless_json(
            prompt, model=PULSE_MODEL, claude_path=claude_path, timeout_s=PULSE_TIMEOUT_S
        )
        matches = (
            parse_matches(result["stdout"], len(inc_items), len(inc_prs))
            if result["status"] == "ok"
            else None
        )
        if matches is None:
            # Exact-tier work is persisted (its ledger writes already
            # happened and are re-absorb-guarded); the cursor stays put so
            # the window re-covers and fuzzy retries next run.
            status = "timeout" if result["status"] == "timeout" else "failed"
            reason = result.get("reason") or ("unparseable" if result["status"] == "ok" else "")
            if reason:
                detail_notes.append(str(reason)[:200])
            recorded = await _record_run(
                db_path,
                **_base_row(
                    status=status,
                    repo=repo,
                    n_prs=len(prs),
                    n_open_items=len(open_items),
                    n_exact=n_exact,
                    prompt_version=PROMPT_VERSION,
                    model=PULSE_MODEL,
                ),
                annotations=annotations,
            )
            if recorded:
                _write_cursor(root, cursor, merged_at=None)
            await _record_telemetry(db_path, status, str(reason)[:120])
            return {"status": status, "detail": reason, "n_exact": n_exact}
        cap = knob_int(cfg, "max_proposals_per_run")
        for m in sorted(matches, key=lambda x: -x["confidence"])[:cap]:
            item = inc_items[m["item"] - 1]
            pr = inc_prs[m["pr"] - 1]
            if (item["id"], pr["number"]) in exact_pairs:
                continue  # the exact tier already carries this pair
            n_fuzzy += 1
            annotations.append(
                _annotation(
                    "fuzzy",
                    "proposed",
                    item,
                    pr,
                    confidence=m["confidence"],
                    rationale=m["reason"] or None,
                )
            )

    recorded = await _record_run(
        db_path,
        **_base_row(
            status="ok",
            repo=repo,
            cursor_after=(
                max(str(cursor_before), new_max_merged) if cursor_before else new_max_merged
            ),
            n_prs=len(prs),
            n_open_items=len(open_items),
            n_exact=n_exact,
            n_fuzzy=n_fuzzy,
            prompt_version=PROMPT_VERSION if fuzzy_ran else None,
            model=PULSE_MODEL if fuzzy_ran else None,
        ),
        annotations=annotations,
    )
    if recorded:
        _write_cursor(root, cursor, merged_at=new_max_merged)
    else:
        detail_notes.append("pulse_write_failed_cursor_preserved")
    await _record_telemetry(
        db_path,
        "ok" if recorded else "failed",
        f"repo={repo}|prs={len(prs)}|exact={n_exact}|fuzzy={n_fuzzy}|recorded={recorded}",
    )
    return {
        "status": "ok" if recorded else "failed",
        "repo": repo,
        "n_prs": len(prs),
        "n_open_items": len(open_items),
        "n_exact": n_exact,
        "n_fuzzy": n_fuzzy,
        "absorbed": absorbed,
        "recorded": recorded,
    }
