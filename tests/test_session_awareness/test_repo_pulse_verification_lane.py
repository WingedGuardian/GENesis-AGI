"""The verification lane, end-to-end through ``run_pulse_worker`` (#1718 half B).

Harness mirrors ``test_repo_pulse_worker.py``: real tmp DB with migrations, the
worker driven through its public entry, gh layers monkeypatched at module level
— these are e2e tests of the lane's wiring, not unit tests of a model of it.

The invariants: one row per merged PR, docs-only born closed, code born open;
an UNREADABLE or EMPTY changed-file list opens the row (fail toward keeping the
obligation — the other direction silently forgives an unverified merge); window
re-coverage creates zero rows AND spends zero extra file-list API calls; a lane
failure surfaces on the run row's detail and never breaks the absorb lanes; the
knob and the pre-migration window each stop the lane without a single gh call.
Plus ``list_pr_files``'s own fail-closed contract, runner-injected.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import aiosqlite
import pytest

from genesis.db.crud import repo_pulse as pulse_crud
from genesis.db.schema._tables import TABLES
from genesis.session_awareness import repo_pulse_gh as gh_mod
from genesis.session_awareness import repo_pulse_worker as rpw
from genesis.session_awareness.repo_pulse_config import DEFAULTS

M58 = importlib.import_module("genesis.db.migrations.0058_session_charters")
M62 = importlib.import_module("genesis.db.migrations.0062_repo_pulse")
M84 = importlib.import_module("genesis.db.migrations.0084_repo_pulse_target_kind")
MIG = importlib.import_module("genesis.db.migrations.20260906234824_pr_verifications")

MERGED = "2026-09-06T10:00:00Z"
REPO = "owner/repo"


@pytest.fixture
def pulse_root(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "repo_pulse"
    monkeypatch.setattr(rpw, "_pulse_root", lambda: root)
    monkeypatch.setattr(rpw, "load_config", lambda: dict(DEFAULTS))
    monkeypatch.setattr(rpw, "open_prs_cache_path", lambda: root / "open_prs.json")
    monkeypatch.setattr(rpw, "effective_mode", lambda: "live")
    return root


@pytest.fixture
async def db_path(tmp_path) -> Path:
    path = tmp_path / "genesis.db"
    pulse_crud._tables_verified = False
    async with aiosqlite.connect(str(path)) as db:
        await M58.up(db)
        await M62.up(db)
        await M84.up(db)
        await MIG.up(db)
        await db.execute(TABLES["follow_ups"])
        await db.commit()
    yield path
    pulse_crud._tables_verified = False


def _pr(number, title="feat: x", merged=MERGED):
    return {"number": number, "title": title, "body": "", "mergedAt": merged}


def _gh(prs):
    async def fake(**kwargs):
        return {"repo": REPO, "prs": sorted(prs, key=lambda p: p["mergedAt"]), "limit_hit": False}

    return fake


def _open_gh():
    async def fake(**kwargs):
        return {"repo": REPO, "prs": [], "limit_hit": False}

    return fake


def _headless():
    async def fake(prompt, **kwargs):
        return {"status": "ok", "stdout": json.dumps({"result": json.dumps({"matches": []})})}

    return fake


def _files(per_pr: dict):
    """Fake list_pr_files: per-PR canned results, call-counting.

    ``per_pr`` maps pr_number → {"files": [...]} | {"error": "..."}.
    An unmapped number is a test bug — raise, never invent data.
    """
    calls: list[int] = []

    async def fake(pr_number, *, repo, runner=None):
        calls.append(pr_number)
        if pr_number not in per_pr:
            raise AssertionError(f"unexpected list_pr_files({pr_number})")
        return per_pr[pr_number]

    fake.calls = calls
    return fake


async def _run(db_path, monkeypatch, *, gh, files):
    monkeypatch.setattr(rpw, "list_merged_prs", gh)
    monkeypatch.setattr(rpw, "list_open_prs", _open_gh())
    monkeypatch.setattr(rpw, "run_headless_json", _headless())
    monkeypatch.setattr(rpw, "list_pr_files", files)
    return await rpw.run_pulse_worker(trigger="manual", force=True, db_path=db_path)


async def _rows(db_path) -> list[dict]:
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM pr_verifications ORDER BY pr_number")
        return [dict(r) for r in await cur.fetchall()]


async def _run_detail(db_path) -> str:
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        return (await pulse_crud.list_runs(db))[0].get("detail") or ""


# ── the lane's classification ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_code_pr_opens_a_row(pulse_root, db_path, monkeypatch):
    files = _files({50: {"files": ["src/genesis/x.py", "README.md"]}})
    out = await _run(db_path, monkeypatch, gh=_gh([_pr(50)]), files=files)
    assert out["status"] == "ok"
    rows = await _rows(db_path)
    assert len(rows) == 1
    assert rows[0]["status"] == "open"
    assert rows[0]["repo"] == REPO and rows[0]["merged_at"] == MERGED
    assert "verification opened=1 auto-closed=0" in await _run_detail(db_path)


@pytest.mark.asyncio
async def test_docs_only_pr_is_born_closed_with_the_reason(pulse_root, db_path, monkeypatch):
    files = _files({51: {"files": ["docs/a.md", "CHANGELOG.md"]}})
    await _run(db_path, monkeypatch, gh=_gh([_pr(51, title="docs: y")]), files=files)
    (row,) = await _rows(db_path)
    assert row["status"] == "closed"
    assert "docs-only" in row["closed_reason"]
    assert "deterministic" in row["closed_reason"]


@pytest.mark.asyncio
async def test_unreadable_file_list_opens_the_row(pulse_root, db_path, monkeypatch):
    """THE fail-direction case: an error must cost a validator look, never
    grant a silent exemption."""
    files = _files({52: {"error": "pr files failed (rc=1)"}})
    await _run(db_path, monkeypatch, gh=_gh([_pr(52)]), files=files)
    (row,) = await _rows(db_path)
    assert row["status"] == "open"


@pytest.mark.asyncio
async def test_empty_file_list_opens_the_row(pulse_root, db_path, monkeypatch):
    """A merged PR touches at least one file — an empty list means the read
    lied (the empty-vs-unreadable ambiguity, measured as a security HIGH on the
    hook's sibling reader). Empty must classify exactly like unreadable."""
    files = _files({53: {"files": []}})
    await _run(db_path, monkeypatch, gh=_gh([_pr(53)]), files=files)
    (row,) = await _rows(db_path)
    assert row["status"] == "open"


@pytest.mark.asyncio
async def test_mixed_diff_opens_the_row(pulse_root, db_path, monkeypatch):
    files = _files({54: {"files": ["docs/a.md", "src/genesis/x.py"]}})
    await _run(db_path, monkeypatch, gh=_gh([_pr(54)]), files=files)
    assert (await _rows(db_path))[0]["status"] == "open"


# ── idempotency, and its API cost ────────────────────────────────────────


@pytest.mark.asyncio
async def test_recovered_window_creates_zero_rows_and_spends_zero_api_calls(
    pulse_root, db_path, monkeypatch
):
    """Re-running over the same merged PRs must be free twice over: no second
    row (the unique index) and no second changed-files fetch (the exists
    pre-check) — the cursor re-covers a day behind BY DESIGN, every day."""
    files = _files({55: {"files": ["src/x.py"]}, 56: {"files": ["docs/a.md"]}})
    await _run(db_path, monkeypatch, gh=_gh([_pr(55), _pr(56)]), files=files)
    assert sorted(files.calls) == [55, 56]
    # second tick over the SAME window — cursor cleared so the PRs re-enumerate
    (pulse_root / rpw.CURSOR_FILENAME).unlink()
    await _run(db_path, monkeypatch, gh=_gh([_pr(55), _pr(56)]), files=files)
    assert sorted(files.calls) == [55, 56], "re-coverage re-fetched file lists"
    assert len(await _rows(db_path)) == 2


# ── failure and off postures ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lane_exception_is_noted_and_cannot_break_the_absorb_lanes(
    pulse_root, db_path, monkeypatch
):
    """A lane exception is CAUGHT — it never propagates out to kill the absorb
    lanes or ``_record_run`` — and NOTED; and then, because the lane could not
    record its window, the run is failed so the shared cursor holds.

    An earlier version of this test asserted ``status == "ok"`` on the theory
    that a best-effort side lane should never fail a run. That was the defect:
    the run row is not what is at stake, the CURSOR is. Recording ok retires
    those PRs from every future enumeration with nothing to re-present them —
    the same reasoning ``test_ledger_read_failure_fails_run_and_keeps_cursor``
    already applies to the ledger read.
    """

    async def boom(pr_number, *, repo, runner=None):
        raise RuntimeError("gh exploded")

    out = await _run(db_path, monkeypatch, gh=_gh([_pr(57)]), files=boom)
    assert out["status"] == "failed"
    detail = await _run_detail(db_path)
    assert "verification_lane_failed" in detail, "the cause must be recorded"
    assert "verification_lane_incomplete" in detail
    assert await _rows(db_path) == []


@pytest.mark.asyncio
async def test_knob_off_means_zero_rows_and_zero_calls(pulse_root, db_path, monkeypatch):
    cfg = dict(DEFAULTS)
    cfg["verification_enabled"] = False
    monkeypatch.setattr(rpw, "load_config", lambda: cfg)
    files = _files({})
    out = await _run(db_path, monkeypatch, gh=_gh([_pr(58)]), files=files)
    assert out["status"] == "ok"
    assert files.calls == []
    assert await _rows(db_path) == []


@pytest.mark.asyncio
async def test_pre_migration_window_spends_no_api_calls(pulse_root, tmp_path, monkeypatch):
    """Tables absent (subprocess pre-migration): the lane must notice BEFORE
    the per-PR loop — a gh call whose result cannot land is a spent budget."""
    path = tmp_path / "old.db"
    pulse_crud._tables_verified = False
    async with aiosqlite.connect(str(path)) as db:
        await M58.up(db)
        await M62.up(db)
        await M84.up(db)  # everything EXCEPT pr_verifications
        await db.execute(TABLES["follow_ups"])
        await db.commit()
    files = _files({})
    out = await _run(path, monkeypatch, gh=_gh([_pr(59)]), files=files)
    pulse_crud._tables_verified = False
    # No gh call is spent (the guard precedes the per-PR loop), AND the run
    # fails so the cursor holds — see
    # test_pre_migration_run_fails_and_keeps_the_cursor for the second half.
    assert out["status"] == "failed"
    assert files.calls == []


# ── list_pr_files: the fail-closed contract, runner-injected ─────────────


def _runner(rc=0, out="", err=""):
    calls: list[list[str]] = []

    async def run(argv):
        calls.append(argv)
        return rc, out, err

    run.calls = calls
    return run


def _line(filename, previous=None):
    return json.dumps({"filename": filename, "previous_filename": previous})


@pytest.mark.asyncio
async def test_files_happy_path_parses_lines():
    out = "\n".join([_line("a.py"), _line("docs/b.md")])
    got = await gh_mod.list_pr_files(1, repo=REPO, runner=_runner(out=out))
    assert got == {"files": ["a.py", "docs/b.md"]}


@pytest.mark.asyncio
async def test_files_rename_source_counts():
    """A file renamed OUT of code INTO docs must not read docs-only — the
    rename SOURCE is a path the PR touched."""
    out = _line("docs/renamed.md", previous="src/was_code.py")
    got = await gh_mod.list_pr_files(1, repo=REPO, runner=_runner(out=out))
    assert got == {"files": ["docs/renamed.md", "src/was_code.py"]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "out",
    [
        "not json at all",
        json.dumps(["a", "list"]),
        json.dumps({"filename": None}),
        json.dumps({"filename": ""}),
        json.dumps({"filename": "a.py", "previous_filename": ""}),
    ],
)
async def test_files_malformed_rows_are_errors_not_partial_lists(out):
    got = await gh_mod.list_pr_files(1, repo=REPO, runner=_runner(out=out))
    assert "error" in got, "a partial list that looks complete is the failure mode"


@pytest.mark.asyncio
async def test_files_nonzero_rc_is_an_error():
    got = await gh_mod.list_pr_files(1, repo=REPO, runner=_runner(rc=1, err="boom"))
    assert "error" in got


@pytest.mark.asyncio
async def test_files_api_cap_is_an_error_not_a_truncated_list():
    out = "\n".join(_line(f"f{i}.py") for i in range(gh_mod._PR_FILES_API_CAP))
    got = await gh_mod.list_pr_files(1, repo=REPO, runner=_runner(out=out))
    assert "error" in got and "cap" in got["error"]


# ── recovery: an incomplete lane must not let the cursor retire its PRs ──


async def _cursor(pulse_root):
    return json.loads((pulse_root / rpw.CURSOR_FILENAME).read_text())


@pytest.mark.asyncio
async def test_pre_migration_run_fails_and_keeps_the_cursor(pulse_root, tmp_path, monkeypatch):
    """THE recovery invariant, and the one the first version of this lane got
    wrong: idempotency is NOT recovery.

    The (repo, pr_number) unique index guarantees a retry cannot duplicate — but
    nothing retries a PR the SHARED cursor has advanced past. The pre-migration
    window is structural on every existing install (the run-recording tables
    predate pr_verifications), so a lane that no-ops quietly while the run
    records `ok` would strand every merge between deploy and the next server
    restart: exactly the obligations this feature exists to keep.
    """
    path = tmp_path / "old.db"
    pulse_crud._tables_verified = False
    async with aiosqlite.connect(str(path)) as db:
        await M58.up(db)
        await M62.up(db)
        await M84.up(db)  # everything EXCEPT pr_verifications
        await db.execute(TABLES["follow_ups"])
        await db.commit()
    out = await _run(path, monkeypatch, gh=_gh([_pr(70)]), files=_files({}))
    pulse_crud._tables_verified = False
    assert out["status"] == "failed"
    assert out["detail"] == "verification_lane_incomplete"
    assert (await _cursor(pulse_root))["last_merged_at"] is None, (
        "the cursor advanced past a PR the lane never recorded — it is gone"
    )


@pytest.mark.asyncio
async def test_the_next_tick_recovers_every_stranded_pr(pulse_root, tmp_path, monkeypatch):
    """The other half: having kept the cursor, the migration landing must let the
    window re-cover — the PRs missed pre-migration end up recorded, not lost."""
    path = tmp_path / "recover.db"
    pulse_crud._tables_verified = False
    async with aiosqlite.connect(str(path)) as db:
        await M58.up(db)
        await M62.up(db)
        await M84.up(db)
        await db.execute(TABLES["follow_ups"])
        await db.commit()
    prs = [_pr(80), _pr(81, title="docs: d")]
    first = await _run(path, monkeypatch, gh=_gh(prs), files=_files({}))
    assert first["status"] == "failed"
    # the server restarts and the migration lands
    async with aiosqlite.connect(str(path)) as db:
        await MIG.up(db)
        await db.commit()
    pulse_crud._tables_verified = False
    files = _files({80: {"files": ["src/x.py"]}, 81: {"files": ["docs/d.md"]}})
    second = await _run(path, monkeypatch, gh=_gh(prs), files=files)
    pulse_crud._tables_verified = False
    assert second["status"] == "ok"
    rows = await _rows(path)
    assert [r["pr_number"] for r in rows] == [80, 81], "stranded PRs were not recovered"
    assert [r["status"] for r in rows] == ["open", "closed"]


@pytest.mark.asyncio
async def test_a_lane_exception_also_keeps_the_cursor(pulse_root, db_path, monkeypatch):
    """Same invariant via the other incompleteness route — a mid-loop raise."""

    async def boom(pr_number, *, repo, runner=None):
        raise RuntimeError("gh exploded")

    out = await _run(db_path, monkeypatch, gh=_gh([_pr(90)]), files=boom)
    assert out["status"] == "failed"
    assert (await _cursor(pulse_root))["last_merged_at"] is None


# ── The lane's OWN watermark ─────────────────────────────────────────────────
# THE DEFECT (Codex P2, #1836): the lane could be switched off independently
# while the SHARED cursor kept advancing, so every PR merged during a
# `verification_enabled: false` window fell permanently behind it — the lane
# never saw those PRs again and the one-row-per-merged-PR invariant broke for
# good, silently, because nothing reports a row that was never opened. The fix
# is a second watermark that advances only on a tick where the lane actually
# ran. These replay the three-phase sequence end-to-end, which is the only
# shape that exercises it: a single-run test cannot see a cursor divergence.


@pytest.mark.asyncio
async def test_a_pr_merged_while_the_lane_was_off_still_gets_its_row(
    pulse_root, db_path, monkeypatch
):
    """ACCEPTANCE BAR — the reported defect, replayed in all three phases."""
    cfg = dict(DEFAULTS)
    monkeypatch.setattr(rpw, "load_config", lambda: cfg)

    # Phase 1: lane ON, PR 70 merges and is recorded.
    files = _files({70: {"files": ["src/a.py"]}})
    assert (await _run(db_path, monkeypatch, gh=_gh([_pr(70)]), files=files))["status"] == "ok"
    assert [r["pr_number"] for r in await _rows(db_path)] == [70]

    # Phase 2: lane OFF. PR 71 merges. The SHARED cursor advances past it —
    # that is correct for every other lane, and is exactly what used to
    # strand this one.
    cfg["verification_enabled"] = False
    out = await _run(
        db_path, monkeypatch, gh=_gh([_pr(70), _pr(71, merged="2026-09-07T10:00:00Z")]),
        files=_files({}),
    )
    assert out["status"] == "ok"
    assert [r["pr_number"] for r in await _rows(db_path)] == [70], "lane off: no new row"
    cursor = json.loads((pulse_root / rpw.CURSOR_FILENAME).read_text())
    assert cursor["last_merged_at"] == "2026-09-07T10:00:00Z", "shared cursor advanced"
    assert cursor["verification_through"] == MERGED, "the lane's watermark did NOT"

    # Phase 3: lane back ON. VERIFY-RED: with one shared cursor, PR 71 is
    # already behind it and this run creates nothing — the permanent hole.
    cfg["verification_enabled"] = True
    files3 = _files({71: {"files": ["src/b.py"]}})
    out = await _run(
        db_path, monkeypatch,
        gh=_gh([_pr(70), _pr(71, merged="2026-09-07T10:00:00Z")]), files=files3,
    )
    assert out["status"] == "ok"
    assert [r["pr_number"] for r in await _rows(db_path)] == [70, 71], (
        "the PR merged while the lane was off must still get its row"
    )
    assert files3.calls == [71], "and PR 70 must cost no second API call"


@pytest.mark.asyncio
async def test_the_watermark_holds_when_the_lane_cannot_complete(
    pulse_root, db_path, monkeypatch
):
    """A FAILED lane must leave its watermark standing, like a disabled one.

    Same hole by a different route: the run fails and keeps the shared cursor
    today, but the watermark must not advance either, or a later successful
    run would start past the PR that failed.
    """

    async def boom(pr_number, *, repo, runner=None):
        raise RuntimeError("gh exploded")

    out = await _run(db_path, monkeypatch, gh=_gh([_pr(72)]), files=boom)
    assert out["status"] == "failed"
    cursor = json.loads((pulse_root / rpw.CURSOR_FILENAME).read_text())
    assert cursor.get("verification_through") is None
    assert await _rows(db_path) == []


@pytest.mark.asyncio
async def test_an_absent_watermark_recovers_rather_than_writing_the_gap_off(
    pulse_root, db_path, monkeypatch
):
    """First tick after this ships: the watermark is absent because the cursor
    file predates it. Absent must NOT read as 'caught up' — the PRs stranded
    by an earlier disabled window are inside the lookback and must be
    re-covered."""
    rpw._atomic_write_json(
        pulse_root / rpw.CURSOR_FILENAME,
        {"last_merged_at": "2026-09-07T10:00:00Z", "last_run_ts": None, "runs": 3},
    )
    files = _files({73: {"files": ["src/c.py"]}})
    out = await _run(
        db_path, monkeypatch, gh=_gh([_pr(73, merged="2026-09-06T10:00:00Z")]), files=files
    )
    assert out["status"] == "ok"
    assert [r["pr_number"] for r in await _rows(db_path)] == [73], (
        "a PR BEHIND the shared cursor must still be recorded on the first "
        "tick, or the deploy writes off the very gap it fixes"
    )


# ── The watermark advances only over a PROVEN-COMPLETE window ────────────────
# One invariant behind five round-2 findings: the watermark was advancing from
# whatever happened to be in hand, while every mechanism that verifies a window
# IS complete had been written for the shared cursor and knew nothing about this
# second window. These pin the invariant rather than the five ways to break it.


@pytest.mark.asyncio
async def test_a_dropped_row_stops_the_watermark(pulse_root, db_path, monkeypatch):
    """VERIFY-RED: drop `verification_window_complete` from the advance
    condition and this passes wrongly.

    `list_merged_prs` silently discards a merged PR whose `number` or
    `mergedAt` is malformed. Advancing past it strands that merge forever —
    nothing re-presents a PR the watermark has passed.
    """

    async def gh_with_dropped(**kwargs):
        return {
            "repo": REPO,
            "prs": [_pr(80)],
            "limit_hit": False,
            "dropped": 1,  # one row the listing silently lost
        }

    out = await _run(
        db_path, monkeypatch, gh=gh_with_dropped, files=_files({80: {"files": ["src/a.py"]}})
    )
    assert out["status"] == "ok"
    cursor = json.loads((pulse_root / rpw.CURSOR_FILENAME).read_text())
    assert cursor.get("verification_through") is None, (
        "an incomplete window must not advance the lane's watermark"
    )


@pytest.mark.asyncio
async def test_a_complete_window_does_advance_it(pulse_root, db_path, monkeypatch):
    """The negative control. Without it, a watermark that never advanced would
    pass the test above while breaking the lane entirely."""
    out = await _run(
        db_path, monkeypatch, gh=_gh([_pr(81)]), files=_files({81: {"files": ["src/a.py"]}})
    )
    assert out["status"] == "ok"
    cursor = json.loads((pulse_root / rpw.CURSOR_FILENAME).read_text())
    assert cursor.get("verification_through") == MERGED
    assert cursor.get("verification_repo") == REPO, "the repo is stored WITH the watermark"


@pytest.mark.asyncio
async def test_a_watermark_from_another_repository_is_discarded(
    pulse_root, db_path, monkeypatch
):
    """Obligation identity is (repo, pr_number); the watermark is a bare
    timestamp. Retarget at a fork and the old repo's timestamp would silently
    apply to the new one, omitting every merge at or before it — forever."""
    rpw._atomic_write_json(
        pulse_root / rpw.CURSOR_FILENAME,
        {
            "last_merged_at": None,
            "last_run_ts": None,
            "runs": 1,
            "verification_through": "2026-09-08T00:00:00Z",
            "verification_repo": "someone/else",
        },
    )
    files = _files({82: {"files": ["src/a.py"]}})
    out = await _run(db_path, monkeypatch, gh=_gh([_pr(82)]), files=files)
    assert out["status"] == "ok"
    assert [r["pr_number"] for r in await _rows(db_path)] == [82], (
        "a PR older than ANOTHER repo's watermark must still be recorded here"
    )


@pytest.mark.asyncio
async def test_a_newer_foreign_watermark_is_not_carried_forward(
    pulse_root, db_path, monkeypatch
):
    """VERIFY-RED: the reset on the READ side is defeated by a monotonic write.

    The stored watermark is a (repo, timestamp) pair, so both halves must agree
    about identity. Resetting the reader while `_write_cursor` still took
    `max(prior, new)` carried a FOREIGN repository's timestamp forward whenever
    it happened to be newer — which is exactly what the reset exists to prevent
    (CodeRabbit Major, PR #1836). Fixing one side of a pair is not fixing it.
    """
    rpw._atomic_write_json(
        pulse_root / rpw.CURSOR_FILENAME,
        {
            "last_merged_at": None,
            "last_run_ts": None,
            "runs": 1,
            # FUTURE relative to this run's PR, so a max() would keep it.
            "verification_through": "2099-01-01T00:00:00Z",
            "verification_repo": "someone/else",
        },
    )
    out = await _run(
        db_path, monkeypatch, gh=_gh([_pr(83)]), files=_files({83: {"files": ["src/a.py"]}})
    )
    assert out["status"] == "ok"
    cursor = json.loads((pulse_root / rpw.CURSOR_FILENAME).read_text())
    assert cursor["verification_through"] is None, (
        "a reset tick CLEARS the watermark — it does not advance it, because the "
        "window it fetched came from the OLD repository's cursor and may be "
        "narrower than the lookback"
    )
    assert cursor["verification_repo"] == REPO, "and it records whose repo it now is"

    # THE RECOVERY, which is the half that matters: the next tick derives its
    # window from the plain lookback and re-covers, with no API call and no
    # operator action.
    files2 = _files({84: {"files": ["src/b.py"]}})
    out2 = await _run(db_path, monkeypatch, gh=_gh([_pr(84)]), files=files2)
    assert out2["status"] == "ok"
    cursor2 = json.loads((pulse_root / rpw.CURSOR_FILENAME).read_text())
    assert cursor2["verification_through"] == MERGED, "the watermark advances again"
