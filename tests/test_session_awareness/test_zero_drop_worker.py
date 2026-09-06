"""A degraded sweep must FREEZE its classes — never half-apply.

This is the detector's single most dangerous failure mode and the reason it
exists at all. If a leg fails (git errors, the PR history caps) and the worker
reconciles anyway, every branch it could not look at is marked RESOLVED. The
board then reads CLEAN, and "what fell through the cracks?" is answered zero —
confidently, and wrongly. A partial sweep must leave the store untouched for
the classes it could not complete, and must not advance recurrence either: a
degraded run is not a counted run.

The legs are independent on purpose, so a gh outage cannot blind the worktree
class and a broken worktree cannot blind the branch classes.
"""

import json

import aiosqlite
import pytest

from genesis.db.crud import zero_drop as zd
from genesis.session_awareness import zero_drop_worker as w

BRANCH = {
    "branch": "feat/stranded",
    "tip_sha": "aaa111",
    "ahead": 4,
    "behind": 0,
    "tip_date": "2020-01-01T00:00:00+00:00",
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A worker wired to a temp home + temp DB, with every git/gh leg faked."""
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GENESIS_ZERO_DROP_DISABLED", raising=False)
    monkeypatch.setattr(w, "effective_mode", lambda: "observe")
    monkeypatch.setattr(
        w,
        "load_config",
        lambda: dict(
            w.__dict__.get("_TEST_CFG", {}),
            **{
                "min_interval_minutes": 60,
                "branch_min_age_hours": 12,
                "worktree_min_age_hours": 6,
                "escalation_k": 3,
                "max_prs": 2000,
                "max_listed": 10,
                "alert_priority": "medium",
            },
        ),
    )

    legs = {
        "local": {"branches": [BRANCH]},
        "remote": {"names": set()},
        "prs": {"repo": "o/r", "prs": [], "limit_hit": False},
        "worktrees": {"observations": [], "errors": [], "held": set(), "prunable": 0},
        "base_ref": "origin/main",
    }

    async def _local(root, *, base="origin/main", runner=None):
        return legs["local"]

    async def _remote(root, *, remote="origin", runner=None):
        return legs["remote"]

    async def _prs(*, limit=2000, repo=None, runner=None):
        return legs["prs"]

    async def _observe(root, *, runner=None):
        return legs["worktrees"]

    async def _base(root, runner=None):
        return legs["base_ref"]

    monkeypatch.setattr(w, "list_local_branches", _local)
    monkeypatch.setattr(w, "list_remote_branch_names", _remote)
    monkeypatch.setattr(w, "list_all_prs", _prs)
    monkeypatch.setattr(w, "_observe_worktrees", _observe)
    monkeypatch.setattr(w, "_resolve_base_ref", _base)
    return legs


@pytest.fixture
async def db_path(tmp_path):
    from genesis.db.schema import create_all_tables

    path = tmp_path / "zd.db"
    conn = await aiosqlite.connect(str(path))
    try:
        await create_all_tables(conn)
        await conn.commit()
    finally:
        await conn.close()
    return str(path)


async def _run(db_path, **kw):
    return await w.run_zero_drop_worker(
        trigger="manual", force=True, db_path=db_path, repo_path="/repo", **kw
    )


async def _rows(db_path, **kw):
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    try:
        return await zd.list_findings(conn, **kw)
    finally:
        await conn.close()


async def _seed_open_finding(db_path, class_="unpushed_branch"):
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    try:
        await zd.apply_sweep(
            conn,
            class_=class_,
            present=[{"branch": "feat/stranded", "tip_sha": "aaa111"}],
            run_id="seed",
        )
    finally:
        await conn.close()


async def test_happy_path_records_findings_and_a_run_record(env, db_path):
    out = await _run(db_path)
    assert out["status"] == "ok"
    assert out["open_findings"] == 1
    assert [r["branch"] for r in await _rows(db_path)] == ["feat/stranded"]

    record = json.loads(w.last_run_path().read_text())
    # Namespaced per leg: the two legs share key names (both have a
    # `too_young`), so a flat merge would silently overwrite one count with
    # the other and the audit would stop summing to its denominator.
    assert record["stages"]["branches"]["refs_total"] == 1
    assert record["stages"]["branches"]["flagged_no_pr"] == 1
    assert set(record["stages"]) == {"branches", "worktrees"}
    assert record["counts_by_status"]["open"] == 1
    assert record["degraded"] == {}


@pytest.mark.parametrize(
    "leg,value,expected_note",
    [
        ("local", {"error": "for-each-ref boom"}, "for-each-ref"),
        ("remote", {"error": "ls-remote boom"}, "ls-remote"),
        ("prs", {"error": "gh boom"}, "pr history"),
        ("prs", {"repo": "o/r", "prs": [], "limit_hit": True}, "limit_hit"),
    ],
)
async def test_a_degraded_branch_leg_freezes_the_branch_classes(
    env, db_path, leg, value, expected_note
):
    """A pre-existing finding must SURVIVE a run that could not see it.

    Resolving it would report the branch as landed on the strength of a failed
    lookup — the detector manufacturing a clean board.
    """
    await _seed_open_finding(db_path)
    env[leg] = value

    out = await _run(db_path)
    assert out["status"] == "degraded"
    assert expected_note in out["degraded"]["branches"]
    assert "unpushed_branch" not in out["applied"], "a frozen class must not be reconciled"

    survivors = await _rows(db_path)
    assert [r["branch"] for r in survivors] == ["feat/stranded"]
    assert survivors[0]["status"] == "open"
    assert survivors[0]["consecutive_runs"] == 1, "a degraded run is not a counted run"


async def test_a_degraded_worktree_leg_does_not_blind_the_branch_legs(env, db_path):
    """The legs are independent: one broken worktree must not cost the branch
    classes their sweep."""
    await _seed_open_finding(db_path, class_="dirty_worktree")
    # The ENUMERATION failed (held=None) — no per-item granularity, so the
    # whole worktree class freezes.
    env["worktrees"] = {
        "observations": [],
        "errors": ["worktree list failed"],
        "held": None,
        "prunable": 0,
    }

    out = await _run(db_path)
    assert out["status"] == "degraded"
    assert "worktrees" in out["degraded"] and "branches" not in out["degraded"]
    assert "unpushed_branch" in out["applied"], "the healthy legs still reconcile"

    dirty = await _rows(db_path, class_="dirty_worktree")
    assert dirty[0]["status"] == "open", "the frozen class keeps its finding"


async def test_findings_resolve_only_on_a_COMPLETE_sweep(env, db_path):
    await _seed_open_finding(db_path)
    env["local"] = {"branches": []}  # a real, complete sweep that saw nothing

    out = await _run(db_path)
    assert out["status"] == "ok"
    assert await _rows(db_path) == []


async def test_debounce_blocks_a_second_sweep_and_writes_nothing(env, db_path):
    await _run(db_path)
    before = w.last_run_path().read_text()

    out = await w.run_zero_drop_worker(
        trigger="session_start", force=False, db_path=db_path, repo_path="/repo"
    )
    assert out["status"] == "debounced"
    assert w.last_run_path().read_text() == before


@pytest.mark.parametrize(
    "setup,expected",
    [("kill_switch", "skipped_disabled"), ("mode_off", "skipped_off")],
)
async def test_levers_stop_the_sweep_before_any_work(env, db_path, monkeypatch, setup, expected):
    if setup == "kill_switch":
        monkeypatch.setenv("GENESIS_ZERO_DROP_DISABLED", "1")
    else:
        monkeypatch.setattr(w, "effective_mode", lambda: "off")

    out = await _run(db_path)
    assert out["status"] == expected
    assert not w.last_run_path().exists()


async def test_sweep_emits_a_durable_heartbeat(env, db_path):
    """A DEAD detector is the failure mode with no natural symptom — it keeps
    answering with a stale, confident zero. The pulse is how that becomes
    visible, and it must be DURABLE (an out-of-process worker cannot reach the
    in-memory event ring the health probe also consults)."""
    await _run(db_path)
    conn = await aiosqlite.connect(db_path)
    try:
        cur = await conn.execute(
            "SELECT COUNT(*) FROM events WHERE subsystem = ? AND event_type = 'heartbeat'",
            (w.HEARTBEAT_SUBSYSTEM,),
        )
        assert (await cur.fetchone())[0] == 1
    finally:
        await conn.close()


async def test_a_degraded_run_still_pulses(env, db_path):
    """Otherwise a repo whose gh access broke would look like a dead detector,
    and the real fault (a broken leg) would be reported as the wrong one."""
    env["prs"] = {"error": "gh boom"}
    await _run(db_path)
    conn = await aiosqlite.connect(db_path)
    try:
        cur = await conn.execute(
            "SELECT COUNT(*) FROM events WHERE subsystem = ? AND event_type = 'heartbeat'",
            (w.HEARTBEAT_SUBSYSTEM,),
        )
        assert (await cur.fetchone())[0] == 1
    finally:
        await conn.close()


async def test_alert_mode_maintains_one_observation(env, db_path, monkeypatch):
    monkeypatch.setattr(w, "effective_mode", lambda: "alert")
    await _run(db_path)

    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    try:
        cur = await conn.execute(
            "SELECT content FROM observations WHERE source = ? AND resolved_at IS NULL",
            (w.ALERT_SOURCE,),
        )
        rows = await cur.fetchall()
        assert len(rows) == 1
        assert "1 stranded-work finding(s) open" in rows[0]["content"]
        assert "feat/stranded" in rows[0]["content"]
    finally:
        await conn.close()


async def test_alert_resolves_when_the_board_comes_clean(env, db_path, monkeypatch):
    monkeypatch.setattr(w, "effective_mode", lambda: "alert")
    await _run(db_path)
    env["local"] = {"branches": []}
    await _run(db_path)

    conn = await aiosqlite.connect(db_path)
    try:
        cur = await conn.execute(
            "SELECT COUNT(*) FROM observations WHERE source = ? AND resolved_at IS NULL",
            (w.ALERT_SOURCE,),
        )
        assert (await cur.fetchone())[0] == 0
    finally:
        await conn.close()


async def test_observe_mode_fills_the_board_without_alerting(env, db_path):
    await _run(db_path)  # env fixture pins mode=observe
    assert len(await _rows(db_path)) == 1
    conn = await aiosqlite.connect(db_path)
    try:
        cur = await conn.execute(
            "SELECT COUNT(*) FROM observations WHERE source = ?", (w.ALERT_SOURCE,)
        )
        assert (await cur.fetchone())[0] == 0
    finally:
        await conn.close()


async def test_base_ref_fallback_is_recorded_not_silent(env, db_path):
    """A wrong base ref inflates every ahead-count, so the assumption is
    stated on the run record instead of being invisible."""
    await _run(db_path)
    # Resolved cleanly TO origin/main — that is not a fallback, and the first
    # version of this could not tell the two apart, so it filed a fallback note
    # on every healthy run of every main-branch repo.
    assert json.loads(w.last_run_path().read_text())["notes"] == []

    env["base_ref"] = "origin/trunk"
    await _run(db_path)
    record = json.loads(w.last_run_path().read_text())
    assert record["base_ref"] == "origin/trunk"
    assert record["notes"] == []

    env["base_ref"] = None  # resolution FAILED
    await _run(db_path)
    record = json.loads(w.last_run_path().read_text())
    assert record["base_ref"] == "origin/main", "the fallback is still used"
    assert record["notes"] == ["base_ref_unresolved_using=origin/main"], (
        "a guessed base inflates every ahead-count — it must never be silent"
    )


# ---------------------------------------------------------------------------
# Blindness must reach a surface. A DEAD detector is caught by the heartbeat;
# a LIVE one with a permanently failing leg is not — it keeps pulsing, keeps
# writing a run record, and keeps the board exactly as it was.
# ---------------------------------------------------------------------------


async def _open_observations(db_path, source):
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    try:
        cur = await conn.execute(
            "SELECT content FROM observations WHERE source = ? AND resolved_at IS NULL",
            (source,),
        )
        return [r["content"] for r in await cur.fetchall()]
    finally:
        await conn.close()


async def test_a_blind_leg_raises_its_own_alarm(env, db_path):
    """An expired gh token freezes the branch classes forever. Without this the
    heartbeat still pulses, the findings alert still says whatever it said last
    week, and every health surface reads green."""
    env["prs"] = {"error": "gh auth token expired"}
    await _run(db_path)

    blind = await _open_observations(db_path, w.BLIND_SOURCE)
    assert len(blind) == 1
    assert "BLIND" in blind[0] and "branches" in blind[0]
    assert "not a measurement" in blind[0]


async def test_the_blind_alarm_is_raised_in_observe_mode_too(env, db_path):
    """The mode lever governs egress about FINDINGS. A broken instrument is not
    a finding — an operator who silenced the board did not ask to be kept in the
    dark about the board being broken."""
    assert w.effective_mode() == "observe"
    env["prs"] = {"error": "gh boom"}
    await _run(db_path)
    assert len(await _open_observations(db_path, w.BLIND_SOURCE)) == 1
    assert await _open_observations(db_path, w.ALERT_SOURCE) == []


async def test_the_blind_alarm_resolves_when_the_leg_recovers(env, db_path):
    env["prs"] = {"error": "gh boom"}
    await _run(db_path)
    env["prs"] = {"repo": "o/r", "prs": [], "limit_hit": False}
    await _run(db_path)
    assert await _open_observations(db_path, w.BLIND_SOURCE) == []


async def test_counts_are_published_with_their_coverage(env, db_path):
    """The count is of the whole STORE, so a run that froze a class is
    reporting rows it did not measure this time. Saying which classes were
    swept is the difference between a count and a claim."""
    await _run(db_path)
    assert json.loads(w.last_run_path().read_text())["coverage"] == "all classes swept"

    env["prs"] = {"error": "gh boom"}
    out = await _run(db_path)
    record = json.loads(w.last_run_path().read_text())
    assert "FROZEN" in record["coverage"]
    assert set(record["frozen_classes"]) == {"unpushed_branch", "pushed_no_pr"}
    assert out["coverage"] == record["coverage"]


async def test_one_unreadable_worktree_does_not_blind_the_whole_class(env, db_path):
    """Quarantine per ITEM, not per class. Freezing all 161 worktrees because
    one was unreadable is a self-inflicted blind spot — and on this install the
    margin for that was a single worktree."""
    await _seed_open_finding(db_path, class_="dirty_worktree")
    env["worktrees"] = {
        "observations": [
            {
                "path": "/w/ok",
                "branch": "other/dirty",
                "detached": False,
                "entries": [("M ", "f.py")],
                "newest_mtime": None,
            }
        ],
        "errors": ["/w/broken: status failed"],
        "held": {"feat/stranded"},
        "prunable": 0,
    }

    out = await _run(db_path)
    assert out["status"] == "degraded"
    assert "unreadable" in out["degraded"]["worktrees"]
    assert "dirty_worktree" in out["applied"], "the readable worktrees still reconcile"

    rows = await _rows(db_path, class_="dirty_worktree")
    by_branch = {r["branch"]: r for r in rows}
    assert by_branch["feat/stranded"]["status"] == "open", "the quarantined finding is held"
    assert "other/dirty" in by_branch, "the readable worktree still produced a finding"


async def test_a_prunable_worktree_is_counted_not_treated_as_an_error(env, db_path):
    env["worktrees"] = {
        "observations": [],
        "errors": [],
        "held": set(),
        "prunable": 3,
    }
    out = await _run(db_path)
    assert out["status"] == "ok", "a gone worktree is absent, not unreadable"
    assert json.loads(w.last_run_path().read_text())["stages"]["worktrees"][
        "prunable_skipped"
    ] == 3


async def test_a_class_that_fails_to_reconcile_degrades_only_itself(env, db_path, monkeypatch):
    """One unexpected DB error used to take the other two classes, the
    heartbeat and the run record with it — with an overdue pulse two days later
    as the only symptom."""
    real = zd.apply_sweep
    calls = {"n": 0}

    async def _flaky(db, *, class_, **kw):
        calls["n"] += 1
        if class_ == "unpushed_branch":
            raise RuntimeError("boom")
        return await real(db, class_=class_, **kw)

    monkeypatch.setattr(zd, "apply_sweep", _flaky)
    monkeypatch.setattr(w.zd_crud, "apply_sweep", _flaky)

    out = await _run(db_path)
    assert out["status"] == "degraded"
    assert "boom" in out["degraded"]["unpushed_branch"]
    assert "pushed_no_pr" in out["applied"] and "dirty_worktree" in out["applied"]
    assert w.last_run_path().exists(), "the run record still lands"


async def test_the_alert_survives_a_failed_create(env, db_path, monkeypatch):
    """Create-then-supersede, not the reverse. Superseding first leaves a window
    where every prior alert is resolved and the replacement does not exist — and
    a failed create makes that window last until the next sweep."""
    from genesis.db.crud import observations as obs

    monkeypatch.setattr(w, "effective_mode", lambda: "alert")
    await _run(db_path)
    assert len(await _open_observations(db_path, w.ALERT_SOURCE)) == 1

    async def _boom(*a, **kw):
        raise RuntimeError("insert failed")

    monkeypatch.setattr(obs, "create", _boom)
    env["local"] = {"branches": [{**BRANCH, "branch": "feat/other"}]}
    out = await _run(db_path)

    assert out["degraded"].get("alert") == "alert_failed"
    assert out["status"] == "degraded", "a failed alert is not an ok run"
    assert len(await _open_observations(db_path, w.ALERT_SOURCE)) == 1, (
        "the previous alert must survive a failed replacement"
    )
