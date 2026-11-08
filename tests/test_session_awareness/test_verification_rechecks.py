"""Closed-row rechecks: storage, upgrade, due state and retention."""

import json

import aiosqlite
import pytest

from genesis.db.crud import pr_verifications as verif_crud
from tests.test_session_awareness.conftest import PR_VERIFICATION_MIGRATIONS
from tests.test_session_awareness.conftest import build_pr_verifications as _build
from tests.test_session_awareness.test_pr_verifications import (
    MIG,
    MIG_VERDICT,
    NOW,
    OLD,
    REPO,
    _open,
    _row,
)
from tests.test_session_awareness.test_pr_verifications import (
    db as db,
)

MIG_RECHECKS = PR_VERIFICATION_MIGRATIONS[2]


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["deep-root", "deep-extra", "deep-history-extra"])
async def test_deep_corruption_never_hides_neighbors_or_aborts_retention(db, shape):
    await _closed(db, 167, None, closed_at=OLD)
    await _closed(db, 168, [EVENT], closed_at=OLD)
    await _closed(db, 169, None, closed_at=OLD)
    nested = "[" * 1100 + "0" + "]" * 1100
    entry = {**EVENT, "results": [], "done_at": None}
    if shape == "deep-root":
        raw = "[" * 10000 + "0" + "]" * 10000
    elif shape == "deep-extra":
        raw = "[" + json.dumps(entry)[:-1] + ', "extra":' + nested + "}]"
    else:
        entry["results"] = [{"at": NOW, "note": "n", "result": "failed", "extra": "DEPTH"}]
        raw = json.dumps([entry]).replace('"DEPTH"', nested)
    await db.execute("UPDATE pr_verifications SET rechecks = ? WHERE pr_number = 167", (raw,))
    await db.commit()
    due = await verif_crud.list_rechecks_due(db, today=TODAY)
    assert [(r["pr_number"], r["malformed"]) for r in due] == [(167, True), (168, False)]
    assert await verif_crud.recheck_state(db, repo=REPO, pr_number=167, index=0) in ("not-json", "bad-entry")
    assert await verif_crud.append_recheck_result(
        db, repo=REPO, pr_number=167, index=0, result="done", note="n", now=NOW
    ) in ("not-json", "bad-entry")
    assert await verif_crud.prune_closed(db, older_than_days=1, now=NOW) == 1
    assert (await _row(db, 167))["rechecks"] == raw
    assert await _row(db, 169) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["trigger", "results", "done_at", "history_note"])
async def test_duplicate_stored_keys_cannot_disagree_with_sqlite(db, key):
    await _closed(db, 165, None, closed_at=OLD)
    entry = {**EVENT, "results": [], "done_at": None}
    if key == "history_note":
        entry["results"] = [{"at": NOW, "result": "failed", "note": "probe"}]
        raw = (
            "["
            + json.dumps(entry).replace('"note": "probe"', '"note": "probe", "note": "probe"')
            + "]"
        )
    else:
        raw = (
            "["
            + json.dumps(entry)[:-1]
            + ","
            + json.dumps(key)
            + ":"
            + json.dumps(entry[key])
            + "}]"
        )
    await db.execute("UPDATE pr_verifications SET rechecks = ? WHERE pr_number = 165", (raw,))
    await db.commit()
    assert (await verif_crud.list_rechecks_due(db, today=TODAY))[0]["malformed"] is True
    assert (
        await verif_crud.append_recheck_result(
            db, repo=REPO, pr_number=165, index=0, result="failed", note="new", now=NOW
        )
        == "not-json"
    )
    assert await verif_crud.prune_closed(db, older_than_days=1, now=NOW) == 0
    assert (await _row(db, 165))["rechecks"] == raw


@pytest.mark.asyncio
async def test_stored_padded_future_trigger_uses_normalized_date(db):
    await _closed(db, 166, None)
    raw = json.dumps([{**FUTURE, "trigger": " 2026-12-31 ", "results": [], "done_at": None}])
    await db.execute("UPDATE pr_verifications SET rechecks = ? WHERE pr_number = 166", (raw,))
    await db.commit()
    assert await verif_crud.list_rechecks_due(db, today=TODAY) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("index", [2**31, 2**32, 2**32 + 1, 2**64, 10**100])
async def test_oversized_indices_never_wrap_to_another_recheck(db, index):
    await _closed(db, 160, [EVENT, PAST])
    before = await _row(db, 160)
    assert (
        await verif_crud.recheck_state(db, repo=REPO, pr_number=160, index=index) == "out-of-range"
    )
    assert (
        await verif_crud.append_recheck_result(
            db, repo=REPO, pr_number=160, index=index, result="done", note="probe", now=NOW
        )
        == "out-of-range"
    )
    assert await _row(db, 160) == before


@pytest.mark.asyncio
async def test_existing_close_callers_capture_typed_gaps_after_migration(db):
    await _open(db, 161)
    assert await verif_crud.close_verification(
        db,
        repo=REPO,
        pr_number=161,
        verdict="pass-with-measured-gaps",
        reason="r",
        evidence=json.dumps({"gaps": [{"kind": "event_gated", **EVENT}]}),
        now=NOW,
    )
    assert await _rechecks(db, 161) == [{**EVENT, "results": [], "done_at": None}]


@pytest.mark.asyncio
async def test_existing_close_callers_refuse_typed_gaps_before_migration(tmp_path):
    async with aiosqlite.connect(tmp_path / "old.db") as conn:
        conn.row_factory = aiosqlite.Row
        await MIG.up(conn)
        await MIG_VERDICT.up(conn)
        await _open(conn, 162)
        before = await _row(conn, 162)
        assert not await verif_crud.close_verification(
            conn,
            repo=REPO,
            pr_number=162,
            verdict="pass-with-measured-gaps",
            reason="r",
            evidence=json.dumps({"gaps": [{"kind": "event_gated", **EVENT}]}),
            now=NOW,
        )
        assert await _row(conn, 162) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        {"results": ["bad"]},
        {"results": [{}]},
        {"results": None},
        {"results": [{"at": NOW, "note": "n", "result": "unknown"}]},
        {"results": [{"at": 0, "note": "n", "result": "failed"}]},
        {"done_at": ""},
        {"done_at": 0},
        {"recheck": None},
        {"trigger": "2026-02-30"},
    ],
)
async def test_malformed_state_is_visible_unchanged_and_never_pruned(db, bad):
    await _closed(db, 163, None, closed_at=OLD)
    await _closed(db, 164, [EVENT], closed_at=OLD)
    raw = json.dumps([{**EVENT, "results": [], "done_at": None, **bad}])
    await db.execute("UPDATE pr_verifications SET rechecks = ? WHERE pr_number = 163", (raw,))
    await db.commit()
    due = await verif_crud.list_rechecks_due(db, today=TODAY)
    assert [(r["pr_number"], r["malformed"]) for r in due] == [(163, True), (164, False)]
    assert (
        await verif_crud.append_recheck_result(
            db, repo=REPO, pr_number=163, index=0, result="done", note="probe", now=NOW
        )
        == "bad-entry"
    )
    assert await verif_crud.prune_closed(db, older_than_days=1, now=NOW) == 0
    assert (await _row(db, 163))["rechecks"] == raw


TODAY = "2026-10-27"
EVENT = {"trigger": "the next weekly run", "recheck": "journalctl -n 20", "detail": "job"}
PAST = {"trigger": "2026-10-20", "recheck": "sqlite3 -readonly x 'select 1'", "detail": "a"}
FUTURE = {"trigger": "2026-12-31", "recheck": "echo later", "detail": "b"}


async def _closed(db, n, rechecks, *, closed_at=NOW):
    await _open(db, n)
    assert await verif_crud.close_verification(
        db,
        repo=REPO,
        pr_number=n,
        verdict="pass-with-measured-gaps",
        reason="r",
        evidence="{}",
        now=closed_at,
        rechecks=rechecks,
    )


async def _rechecks(db, n):
    import json

    raw = (await _row(db, n))["rechecks"]
    return None if raw is None else json.loads(raw)


@pytest.mark.asyncio
async def test_close_stores_rechecks_unfinished_in_the_same_write(db):
    await _closed(db, 110, [EVENT, PAST])
    stored = await _rechecks(db, 110)
    assert stored == [
        {**EVENT, "results": [], "done_at": None},
        {**PAST, "results": [], "done_at": None},
    ]
    assert (await _row(db, 110))["status"] == "closed"


@pytest.mark.asyncio
async def test_a_close_without_rechecks_leaves_the_column_NULL(db):
    await _closed(db, 111, None)
    assert (await _row(db, 111))["rechecks"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [[{"trigger": "x", "recheck": ""}], ["not a dict"]])
async def test_close_refuses_a_recheck_missing_its_fields(db, bad):
    await _open(db, 112)
    with pytest.raises(ValueError, match="a recheck needs"):
        await verif_crud.close_verification(
            db,
            repo=REPO,
            pr_number=112,
            verdict="pass-mechanical",
            reason="r",
            evidence=None,
            now=NOW,
            rechecks=bad,
        )
    assert (await _row(db, 112))["status"] == "open", "nothing written"


@pytest.mark.asyncio
async def test_the_rechecks_guard_is_separate_from_the_verdict_guard(tmp_path):
    """Verdict columns present, rechecks absent: the ordinary state between this
    deploy and migration. Ordinary closes work; owed rechecks refuse the close."""
    async with aiosqlite.connect(str(tmp_path / "window.db")) as conn:
        conn.row_factory = aiosqlite.Row
        await MIG.up(conn)
        await MIG_VERDICT.up(conn)
        await conn.commit()
        assert await verif_crud._verdict_columns_available(conn)
        assert not await verif_crud._rechecks_column_available(conn)
        await verif_crud.open_verification(
            conn, repo=REPO, pr_number=1, pr_title="t", merged_at=NOW, now=NOW
        )
        assert not await verif_crud.close_verification(
            conn,
            repo=REPO,
            pr_number=1,
            verdict="pass-with-measured-gaps",
            reason="r",
            evidence=None,
            now=NOW,
            rechecks=[EVENT],
        )
        assert (await _row(conn, 1))["status"] == "open"
        assert await verif_crud.close_verification(
            conn,
            repo=REPO,
            pr_number=1,
            verdict="pass-mechanical",
            reason="r",
            evidence=None,
            now=NOW,
        )
        assert await verif_crud.list_rechecks_due(conn, today=TODAY) == []
        assert (
            await verif_crud.append_recheck_result(
                conn, repo=REPO, pr_number=1, index=0, result="done", note="n", now=NOW
            )
            == "unavailable"
        )
        assert await verif_crud.prune_closed(conn, now="2027-12-01T00:00:00+00:00") == 1


@pytest.mark.asyncio
async def test_the_due_filter_is_date_on_or_before_today_and_every_event(db):
    """A date trigger is due once it is today or past; a named event cannot be judged
    by a query, so it is always listed. Oldest close first."""
    today_trigger = {**PAST, "trigger": TODAY, "detail": "today"}
    await _closed(db, 120, [FUTURE, PAST, EVENT], closed_at="2026-10-02T00:00:00+00:00")
    await _closed(db, 121, [today_trigger], closed_at="2026-10-01T00:00:00+00:00")
    await _closed(db, 122, None)
    due = await verif_crud.list_rechecks_due(db, today=TODAY)
    assert [(r["pr_number"], r["index"]) for r in due] == [(121, 0), (120, 1), (120, 2)]
    assert due[0]["trigger"] == TODAY and due[2]["trigger"] == EVENT["trigger"]
    assert all(r["attempts"] == 0 and r["latest"] is None for r in due)
    assert due[1]["recheck"] == PAST["recheck"]


@pytest.mark.asyncio
async def test_the_due_filter_drops_a_finished_recheck_and_shows_a_failed_one(db):
    await _closed(db, 125, [PAST, EVENT])
    assert (
        await verif_crud.append_recheck_result(
            db, repo=REPO, pr_number=125, index=0, result="failed", note="2 of 5", now=NOW
        )
        == "recorded"
    )
    assert (
        await verif_crud.append_recheck_result(
            db, repo=REPO, pr_number=125, index=1, result="done", note="ran", now=NOW
        )
        == "recorded"
    )
    due = await verif_crud.list_rechecks_due(db, today=TODAY)
    assert [r["index"] for r in due] == [0], "done is finished; failed stays due"
    assert due[0]["attempts"] == 1 and due[0]["latest"]["result"] == "failed"


@pytest.mark.asyncio
async def test_a_malformed_rechecks_column_is_listed_not_hidden(db):
    await _closed(db, 126, None)
    await db.execute("UPDATE pr_verifications SET rechecks = 'not json' WHERE pr_number = 126")
    await db.commit()
    due = await verif_crud.list_rechecks_due(db, today=TODAY)
    assert [(r["pr_number"], r["index"], r["malformed"]) for r in due] == [(126, None, True)]


@pytest.mark.asyncio
async def test_a_done_result_finishes_the_recheck_and_NEVER_reopens_the_row(db):
    await _closed(db, 130, [EVENT])
    before = await _row(db, 130)
    out = await verif_crud.append_recheck_result(
        db, repo=REPO, pr_number=130, index=0, result="done", note="ran it: 7/7", now=OLD
    )
    assert out == "recorded"
    after = await _row(db, 130)
    for col in ("status", "verdict", "closed_at", "closed_reason", "evidence", "attempt_count"):
        assert after[col] == before[col], f"{col} must not change — the row stays closed"
    (recheck,) = await _rechecks(db, 130)
    assert recheck["done_at"] == OLD
    assert recheck["results"] == [{"at": OLD, "result": "done", "note": "ran it: 7/7"}]


@pytest.mark.asyncio
async def test_a_failed_result_is_appended_and_leaves_the_recheck_unfinished(db):
    await _closed(db, 131, [EVENT])
    for note in ("first: 2 of 5", "second: 4 of 5"):
        assert (
            await verif_crud.append_recheck_result(
                db, repo=REPO, pr_number=131, index=0, result="failed", note=note, now=NOW
            )
            == "recorded"
        )
    (recheck,) = await _rechecks(db, 131)
    assert recheck["done_at"] is None
    assert [r["note"] for r in recheck["results"]] == ["first: 2 of 5", "second: 4 of 5"]
    assert (await _row(db, 131))["status"] == "closed"


@pytest.mark.asyncio
async def test_a_finished_recheck_refuses_another_result(db):
    await _closed(db, 132, [EVENT])
    await verif_crud.append_recheck_result(
        db, repo=REPO, pr_number=132, index=0, result="done", note="ran", now=NOW
    )
    out = await verif_crud.append_recheck_result(
        db, repo=REPO, pr_number=132, index=0, result="failed", note="late", now=NOW
    )
    assert out == "already-done"
    (recheck,) = await _rechecks(db, 132)
    assert len(recheck["results"]) == 1, "nothing appended after done"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("setup", "pr", "index", "expect"),
    [
        ("closed", 133, 1, "out-of-range"),
        ("closed", 133, 99, "out-of-range"),
        ("null", 133, 0, "no-rechecks"),
        ("open", 133, 0, "missing"),
        ("closed", 9999, 0, "missing"),
    ],
)
async def test_an_append_that_matches_nothing_names_its_cause(db, setup, pr, index, expect):
    if setup == "open":
        await _open(db, 133)
    else:
        await _closed(db, 133, [EVENT] if setup == "closed" else None)
    out = await verif_crud.append_recheck_result(
        db, repo=REPO, pr_number=pr, index=index, result="done", note="n", now=NOW
    )
    assert out == expect
    assert await verif_crud.recheck_state(db, repo=REPO, pr_number=pr, index=index) == expect


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"result": "passed"}, "result must be one of"),
        ({"note": "   "}, "non-empty note"),
        ({"index": -1}, "non-negative"),
        ({"index": True}, "non-negative"),
    ],
)
async def test_append_refuses_a_malformed_call(db, kwargs, match):
    await _closed(db, 134, [EVENT])
    call = {"repo": REPO, "pr_number": 134, "index": 0, "result": "done", "note": "n"}
    with pytest.raises(ValueError, match=match):
        await verif_crud.append_recheck_result(db, **{**call, **kwargs}, now=NOW)


@pytest.mark.asyncio
async def test_concurrent_appends_both_land(tmp_path):
    """Only recorded acknowledges a result; a raced writer re-reads and retries."""
    import asyncio

    path = tmp_path / "race.db"
    async with aiosqlite.connect(str(path)) as conn:
        conn.row_factory = aiosqlite.Row
        await _build(conn)
        await conn.commit()
        await _closed(conn, 135, [EVENT])

    async def append(note):
        async with aiosqlite.connect(str(path), timeout=10) as c:
            await c.execute("PRAGMA busy_timeout=5000")
            for _ in range(8):
                out = await verif_crud.append_recheck_result(
                    c, repo=REPO, pr_number=135, index=0, result="failed", note=note, now=NOW
                )
                if out != "raced":
                    return out
            pytest.fail("writer never obtained a stable recheck snapshot")

    outs = await asyncio.gather(*(append(f"writer {i}") for i in range(4)))
    assert outs == ["recorded"] * 4
    async with aiosqlite.connect(str(path)) as conn:
        conn.row_factory = aiosqlite.Row
        (recheck,) = await _rechecks(conn, 135)
    assert sorted(r["note"] for r in recheck["results"]) == [f"writer {i}" for i in range(4)]


@pytest.mark.asyncio
async def test_prune_keeps_an_old_closed_row_that_still_owes_a_recheck(db):
    """Pruning it would forgive the owed measurement silently — the open-row rule,
    one column over. A row whose rechecks are all done prunes like any other."""
    await _closed(db, 140, [EVENT], closed_at=OLD)
    await _closed(db, 141, [EVENT], closed_at=OLD)
    await verif_crud.append_recheck_result(
        db, repo=REPO, pr_number=141, index=0, result="done", note="ran", now=OLD
    )
    await _closed(db, 142, [EVENT, PAST], closed_at=OLD)
    await verif_crud.append_recheck_result(
        db, repo=REPO, pr_number=142, index=0, result="done", note="ran", now=OLD
    )
    await _closed(db, 143, None, closed_at=OLD)
    assert await verif_crud.prune_closed(db, older_than_days=180, now=NOW) == 2
    assert await _row(db, 140) is not None, "unfinished recheck — kept"
    assert await _row(db, 142) is not None, "one of two unfinished — kept"
    assert await _row(db, 141) is None and await _row(db, 143) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["not json", '{"a": 1', '["a string element"]', "[1, null]"])
async def test_prune_never_fails_or_deletes_on_an_unreadable_rechecks_column(db, raw):
    await _closed(db, 144, None, closed_at=OLD)
    await _closed(db, 145, None, closed_at=OLD)
    await db.execute("UPDATE pr_verifications SET rechecks = ? WHERE pr_number = 144", (raw,))
    await db.commit()
    assert await verif_crud.prune_closed(db, older_than_days=180, now=NOW) == 1
    assert await _row(db, 144) is not None, "unreadable — kept, never deleted"
    assert await _row(db, 145) is None, "the rest of the prune still ran"


@pytest.mark.asyncio
async def test_the_rechecks_migration_adds_one_NULL_column_and_is_idempotent(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "legacy.db")) as conn:
        conn.row_factory = aiosqlite.Row
        await MIG.up(conn)
        await MIG_VERDICT.up(conn)
        await verif_crud.open_verification(
            conn, repo=REPO, pr_number=1, pr_title="t", merged_at=OLD, now=OLD
        )
        await MIG_RECHECKS.up(conn)
        await MIG_RECHECKS.up(conn)
        cur = await conn.execute("SELECT rechecks FROM pr_verifications")
        assert (await cur.fetchone())["rechecks"] is None, "no backfill"


@pytest.mark.asyncio
async def test_a_list_with_any_non_object_entry_is_listed_once_as_malformed(db):
    """Skipping only the bad entry would hide the row's other rechecks behind a list
    nothing can append to; listing it once keeps the row visible."""
    await _closed(db, 150, None)
    await db.execute(
        "UPDATE pr_verifications SET rechecks = ? WHERE pr_number = 150",
        ('[{"trigger": "x", "recheck": "y", "detail": "z", "results": [], "done_at": null}, 7]',),
    )
    await db.commit()
    due = await verif_crud.list_rechecks_due(db, today=TODAY)
    assert [(r["pr_number"], r["index"], r["malformed"]) for r in due] == [(150, None, True)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "index", "expect"),
    [
        ("[]", 0, "no-rechecks"),
        ("not json", 0, "not-json"),
        ('{"a": 1}', 0, "bad-entry"),
        ('["a string"]', 0, "bad-entry"),
        ('[{"results": 5, "done_at": null}]', 0, "bad-entry"),
        ('[{"results": [], "done_at": null}]', 1, "bad-entry"),
    ],
)
async def test_recheck_state_separates_every_unreadable_shape(db, raw, index, expect):
    await _closed(db, 151, None)
    await db.execute("UPDATE pr_verifications SET rechecks = ? WHERE pr_number = 151", (raw,))
    await db.commit()
    assert await verif_crud.recheck_state(db, repo=REPO, pr_number=151, index=index) == expect
    out = await verif_crud.append_recheck_result(
        db, repo=REPO, pr_number=151, index=index, result="done", note="n", now=NOW
    )
    assert out == expect, "the append names the same cause and writes nothing"


_TYPED = (
    '{"gaps": [{"kind": "method", "detail": "m"}, {"kind": "event_gated", "detail": "d", '
    '"trigger": "2026-11-02", "recheck": "journalctl -n 5"}, {"kind": "event_gated", '
    '"detail": "e", "trigger": "next deploy"}]}'
)
_UNTYPED = '{"scope_limits": ["the weekly job had not run yet"]}'


@pytest.mark.asyncio
async def test_the_migration_backfills_TYPED_event_gaps_and_leaves_untyped_NULL(tmp_path):
    """A typed document carries its event-gated gaps as structure, so they are copied
    across unfinished; an untyped one never chose a kind, so it is left alone. An
    incomplete entry (no recheck) and malformed evidence are skipped, never fatal."""
    async with aiosqlite.connect(str(tmp_path / "upgrade.db")) as conn:
        conn.row_factory = aiosqlite.Row
        await MIG.up(conn)
        await MIG_VERDICT.up(conn)
        for n, evidence in ((1, _TYPED), (2, _UNTYPED), (3, "not json"), (4, None)):
            await verif_crud.open_verification(
                conn, repo=REPO, pr_number=n, pr_title="t", merged_at=OLD, now=OLD
            )
            await verif_crud.close_verification(
                conn,
                repo=REPO,
                pr_number=n,
                verdict="pass-with-measured-gaps",
                reason="r",
                evidence=None,
                now=OLD,
            )
            # Model legacy rows directly: the current close now refuses typed
            # rechecks before migration, including malformed declarations.
            await conn.execute(
                "UPDATE pr_verifications SET evidence = ? WHERE pr_number = ?",
                (evidence, n),
            )
        await verif_crud.open_verification(
            conn, repo=REPO, pr_number=5, pr_title="t", merged_at=OLD, now=OLD
        )
        await conn.execute(
            "UPDATE pr_verifications SET evidence = ? WHERE pr_number = 5", (_TYPED,)
        )
        await MIG_RECHECKS.up(conn)
        await MIG_RECHECKS.up(conn)  # idempotent: a second run changes nothing
        await conn.commit()
        import json

        cur = await conn.execute("SELECT pr_number, rechecks FROM pr_verifications")
        got = {r["pr_number"]: r["rechecks"] for r in await cur.fetchall()}
    assert json.loads(got[1]) == [
        {
            "trigger": "2026-11-02",
            "recheck": "journalctl -n 5",
            "detail": "d",
            "results": [],
            "done_at": None,
        }
    ]
    assert got[2] is None, "untyped prose is never mapped to a kind"
    assert got[3] is None and got[4] is None
    assert got[5] is None, "an OPEN row owes no recheck"
