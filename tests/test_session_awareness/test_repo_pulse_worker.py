"""Detached repo-pulse worker: end-to-end runs against a tmp DB with
migrations applied and fake gh/headless layers (ledger_worker test lineage).

The invariants under test: the FUZZY tier never writes session_ledger in
any mode (proposal-only by construction); the exact tier absorbs ONLY on
the explicit Ledger: marker and ONLY in live mode; the cursor advances
monotonically and only on recorded ok runs; debounced boundaries leave no
run row; reconciliation resolves proposals with the attribution guard;
re-covered windows never re-absorb a reopened item.
"""

from __future__ import annotations

import fcntl
import importlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from genesis.db.crud import repo_pulse as pulse_crud
from genesis.session_awareness import repo_pulse_worker as rpw
from genesis.session_awareness.repo_pulse_config import DEFAULTS

M58 = importlib.import_module("genesis.db.migrations.0058_session_charters")
M62 = importlib.import_module("genesis.db.migrations.0062_repo_pulse")

SID = "aaaabbbb-cccc-dddd-eeee-ffff00001111"
ITEM = "0123456789abcdef0123456789abcdef"
MERGED_NEW = "2026-07-16T10:00:00Z"
MERGED_OLD = "2026-07-14T10:00:00Z"


# ── fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def pulse_root(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "repo_pulse"
    monkeypatch.setattr(rpw, "_pulse_root", lambda: root)
    monkeypatch.setattr(rpw, "load_config", lambda: dict(DEFAULTS))
    return root


@pytest.fixture
def live_mode(monkeypatch):
    monkeypatch.setattr(rpw, "effective_mode", lambda: "live")


@pytest.fixture
async def db_path(tmp_path) -> Path:
    path = tmp_path / "genesis.db"
    pulse_crud._tables_verified = False
    async with aiosqlite.connect(str(path)) as db:
        await M58.up(db)
        await M62.up(db)
        await db.commit()
    yield path
    pulse_crud._tables_verified = False


def _pr(number=1080, title="feat: pulse work", body="", merged=MERGED_NEW):
    return {"number": number, "title": title, "body": body, "mergedAt": merged}


def _gh(prs, *, limit_hit=False, error=None, repo="owner/repo"):
    """Fake list_merged_prs — returns a canned listing, records calls."""
    calls: list[dict] = []

    async def fake(**kwargs):
        calls.append(kwargs)
        if error is not None:
            return {"error": error}
        return {
            "repo": repo,
            "prs": sorted(prs, key=lambda p: p["mergedAt"]),
            "limit_hit": limit_hit,
        }

    fake.calls = calls
    return fake


def _headless(matches=None, *, status="ok", reason=None):
    async def fake(prompt, **kwargs):
        if status != "ok":
            out = {"status": status}
            if reason:
                out["reason"] = reason
            return out
        inner = json.dumps({"matches": matches or []})
        return {"status": "ok", "stdout": json.dumps({"result": inner})}

    return fake


async def _seed_item(
    db_path,
    item_id=ITEM,
    *,
    text="ship the repo-pulse annotator",
    status="open",
    session_id=SID,
    source_ref=None,
    evidence=None,
    created="2026-07-10T00:00:00+00:00",
):
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            "INSERT INTO session_ledger "
            "(id, session_id, text, status, source_ref, added_by, evidence, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'foreground', ?, ?)",
            (item_id, session_id, text, status, source_ref, evidence, created),
        )
        await db.commit()


async def _item_row(db_path, item_id=ITEM) -> dict | None:
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM session_ledger WHERE id = ?", (item_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def _runs(db_path) -> list[dict]:
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        return await pulse_crud.list_runs(db)


async def _anns(db_path, **kw) -> list[dict]:
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        return await pulse_crud.list_annotations(db, **kw)


def _cursor(root: Path) -> dict:
    return json.loads((root / rpw.CURSOR_FILENAME).read_text())


def _write_cursor_file(root: Path, **data):
    root.mkdir(parents=True, exist_ok=True)
    base = {"last_merged_at": None, "last_run_ts": None, "runs": 0}
    base.update(data)
    (root / rpw.CURSOR_FILENAME).write_text(json.dumps(base))


async def _run(db_path, monkeypatch, *, gh, headless=None, force=True, **kw):
    monkeypatch.setattr(rpw, "list_merged_prs", gh)
    monkeypatch.setattr(rpw, "run_headless_json", headless or _headless([]))
    return await rpw.run_pulse_worker(trigger="manual", force=force, db_path=db_path, **kw)


# ── mode matrix ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_off_mode_leaves_zero_trace(pulse_root, db_path, monkeypatch):
    monkeypatch.setattr(rpw, "effective_mode", lambda: "off")
    out = await _run(db_path, monkeypatch, gh=_gh([_pr()]))
    assert out == {"status": "skipped_off"}
    assert await _runs(db_path) == []
    assert not pulse_root.exists()  # no lock, no cursor, no dir


@pytest.mark.asyncio
async def test_env_kill_switch(pulse_root, db_path, monkeypatch, live_mode):
    monkeypatch.setenv("GENESIS_REPO_PULSE_DISABLED", "1")
    out = await _run(db_path, monkeypatch, gh=_gh([_pr()]))
    assert out == {"status": "skipped_disabled"}
    assert await _runs(db_path) == []


@pytest.mark.asyncio
async def test_live_mode_marker_absorbs_with_evidence(pulse_root, db_path, monkeypatch, live_mode):
    await _seed_item(db_path)
    out = await _run(db_path, monkeypatch, gh=_gh([_pr(body=f"Ledger: {ITEM}")]))
    assert out["status"] == "ok"
    assert out["absorbed"] == [ITEM]
    row = await _item_row(db_path)
    assert row["status"] == "absorbed"
    assert "PR #1080" in row["evidence"]
    assert "[repo-pulse exact]" in row["evidence"]
    anns = await _anns(db_path)
    assert len(anns) == 1
    assert anns[0]["tier"] == "exact"
    assert anns[0]["status"] == "applied"
    assert anns[0]["rationale"] == "ledger-marker"
    # cursor advanced to the max processed mergedAt
    assert _cursor(pulse_root)["last_merged_at"] == MERGED_NEW


@pytest.mark.asyncio
async def test_propose_only_mode_never_calls_ledger_update(pulse_root, db_path, monkeypatch):
    monkeypatch.setattr(rpw, "effective_mode", lambda: "propose_only")

    async def boom(*a, **kw):  # pragma: no cover — the assertion IS non-invocation
        raise AssertionError("ledger_update must not be called in propose_only")

    monkeypatch.setattr(rpw, "ledger_update", boom)
    await _seed_item(db_path)
    out = await _run(db_path, monkeypatch, gh=_gh([_pr(body=f"Ledger: {ITEM}")]))
    assert out["status"] == "ok"
    assert (await _item_row(db_path))["status"] == "open"
    anns = await _anns(db_path)
    assert anns[0]["status"] == "proposed"
    assert anns[0]["rationale"] == "ledger-marker (propose_only)"


@pytest.mark.asyncio
async def test_bare_hex_is_proposed_even_in_live(pulse_root, db_path, monkeypatch, live_mode):
    await _seed_item(db_path)
    out = await _run(db_path, monkeypatch, gh=_gh([_pr(body=f"relates to {ITEM}")]))
    assert out["status"] == "ok"
    assert (await _item_row(db_path))["status"] == "open"
    anns = await _anns(db_path)
    assert anns[0]["tier"] == "exact"
    assert anns[0]["status"] == "proposed"
    assert anns[0]["rationale"] == "bare-hex"


# ── THE invariant: fuzzy never writes the live ledger ────────────────────


@pytest.mark.asyncio
async def test_fuzzy_matches_never_write_session_ledger(
    pulse_root, db_path, monkeypatch, live_mode
):
    await _seed_item(db_path)
    before = await _item_row(db_path)
    out = await _run(
        db_path,
        monkeypatch,
        gh=_gh([_pr()]),  # no hex anywhere — fuzzy only
        headless=_headless([{"item": 1, "pr": 1, "confidence": 0.95, "reason": "same work"}]),
    )
    assert out["status"] == "ok"
    assert out["n_fuzzy"] == 1
    anns = await _anns(db_path)
    assert anns[0]["tier"] == "fuzzy"
    assert anns[0]["status"] == "proposed"
    assert anns[0]["confidence"] == 0.95
    after = await _item_row(db_path)
    assert after == before  # byte-identical row: status open, updated_at untouched


@pytest.mark.asyncio
async def test_no_pulse_inserts_into_session_ledger(pulse_root, db_path, monkeypatch, live_mode):
    """Pulse only UPDATEs — it must never add rows (added_by='pulse' is
    reserved for a future writer, not this one)."""
    await _seed_item(db_path)
    await _run(
        db_path,
        monkeypatch,
        gh=_gh([_pr(body=f"Ledger: {ITEM}")]),
        headless=_headless([{"item": 1, "pr": 1, "confidence": 0.9, "reason": "x"}]),
    )
    async with aiosqlite.connect(str(db_path)) as db:
        cur = await db.execute("SELECT COUNT(*) FROM session_ledger")
        assert (await cur.fetchone())[0] == 1


# ── cursor + debounce ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cursor_not_advanced_on_gh_failure(pulse_root, db_path, monkeypatch, live_mode):
    _write_cursor_file(pulse_root, last_merged_at=MERGED_OLD)
    out = await _run(db_path, monkeypatch, gh=_gh([], error="pr list failed (rc=1)"))
    assert out["status"] == "failed"
    runs = await _runs(db_path)
    assert runs[0]["status"] == "failed"
    assert "pr list failed" in runs[0]["detail"]
    cur = _cursor(pulse_root)
    assert cur["last_merged_at"] == MERGED_OLD  # untouched
    assert cur["last_run_ts"] is not None  # but the attempt still debounces


@pytest.mark.asyncio
async def test_cursor_not_advanced_on_fuzzy_timeout(pulse_root, db_path, monkeypatch, live_mode):
    await _seed_item(db_path)
    _write_cursor_file(pulse_root, last_merged_at=MERGED_OLD)
    out = await _run(db_path, monkeypatch, gh=_gh([_pr()]), headless=_headless(status="timeout"))
    assert out["status"] == "timeout"
    assert (await _runs(db_path))[0]["status"] == "timeout"
    assert _cursor(pulse_root)["last_merged_at"] == MERGED_OLD


@pytest.mark.asyncio
async def test_fuzzy_failure_still_persists_exact_work(pulse_root, db_path, monkeypatch, live_mode):
    """Exact absorbs land even when the fuzzy call dies — and the run row
    carries the exact annotations so nothing is invisible."""
    await _seed_item(db_path)
    second = "fedcba9876543210fedcba9876543210"
    await _seed_item(db_path, second, text="another open item")
    out = await _run(
        db_path,
        monkeypatch,
        gh=_gh([_pr(body=f"Ledger: {ITEM}")]),
        headless=_headless(status="failed", reason="exit_1"),
    )
    assert out["status"] == "failed"
    assert out["n_exact"] == 1
    assert (await _item_row(db_path))["status"] == "absorbed"
    anns = await _anns(db_path)
    assert [a["status"] for a in anns] == ["applied"]
    assert _cursor(pulse_root)["last_merged_at"] is None  # window re-covers


@pytest.mark.asyncio
async def test_cursor_advance_is_monotonic(pulse_root, db_path, monkeypatch, live_mode):
    _write_cursor_file(pulse_root, last_merged_at="2026-07-17T00:00:00Z")
    # a stale worker processing an older window must not regress the cursor
    out = await _run(db_path, monkeypatch, gh=_gh([_pr(merged="2026-07-18T00:00:00Z")]))
    assert out["status"] == "ok"
    assert _cursor(pulse_root)["last_merged_at"] == "2026-07-18T00:00:00Z"


@pytest.mark.asyncio
async def test_debounce_exits_silently_without_run_row(pulse_root, db_path, monkeypatch, live_mode):
    _write_cursor_file(pulse_root, last_run_ts=datetime.now(UTC).isoformat())
    out = await _run(db_path, monkeypatch, gh=_gh([_pr()]), force=False)
    assert out == {"status": "debounced"}
    assert await _runs(db_path) == []


@pytest.mark.asyncio
async def test_stale_last_run_does_not_debounce(pulse_root, db_path, monkeypatch, live_mode):
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    _write_cursor_file(pulse_root, last_run_ts=old)
    out = await _run(db_path, monkeypatch, gh=_gh([_pr()]), force=False)
    assert out["status"] == "ok"


@pytest.mark.asyncio
async def test_no_new_prs_records_and_keeps_cursor(pulse_root, db_path, monkeypatch, live_mode):
    _write_cursor_file(pulse_root, last_merged_at=MERGED_NEW)
    out = await _run(db_path, monkeypatch, gh=_gh([_pr(merged=MERGED_OLD)]))
    assert out["status"] == "no_new_prs"
    runs = await _runs(db_path)
    assert runs[0]["status"] == "no_new_prs"
    assert _cursor(pulse_root)["last_merged_at"] == MERGED_NEW


@pytest.mark.asyncio
async def test_limit_hit_is_recorded_loudly(pulse_root, db_path, monkeypatch, live_mode):
    out = await _run(db_path, monkeypatch, gh=_gh([_pr()], limit_hit=True))
    assert out["status"] == "ok"
    assert "limit_hit" in (await _runs(db_path))[0]["detail"]


@pytest.mark.asyncio
async def test_lock_busy_recorded(pulse_root, db_path, monkeypatch, live_mode):
    pulse_root.mkdir(parents=True)
    holder = (pulse_root / rpw.LOCK_FILENAME).open("w")
    try:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        out = await _run(db_path, monkeypatch, gh=_gh([_pr()]))
        assert out == {"status": "lock_busy"}
        assert (await _runs(db_path))[0]["status"] == "lock_busy"
    finally:
        holder.close()


@pytest.mark.asyncio
async def test_pre_migration_write_miss_preserves_cursor(
    pulse_root, tmp_path, monkeypatch, live_mode
):
    """Un-migrated DB: run completes, nothing recorded, cursor untouched —
    the window replays once the migration lands."""
    bare = tmp_path / "bare.db"
    async with aiosqlite.connect(str(bare)) as db:
        await M58.up(db)  # ledger exists, pulse tables don't
        await db.commit()
    pulse_crud._tables_verified = False
    out = await _run(bare, monkeypatch, gh=_gh([_pr()]))
    assert out["status"] == "failed"
    assert not (pulse_root / rpw.CURSOR_FILENAME).exists()


# ── re-absorb guard ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reopened_item_not_reabsorbed_on_recovered_window(
    pulse_root, db_path, monkeypatch, live_mode
):
    await _seed_item(db_path)
    gh = _gh([_pr(body=f"Ledger: {ITEM}")])
    out = await _run(db_path, monkeypatch, gh=gh)
    assert out["absorbed"] == [ITEM]
    # the user reopens the item; a later run re-covers the same window
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute("UPDATE session_ledger SET status = 'open' WHERE id = ?", (ITEM,))
        await db.commit()
    (pulse_root / rpw.CURSOR_FILENAME).unlink()  # force full re-coverage
    out2 = await _run(db_path, monkeypatch, gh=gh)
    assert out2["status"] == "ok"
    assert out2["absorbed"] == []
    assert (await _item_row(db_path))["status"] == "open"
    applied = [a for a in await _anns(db_path) if a["status"] == "applied"]
    assert len(applied) == 1  # the original annotation, no duplicate


# ── reconciliation ───────────────────────────────────────────────────────


async def _seed_proposal(db_path, ann_id, item_id, pr_number, observed_at=None):
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        await pulse_crud.record_run(
            db,
            run_id=f"seed-{ann_id}",
            started_at="2026-07-15T00:00:00+00:00",
            finished_at="2026-07-15T00:00:01+00:00",
            trigger="manual",
            repo="o/r",
            cursor_before=None,
            cursor_after=None,
            status="ok",
            annotations=[
                {
                    "id": ann_id,
                    "observed_at": observed_at or datetime.now(UTC).isoformat(),
                    "tier": "fuzzy",
                    "item_id": item_id,
                    "item_session_id": SID,
                    "item_text": "t",
                    "pr_number": pr_number,
                    "status": "proposed",
                    "confidence": 0.9,
                }
            ],
        )


@pytest.mark.asyncio
async def test_reconcile_transitions(pulse_root, db_path, monkeypatch, live_mode):
    i_confirmed = "1111111111111111aaaaaaaaaaaaaaaa"
    i_other = "2222222222222222aaaaaaaaaaaaaaaa"
    i_dropped = "3333333333333333aaaaaaaaaaaaaaaa"
    i_done = "4444444444444444aaaaaaaaaaaaaaaa"
    i_fresh = "5555555555555555aaaaaaaaaaaaaaaa"
    i_stale = "6666666666666666aaaaaaaaaaaaaaaa"
    await _seed_item(db_path, i_confirmed, status="absorbed", evidence="PR #1080: pulse (merged x)")
    await _seed_item(db_path, i_other, status="absorbed", evidence="PR #999: other work")
    await _seed_item(db_path, i_dropped, status="dropped")
    await _seed_item(db_path, i_done, status="done")
    await _seed_item(db_path, i_fresh, status="open")
    await _seed_item(db_path, i_stale, status="open")
    for n, (ann, item) in enumerate(
        [
            ("a-conf", i_confirmed),
            ("a-other", i_other),
            ("a-drop", i_dropped),
            ("a-done", i_done),
            ("a-fresh", i_fresh),
        ]
    ):
        await _seed_proposal(db_path, ann, item, 1080 + (0 if ann == "a-conf" else n))
    await _seed_proposal(db_path, "a-stale", i_stale, 1099, observed_at="2026-05-01T00:00:00+00:00")
    await _seed_proposal(db_path, "a-orphan", "9999999999999999aaaaaaaaaaaaaaaa", 1098)
    pulse_crud._tables_verified = False

    out = await _run(db_path, monkeypatch, gh=_gh([]))  # no new prs; reconcile still runs
    assert out["status"] == "no_new_prs"
    by_id = {a["id"]: a for a in await _anns(db_path)}
    assert by_id["a-conf"]["status"] == "confirmed"
    assert by_id["a-other"]["status"] == "superseded"  # attribution guard
    assert by_id["a-other"]["resolution_ref"] == "absorbed_via_other_evidence"
    assert by_id["a-drop"]["status"] == "rejected"
    assert by_id["a-done"]["status"] == "superseded"
    assert by_id["a-fresh"]["status"] == "proposed"  # live proposal untouched
    assert by_id["a-stale"]["status"] == "superseded"
    assert by_id["a-stale"]["resolution_ref"] == "stale_30d"
    assert by_id["a-orphan"]["status"] == "superseded"
    assert by_id["a-orphan"]["resolution_ref"] == "item_missing"
    assert "reconciled" in (await _runs(db_path))[0]["detail"]


# ── fuzzy shaping ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fuzzy_skips_pairs_already_exact_matched(pulse_root, db_path, monkeypatch, live_mode):
    """propose_only: the exact tier already carries the (item, pr) pair —
    a fuzzy echo of the same pair is noise, not a second signal."""
    monkeypatch.setattr(rpw, "effective_mode", lambda: "propose_only")
    await _seed_item(db_path)
    out = await _run(
        db_path,
        monkeypatch,
        gh=_gh([_pr(body=f"Ledger: {ITEM}")]),
        headless=_headless([{"item": 1, "pr": 1, "confidence": 0.9, "reason": "dup"}]),
    )
    assert out["status"] == "ok"
    assert out["n_exact"] == 1
    assert out["n_fuzzy"] == 0
    assert [a["tier"] for a in await _anns(db_path)] == ["exact"]


@pytest.mark.asyncio
async def test_fuzzy_proposal_cap(pulse_root, db_path, monkeypatch, live_mode):
    monkeypatch.setattr(rpw, "load_config", lambda: dict(DEFAULTS, max_proposals_per_run=2))
    await _seed_item(db_path)
    prs = [_pr(number=1080 + i, merged=f"2026-07-16T10:00:{i:02d}Z") for i in range(5)]
    matches = [
        {"item": 1, "pr": i + 1, "confidence": 0.5 + i / 10, "reason": "r"} for i in range(5)
    ]
    out = await _run(db_path, monkeypatch, gh=_gh(prs), headless=_headless(matches))
    assert out["n_fuzzy"] == 2
    confs = sorted(a["confidence"] for a in await _anns(db_path))
    assert confs == [0.8, 0.9]  # highest-confidence first


@pytest.mark.asyncio
async def test_no_open_items_skips_fuzzy_but_records(pulse_root, db_path, monkeypatch, live_mode):
    called = []

    async def fake_headless(*a, **kw):  # pragma: no cover
        called.append(1)
        return {"status": "ok", "stdout": "{}"}

    monkeypatch.setattr(rpw, "run_headless_json", fake_headless)
    monkeypatch.setattr(rpw, "list_merged_prs", _gh([_pr()]))
    out = await rpw.run_pulse_worker(trigger="manual", force=True, db_path=db_path)
    assert out["status"] == "ok"
    assert out["n_open_items"] == 0
    assert called == []  # no Haiku call burned on an empty ledger
    runs = await _runs(db_path)
    assert runs[0]["model"] is None  # fuzzy never ran
    assert _cursor(rpw._pulse_root())["last_merged_at"] == MERGED_NEW
