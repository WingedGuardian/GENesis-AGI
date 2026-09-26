"""pr_verifications crud + the two-build-path schema parity (issue #1718 half B).

The invariants: (repo, pr_number) is unique at the SCHEMA level, so re-observing
a merged PR can never duplicate; a docs-only row is born closed with its reason;
a closed row never flips back and never has its record overwritten; OPEN rows
are never pruned — an open row IS the obligation; and the fresh-install DDL
(``schema/_tables.py``) and the migration produce the SAME table, because two
build paths that drift is two schemas diverging with nothing to notice.
"""

from __future__ import annotations

import re

import aiosqlite
import pytest

from genesis.db.crud import pr_verifications as verif_crud
from genesis.db.schema._tables import INDEXES, TABLES
from tests.test_session_awareness.conftest import (  # noqa: E402
    PR_VERIFICATION_MIGRATIONS,
)
from tests.test_session_awareness.conftest import (
    build_pr_verifications as _build,
)

#: The FIRST migration only — for the two tests that must observe the
#: pre-verdict-column world deliberately. Everything else goes through
#: ``_build`` so a new migration reaches every suite at once (see the conftest).
MIG = PR_VERIFICATION_MIGRATIONS[0]
MIG_VERDICT = PR_VERIFICATION_MIGRATIONS[1]

NOW = "2026-09-06T23:00:00+00:00"
OLD = "2026-01-01T00:00:00+00:00"
REPO = "owner/repo"


@pytest.fixture
async def db(tmp_path):
    path = tmp_path / "genesis.db"
    async with aiosqlite.connect(str(path)) as conn:
        conn.row_factory = aiosqlite.Row
        await _build(conn)
        await conn.commit()
        yield conn


@pytest.fixture
async def bare_db(tmp_path):
    """No migration — the pre-migration subprocess window."""
    async with aiosqlite.connect(str(tmp_path / "bare.db")) as conn:
        conn.row_factory = aiosqlite.Row
        yield conn


async def _row(db, pr_number):
    cur = await db.execute(
        "SELECT * FROM pr_verifications WHERE repo = ? AND pr_number = ?", (REPO, pr_number)
    )
    r = await cur.fetchone()
    return dict(r) if r else None


# ── open_verification ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_open_creates_an_open_row(db):
    out = await verif_crud.open_verification(
        db, repo=REPO, pr_number=7, pr_title="feat: x", merged_at="2026-09-06T10:00:00Z", now=NOW
    )
    assert out == "created"
    row = await _row(db, 7)
    assert row["status"] == "open"
    assert row["closed_reason"] is None and row["closed_at"] is None
    assert row["created_at"] == NOW


@pytest.mark.asyncio
async def test_docs_only_row_is_born_closed_with_the_reason(db):
    out = await verif_crud.open_verification(
        db,
        repo=REPO,
        pr_number=8,
        pr_title="docs: y",
        merged_at="2026-09-06T10:00:00Z",
        now=NOW,
        closed_reason="docs-only diff (2 path(s)) — deterministic exemption, no runtime surface",
    )
    assert out == "created"
    row = await _row(db, 8)
    assert row["status"] == "closed"
    assert "docs-only" in row["closed_reason"]
    assert row["closed_at"] == NOW


@pytest.mark.asyncio
async def test_reobserving_a_pr_is_absorbed_not_duplicated(db):
    """The dedup is the SCHEMA (unique index + INSERT OR IGNORE) — a re-covered
    enumeration window, or two workers racing, cannot create a second row, and
    the second write reports 'exists' so a lane never counts it as new."""
    await verif_crud.open_verification(
        db, repo=REPO, pr_number=9, pr_title="a", merged_at="2026-09-06T10:00:00Z", now=NOW
    )
    out = await verif_crud.open_verification(
        db, repo=REPO, pr_number=9, pr_title="a", merged_at="2026-09-06T10:00:00Z", now=NOW
    )
    assert out == "exists"
    cur = await db.execute(
        "SELECT COUNT(*) FROM pr_verifications WHERE repo = ? AND pr_number = 9", (REPO,)
    )
    assert (await cur.fetchone())[0] == 1


@pytest.mark.asyncio
async def test_a_closed_row_is_not_reopened_by_a_late_open(db):
    """Window re-coverage after a validator verified the PR must not resurrect
    the obligation — the second write is ignored whatever status the row holds."""
    await verif_crud.open_verification(
        db, repo=REPO, pr_number=10, pr_title="a", merged_at="2026-09-06T10:00:00Z", now=NOW
    )
    assert await verif_crud.close_verification(
        db, repo=REPO, pr_number=10, verdict="pass-mechanical", reason="verified",
        evidence="ran the E2E", now=NOW
    )
    out = await verif_crud.open_verification(
        db, repo=REPO, pr_number=10, pr_title="a", merged_at="2026-09-06T10:00:00Z", now=NOW
    )
    assert out == "exists"
    assert (await _row(db, 10))["status"] == "closed"


@pytest.mark.asyncio
async def test_same_pr_number_in_another_repo_is_a_distinct_obligation(db):
    """The key is (repo, pr_number), not pr_number — a fork or rename must not
    alias two different PRs onto one row."""
    a = await verif_crud.open_verification(
        db, repo=REPO, pr_number=11, pr_title="a", merged_at="2026-09-06T10:00:00Z", now=NOW
    )
    b = await verif_crud.open_verification(
        db, repo="other/repo", pr_number=11, pr_title="b", merged_at="2026-09-06T10:00:00Z", now=NOW
    )
    assert (a, b) == ("created", "created")


# ── close_verification ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_close_records_evidence_and_only_touches_open_rows(db):
    await verif_crud.open_verification(
        db, repo=REPO, pr_number=12, pr_title="a", merged_at="2026-09-06T10:00:00Z", now=NOW
    )
    assert await verif_crud.close_verification(
        db, repo=REPO, pr_number=12, verdict="pass-mechanical", reason="verified",
        evidence="health 200", now=NOW
    )
    row = await _row(db, 12)
    assert (row["status"], row["evidence"]) == ("closed", "health 200")
    # A second close must not overwrite the record — two validators cannot fight.
    assert not await verif_crud.close_verification(
        db, repo=REPO, pr_number=12, verdict="pass-mechanical", reason="other",
        evidence="other", now=NOW
    )
    assert (await _row(db, 12))["evidence"] == "health 200"


# ── the four verdicts: who closes, who parks ─────────────────────────────


async def _open(db, n, merged="2026-09-06T10:00:00Z"):
    await verif_crud.open_verification(
        db, repo=REPO, pr_number=n, pr_title="t", merged_at=merged, now=NOW
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", verif_crud.PASS_VERDICTS)
async def test_a_pass_closes_the_row_and_records_its_verdict(db, verdict):
    """The verdict lands in the SAME write that moves status, so no reader can
    ever observe one without the other."""
    await _open(db, 50)
    assert await verif_crud.close_verification(
        db, repo=REPO, pr_number=50, verdict=verdict, reason="r", evidence="{}", now=NOW
    )
    row = await _row(db, 50)
    assert row["status"] == "closed"
    assert row["verdict"] == verdict
    assert row["attempt_count"] == 1, "a clean verification is an attempt too"
    assert row["last_attempt_at"] == NOW


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["fail-intent", "cannot-verify"])
async def test_close_refuses_a_non_closing_verdict(db, verdict):
    """These two do not discharge the obligation, so the closer must not take
    them — one writer per state transition."""
    await _open(db, 51)
    with pytest.raises(ValueError, match="do not discharge"):
        await verif_crud.close_verification(
            db, repo=REPO, pr_number=51, verdict=verdict, reason="r", evidence=None, now=NOW
        )
    assert (await _row(db, 51))["status"] == "open", "the row must be untouched"


@pytest.mark.asyncio
async def test_close_refuses_an_unknown_verdict(db):
    await _open(db, 52)
    with pytest.raises(ValueError, match="accepts only"):
        await verif_crud.close_verification(
            db, repo=REPO, pr_number=52, verdict="PASS", reason="r", evidence=None, now=NOW
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["fail-intent", "cannot-verify"])
async def test_record_attempt_leaves_the_row_open_with_its_reason(db, verdict):
    await _open(db, 53)
    out = await verif_crud.record_attempt(
        db, repo=REPO, pr_number=53, verdict=verdict, note="needs another box", now=NOW
    )
    assert out == "recorded"
    row = await _row(db, 53)
    assert row["status"] == "open", "the obligation is NOT discharged"
    assert row["verdict"] == verdict
    assert row["last_attempt_note"] == "needs another box"
    assert row["last_attempt_at"] == NOW
    assert row["attempt_count"] == 1
    assert row["closed_reason"] is None and row["closed_at"] is None


@pytest.mark.asyncio
async def test_record_attempt_is_last_wins_and_counts_every_try(db):
    """One note, overwritten — the owner's requirement is singular. The counter
    is the one fact an overwrite destroys, which is why it exists."""
    await _open(db, 54)
    for note in ("first reason", "second reason"):
        await verif_crud.record_attempt(
            db, repo=REPO, pr_number=54, verdict="cannot-verify", note=note, now=NOW
        )
    row = await _row(db, 54)
    assert row["last_attempt_note"] == "second reason"
    assert row["attempt_count"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", verif_crud.PASS_VERDICTS)
async def test_record_attempt_refuses_a_pass(db, verdict):
    await _open(db, 55)
    with pytest.raises(ValueError, match="discharges the obligation"):
        await verif_crud.record_attempt(
            db, repo=REPO, pr_number=55, verdict=verdict, note="n", now=NOW
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "   ", "\n\t "])
async def test_record_attempt_refuses_an_empty_note(db, bad):
    """"Attempted but could not finish" with no stated cause is the unverifiable
    claim in permanent record that this guard exists to refuse."""
    await _open(db, 56)
    with pytest.raises(ValueError, match="non-empty note"):
        await verif_crud.record_attempt(
            db, repo=REPO, pr_number=56, verdict="cannot-verify", note=bad, now=NOW
        )
    assert (await _row(db, 56))["attempt_count"] == 0, "no partial write"


@pytest.mark.asyncio
async def test_record_attempt_cannot_annotate_a_closed_row(db):
    """A discharged obligation is a decision already made — the same rule the
    closer carries, enforced by WHERE status='open' rather than by convention."""
    await _open(db, 57)
    await verif_crud.close_verification(
        db, repo=REPO, pr_number=57, verdict="pass-mechanical", reason="r", evidence=None, now=NOW
    )
    out = await verif_crud.record_attempt(
        db, repo=REPO, pr_number=57, verdict="cannot-verify", note="late", now=NOW
    )
    assert out == "missing"
    row = await _row(db, 57)
    assert row["verdict"] == "pass-mechanical", "the terminal verdict is not overwritten"
    assert row["last_attempt_note"] is None


@pytest.mark.asyncio
async def test_record_attempt_on_an_unknown_pr_reports_missing(db):
    out = await verif_crud.record_attempt(
        db, repo=REPO, pr_number=9999, verdict="cannot-verify", note="n", now=NOW
    )
    assert out == "missing"


@pytest.mark.asyncio
async def test_the_pass_iff_closed_invariant_holds_across_both_writers(db):
    """The invariant the migration docstring claims, asserted against the only
    two writers that can set a verdict — because an adversarial review showed a
    cross-column invariant cannot be held by writers that ignore each other.

    Drive every verdict through its own writer, then assert the biconditional
    over the whole table rather than per row.
    """
    await _open(db, 60)
    await verif_crud.close_verification(
        db, repo=REPO, pr_number=60, verdict="pass-mechanical", reason="r", evidence=None, now=NOW
    )
    await _open(db, 61)
    await verif_crud.close_verification(
        db,
        repo=REPO,
        pr_number=61,
        verdict="pass-with-measured-gaps",
        reason="r",
        evidence=None,
        now=NOW,
    )
    await _open(db, 62)
    await verif_crud.record_attempt(
        db, repo=REPO, pr_number=62, verdict="fail-intent", note="broken", now=NOW
    )
    await _open(db, 63)
    await verif_crud.record_attempt(
        db, repo=REPO, pr_number=63, verdict="cannot-verify", note="no box", now=NOW
    )
    await _open(db, 64)  # never attempted

    cur = await db.execute("SELECT pr_number, status, verdict FROM pr_verifications")
    for row in await cur.fetchall():
        is_pass = row["verdict"] in verif_crud.PASS_VERDICTS
        is_closed = row["status"] == "closed"
        assert is_pass == is_closed, (
            f"PR {row['pr_number']}: verdict={row['verdict']!r} status={row['status']!r} "
            f"violates 'a PASS verdict exists iff the row is closed'"
        )


# ── readers ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_open_puts_never_attempted_before_parked_rows(db):
    """Head-of-line blocking, caught by review: a cannot-verify row that cannot
    be discharged on this install is typically the OLDEST, so a pure
    oldest-first sort parks it permanently at the head of a capped window and
    starves the work a validator could actually do."""
    await _open(db, 70, merged="2026-09-01T00:00:00Z")  # oldest, will be parked
    await _open(db, 71, merged="2026-09-05T00:00:00Z")
    await _open(db, 72, merged="2026-09-03T00:00:00Z")
    await verif_crud.record_attempt(
        db, repo=REPO, pr_number=70, verdict="cannot-verify", note="needs another install", now=NOW
    )
    rows = await verif_crud.list_open(db)
    assert [r["pr_number"] for r in rows] == [72, 71, 70], (
        "never-attempted first (oldest-first within the group), parked last"
    )


@pytest.mark.asyncio
async def test_open_repos_for_pr_is_exact_status_filtered_and_multi_repo(db):
    await _open(db, 80)
    await verif_crud.open_verification(
        db, repo="other/repo", pr_number=80, pr_title="t", merged_at=NOW, now=NOW
    )
    await _open(db, 81)
    await verif_crud.close_verification(
        db, repo=REPO, pr_number=81, verdict="pass-mechanical", reason="r", evidence=None, now=NOW
    )

    assert await verif_crud.open_repos_for_pr(db, pr_number=80) == ["other/repo", REPO]
    assert await verif_crud.open_repos_for_pr(db, pr_number=81) == [], "closed is not open"
    assert await verif_crud.open_repos_for_pr(db, pr_number=9999) == []


@pytest.mark.asyncio
async def test_list_closed_is_most_recently_closed_first_and_closed_only(db):
    await _open(db, 90)
    await _open(db, 91)
    await _open(db, 92)  # stays open
    await verif_crud.close_verification(
        db,
        repo=REPO,
        pr_number=90,
        verdict="pass-mechanical",
        reason="r",
        evidence="a",
        now="2026-09-10T00:00:00+00:00",
    )
    await verif_crud.close_verification(
        db,
        repo=REPO,
        pr_number=91,
        verdict="pass-with-measured-gaps",
        reason="r",
        evidence="b",
        now="2026-09-12T00:00:00+00:00",
    )
    rows = await verif_crud.list_closed(db)
    assert [r["pr_number"] for r in rows] == [91, 90]
    assert [r["evidence"] for r in rows] == ["b", "a"]
    assert 92 not in [r["pr_number"] for r in rows]


@pytest.mark.asyncio
async def test_list_open_is_oldest_merge_first_and_open_only(db):
    for n, merged in ((20, "2026-09-03T00:00:00Z"), (21, "2026-09-01T00:00:00Z")):
        await verif_crud.open_verification(
            db, repo=REPO, pr_number=n, pr_title="t", merged_at=merged, now=NOW
        )
    await verif_crud.open_verification(
        db,
        repo=REPO,
        pr_number=22,
        pr_title="d",
        merged_at="2026-08-30T00:00:00Z",
        now=NOW,
        closed_reason="docs-only",
    )
    rows = await verif_crud.list_open(db)
    assert [r["pr_number"] for r in rows] == [21, 20]
    assert await verif_crud.counts(db) == {"open": 2, "closed": 1}


# ── retention ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prune_deletes_only_old_closed_rows_never_open_ones(db):
    """THE fail-direction test: an ancient OPEN row survives every prune —
    deleting it would silently forgive an unverified merge, which is the exact
    failure this table exists to prevent."""
    await verif_crud.open_verification(
        db, repo=REPO, pr_number=30, pr_title="ancient open", merged_at=OLD, now=OLD
    )
    await verif_crud.open_verification(
        db,
        repo=REPO,
        pr_number=31,
        pr_title="old closed",
        merged_at=OLD,
        now=OLD,
        closed_reason="docs-only",
    )
    await verif_crud.open_verification(
        db,
        repo=REPO,
        pr_number=32,
        pr_title="fresh closed",
        merged_at=NOW,
        now=NOW,
        closed_reason="docs-only",
    )
    deleted = await verif_crud.prune_closed(db, older_than_days=180, now=NOW)
    assert deleted == 1
    assert (await _row(db, 30))["status"] == "open"  # ancient, open, UNTOUCHED
    assert await _row(db, 31) is None
    assert (await _row(db, 32))["status"] == "closed"


# ── pre-migration posture ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_everything_noops_before_the_migration(bare_db):
    assert not await verif_crud.tables_available(bare_db)
    assert (
        await verif_crud.open_verification(
            bare_db, repo=REPO, pr_number=1, pr_title=None, merged_at=NOW, now=NOW
        )
        == "unavailable"
    )
    assert not await verif_crud.exists(bare_db, repo=REPO, pr_number=1)
    assert await verif_crud.list_open(bare_db) == []
    assert await verif_crud.counts(bare_db) == {}
    assert await verif_crud.prune_closed(bare_db, now=NOW) == 0
    # The verdict-era surface, held to the same contract. "everything" is this
    # test's whole claim, so a new function missing here is a false claim.
    assert await verif_crud.open_repos_for_pr(bare_db, pr_number=1) == []
    assert await verif_crud.list_closed(bare_db) == []
    assert not await verif_crud._verdict_columns_available(bare_db)
    assert (
        await verif_crud.record_attempt(
            bare_db,
            repo=REPO,
            pr_number=1,
            verdict="cannot-verify",
            note="n",
            now=NOW,
        )
        == "unavailable"
    )
    assert not await verif_crud.close_verification(
        bare_db,
        repo=REPO,
        pr_number=1,
        verdict="pass-mechanical",
        reason="r",
        evidence=None,
        now=NOW,
    )


# ── the two build paths produce ONE schema ───────────────────────────────


def _normalize(sql: str) -> str:
    """Collapse whitespace, INCLUDING around punctuation.

    Whitespace-adjacent-to-punctuation is load-bearing here, and finding that
    out cost a red test. ``ALTER TABLE … ADD COLUMN`` does not re-render the
    CREATE statement — it splices the new column-definition text into the stored
    one, and the splice lands in different places than a human writing the
    column inline: the comma goes AFTER the previous definition's trailing
    newline (``NOT NULL ,``) and the new column goes flush against the closing
    paren (``TEXT)``), where the fresh path has ``NOT NULL,`` and ``TEXT )``.
    MEASURED on SQLite 3.45.1.

    So byte-identical stored text between the two build paths is UNACHIEVABLE by
    construction, not a thing careful DDL authoring can reach. Normalizing the
    punctuation is therefore not a relaxation of the guard — it removes a false
    failure the guard would otherwise raise on every ALTER-added column forever.
    Everything the test exists to catch survives: a missing or extra column, a
    different type, a different DEFAULT, a dropped CHECK, a divergent index.
    """
    return re.sub(r"\s*([(),])\s*", r"\1", " ".join(sql.split()))


@pytest.mark.asyncio
async def test_fresh_install_ddl_and_migration_are_the_same_schema(tmp_path):
    """schema/_tables.py builds fresh installs; the migration builds existing
    ones. If they drift, two populations run different schemas and nothing
    notices until a query fails on exactly one of them. Compare what SQLite
    itself recorded, table AND both indexes."""
    async with aiosqlite.connect(str(tmp_path / "fresh.db")) as fresh:
        await fresh.execute(TABLES["pr_verifications"])
        for ddl in INDEXES:
            if "pr_verifications" in ddl:
                await fresh.execute(ddl)
        cur = await fresh.execute(
            "SELECT name, sql FROM sqlite_master WHERE tbl_name='pr_verifications' "
            "AND sql IS NOT NULL ORDER BY name"
        )
        fresh_schema = {name: _normalize(sql) for name, sql in await cur.fetchall()}
    async with aiosqlite.connect(str(tmp_path / "migrated.db")) as migrated:
        await _build(migrated)
        cur = await migrated.execute(
            "SELECT name, sql FROM sqlite_master WHERE tbl_name='pr_verifications' "
            "AND sql IS NOT NULL ORDER BY name"
        )
        migrated_schema = {name: _normalize(sql) for name, sql in await cur.fetchall()}
    assert fresh_schema == migrated_schema
    assert "pr_verifications" in fresh_schema
    assert "idx_prv_repo_pr" in fresh_schema, "the dedup index is load-bearing"


def _columns(rows) -> list[tuple]:
    """(cid, name, type, notnull, default, pk) per column — the EFFECTIVE shape.

    ``pk`` is field 5 and is kept: the pragma reports it, and discarding it would
    make a PRIMARY KEY moving between columns invisible to this lens.
    """
    return [(r[0], r[1], r[2].upper(), r[3], r[4], r[5]) for r in rows]


#: Drift classes ``_normalize`` must still DISTINGUISH, as (a, b) DDL pairs that
#: differ only in the named way. Paired with the splice case it must ABSORB.
_MUST_DISTINGUISH = {
    "a dropped NOT NULL": ("CREATE TABLE t (a TEXT NOT NULL)", "CREATE TABLE t (a TEXT)"),
    "a changed type": ("CREATE TABLE t (a TEXT)", "CREATE TABLE t (a INTEGER)"),
    "a renamed column": ("CREATE TABLE t (a TEXT)", "CREATE TABLE t (b TEXT)"),
    "a dropped CHECK": (
        "CREATE TABLE t (a TEXT CHECK(a IN ('x')))",
        "CREATE TABLE t (a TEXT)",
    ),
    "a changed DEFAULT": (
        "CREATE TABLE t (a INTEGER DEFAULT 0)",
        "CREATE TABLE t (a INTEGER DEFAULT 1)",
    ),
    "a dropped PRIMARY KEY": (
        "CREATE TABLE t (a TEXT PRIMARY KEY)",
        "CREATE TABLE t (a TEXT)",
    ),
    "a reordered column pair": (
        "CREATE TABLE t (a TEXT, b TEXT)",
        "CREATE TABLE t (b TEXT, a TEXT)",
    ),
    "an extra column": ("CREATE TABLE t (a TEXT)", "CREATE TABLE t (a TEXT, b TEXT)"),
}


def test_normalize_absorbs_the_alter_splice_and_nothing_else():
    """The comparator is itself under test — the mutation I did not imagine.

    An adversarial review MEASURED that replacing ``_normalize``'s body with
    ``return ""`` left the whole file GREEN: every other test perturbs what the
    comparator LOOKS AT, so none of them notices the comparator going blind, and
    the equality becomes trivially true. That matters because ``_normalize`` is
    the only lens that can see CHECK, PRIMARY KEY, COLLATE and friends — the
    ``table_info`` lens cannot — and because a regex is exactly what a later
    session chasing a red test will "tidy".

    So: it must ABSORB the ALTER splice (the thing it was widened for) and must
    still DISTINGUISH every drift class its docstring claims. A vacuous
    implementation fails the second half immediately.
    """
    spliced = "CREATE TABLE t ( a TEXT NOT NULL , b TEXT)"
    inline = "CREATE TABLE t (a TEXT NOT NULL, b TEXT )"
    assert _normalize(spliced) == _normalize(inline), "must absorb the ALTER splice"

    for label, (a, b) in _MUST_DISTINGUISH.items():
        assert _normalize(a) != _normalize(b), f"_normalize went blind to {label}"


@pytest.mark.asyncio
async def test_both_build_paths_agree_on_the_effective_column_shape(tmp_path):
    """A SECOND, independent layer over the text comparison above.

    The text check normalizes punctuation (it must — see ``_normalize``), so it
    is one lens rather than two. This one never looks at the stored string at
    all: it asks SQLite what the table actually IS, column by column, in order,
    with types, NOT NULL flags and DEFAULTs. A drift that survived a normalizer
    change would still fail here, and vice versa — different failure modes,
    which is the point of having both.
    """
    async with aiosqlite.connect(str(tmp_path / "fresh.db")) as fresh:
        await fresh.execute(TABLES["pr_verifications"])
        cur = await fresh.execute("PRAGMA table_info(pr_verifications)")
        fresh_cols = _columns(await cur.fetchall())
    async with aiosqlite.connect(str(tmp_path / "migrated.db")) as migrated:
        await _build(migrated)
        cur = await migrated.execute("PRAGMA table_info(pr_verifications)")
        migrated_cols = _columns(await cur.fetchall())

    assert fresh_cols == migrated_cols
    # Guard-the-guard: the comparison is only meaningful if it actually saw the
    # four new columns in their appended positions. Without this the test would
    # pass just as happily against two identical PRE-migration tables.
    assert [c[1] for c in fresh_cols][-4:] == [
        "verdict",
        "attempt_count",
        "last_attempt_at",
        "last_attempt_note",
    ]
    by_name = {c[1]: c for c in fresh_cols}
    assert by_name["attempt_count"][3] == 1, "attempt_count must be NOT NULL"
    assert by_name["attempt_count"][4] == "0", "…with a 0 default, or ADD COLUMN is illegal"
    assert by_name["verdict"][3] == 0, "verdict is NULLable — NULL means never attempted"


@pytest.mark.asyncio
async def test_migration_is_idempotent(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "twice.db")) as db:
        await _build(db)
        await _build(db)  # a re-run must be a no-op, not an error


@pytest.mark.asyncio
async def test_verdict_columns_are_absent_before_their_migration(tmp_path):
    """The pre-migration window is STRUCTURAL on every existing install: the
    table is present (shipped 20260906234824) while the verdict columns are not,
    until the numbered runner catches up at the next restart. This pins that the
    window is real, so the CRUD guard that answers "unavailable" in it is not
    belt-and-braces."""
    async with aiosqlite.connect(str(tmp_path / "old.db")) as conn:
        await MIG.up(conn)
        cur = await conn.execute("PRAGMA table_info(pr_verifications)")
        cols = {row[1] for row in await cur.fetchall()}
    assert "status" in cols, "guard-the-guard: the old migration did build the table"
    assert not cols & {"verdict", "attempt_count", "last_attempt_at", "last_attempt_note"}


@pytest.mark.asyncio
async def test_the_verdict_writers_answer_unavailable_in_the_real_upgrade_window(tmp_path):
    """TABLE present, verdict COLUMNS absent — the state every existing install
    passes through between the code deploy and the next migration run.

    Found by a mutation sweep: making ``_verdict_columns_available`` return True
    unconditionally SURVIVED the pre-migration test, because that test uses a
    table-LESS database, so ``_tables_available`` short-circuits first and the
    column guard is never reached. The table-less case and the columns-missing
    case are different states and only one of them was covered.

    Both writers must decline rather than raise ``no such column`` inside a
    caller's ``except``, which is how a run reports success having written
    nothing.
    """
    async with aiosqlite.connect(str(tmp_path / "window.db")) as conn:
        conn.row_factory = aiosqlite.Row
        await MIG.up(conn)  # the ORIGINAL migration only
        await conn.commit()
        await verif_crud.open_verification(
            conn, repo=REPO, pr_number=1, pr_title="t", merged_at=NOW, now=NOW
        )

        assert await verif_crud.tables_available(conn), "guard-the-guard: table IS present"
        assert not await verif_crud._verdict_columns_available(conn)

        assert (
            await verif_crud.record_attempt(
                conn, repo=REPO, pr_number=1, verdict="cannot-verify", note="n", now=NOW
            )
            == "unavailable"
        )
        assert not await verif_crud.close_verification(
            conn,
            repo=REPO,
            pr_number=1,
            verdict="pass-mechanical",
            reason="r",
            evidence=None,
            now=NOW,
        )
        # The row is untouched and still the obligation it was.
        cur = await conn.execute(
            "SELECT status FROM pr_verifications WHERE repo = ? AND pr_number = 1", (REPO,)
        )
        assert (await cur.fetchone())["status"] == "open"

        # And the readers that do NOT need the new columns still work in the window.
        assert await verif_crud.open_repos_for_pr(conn, pr_number=1) == [REPO]
        assert [r["pr_number"] for r in await verif_crud.list_open(conn)] == [1]


@pytest.mark.asyncio
async def test_a_legacy_row_survives_the_migration_with_no_fabricated_verdict(tmp_path):
    """The 40 already-closed rows on a live install are docs-only PATH exemptions,
    not validator conclusions. A backfill would fabricate a verdict nobody
    reached, in the one ledger whose purpose is to be trustworthy about what was
    verified. So: row preserved, verdict NULL, attempt_count 0."""
    async with aiosqlite.connect(str(tmp_path / "legacy.db")) as conn:
        conn.row_factory = aiosqlite.Row
        await MIG.up(conn)
        await verif_crud.open_verification(
            conn,
            repo=REPO,
            pr_number=99,
            pr_title="docs: a thing",
            merged_at=OLD,
            now=OLD,
            closed_reason="docs-only by path rule (1 path(s)) — deterministic exemption",
        )
        await MIG_VERDICT.up(conn)
        cur = await conn.execute(
            "SELECT * FROM pr_verifications WHERE repo = ? AND pr_number = ?", (REPO, 99)
        )
        row = dict(await cur.fetchone())
    assert row["status"] == "closed"
    assert "docs-only by path rule" in row["closed_reason"]
    assert row["verdict"] is None, "no fabricated verdict"
    assert row["attempt_count"] == 0
    assert row["last_attempt_at"] is None and row["last_attempt_note"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "   ", "\n\t "])
async def test_close_refuses_an_empty_reason(db, bad):
    """A closed row with no reason is an unverifiable claim in permanent record:
    it says "handled" and carries nothing.

    The schema permits it (status='closed' with closed_reason NULL is valid
    DDL), so the guard lives at the writer — and it matters precisely because
    the eventual closer is a validator SESSION, i.e. an LLM caller, which is
    exactly the caller that passes an empty string. It RAISES rather than
    returning False: a caller that omitted its reason has a bug, and a silent
    no-op would leave the obligation open while the caller believes it closed.
    """
    await verif_crud.open_verification(
        db, repo=REPO, pr_number=40, pr_title="a", merged_at=NOW, now=NOW
    )
    with pytest.raises(ValueError, match="non-empty reason"):
        await verif_crud.close_verification(
            db, repo=REPO, pr_number=40, verdict="pass-mechanical", reason=bad,
            evidence="x", now=NOW
        )
    assert (await _row(db, 40))["status"] == "open", "the row must be untouched"


# ── retention window: a sub-1-day window would delete the whole closed set ────
#
# `prune_closed` computes `cutoff = now - timedelta(days=older_than_days)`, so a
# NEGATIVE window subtracts a negative and puts the cutoff in the FUTURE — at
# which point `closed_at < cutoff` matches EVERY closed row and the "retention"
# pass empties the table it exists to bound. The guard lives in the CRUD, not
# only at the CLI, because the caller that gets this wrong is the one that never
# thought about it. Mirrors `prune_merge_journal` (crud/entities.py), which names
# the same class for the entity journal.


@pytest.mark.parametrize("bad", [0, -1, -180])
async def test_prune_closed_refuses_a_sub_one_day_window(db, bad):
    await verif_crud.open_verification(
        db, repo=REPO, pr_number=90, pr_title="closed", merged_at=OLD, now=OLD,
        closed_reason="docs-only",
    )

    with pytest.raises(ValueError, match="retention window must be >= 1 day"):
        await verif_crud.prune_closed(db, older_than_days=bad, now=NOW)

    # The row it would have destroyed is still there — the guard refuses, it
    # does not partially delete.
    assert (await verif_crud.counts(db)).get("closed", 0) == 1


async def test_prune_closed_still_accepts_a_one_day_window(db):
    """The boundary stays OPEN: >= 1 is valid, so the guard cannot over-refuse."""
    await verif_crud.open_verification(
        db, repo=REPO, pr_number=91, pr_title="closed", merged_at=OLD, now=OLD,
        closed_reason="docs-only",
    )
    assert await verif_crud.prune_closed(db, older_than_days=1, now=NOW) == 1
