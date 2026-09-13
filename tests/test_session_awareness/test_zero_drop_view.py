"""The accounting view — denominators, honest degradation, and no false zeroes.

The view exists to make "what fell through the cracks?" answerable by
ENUMERATION. Every test here pins one of the three properties that makes an
answer trustworthy rather than merely present:

  1. a count is rendered with its denominator,
  2. a part that could not be read says so instead of reporting nothing,
  3. a stale source is never presented as a measured zero.

Property 2 is the one worth the most attention: a view that blanks when one
store is unreadable is strictly worse than five separate surfaces, because it
loses the four parts that WERE readable while looking like a complete answer.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from genesis.db.schema import create_all_tables
from genesis.session_awareness import zero_drop_view as V


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


@pytest.fixture
def pulse_home(tmp_path, monkeypatch):
    """Point the repo-pulse cache at a temp home and hand back a writer."""
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "home"))

    def write(record):
        from genesis.session_awareness import repo_pulse

        path = repo_pulse.open_prs_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record))
        return path

    return write


# ── The whole view ─────────────────────────────────────────────────────────


async def test_the_view_has_all_five_parts_and_a_timestamp(db, pulse_home):
    view = await V.build_view(db, now=datetime.now(UTC))

    assert set(view) == {
        "computed_at",
        "gaps",
        "pr_pipeline",
        "items_by_store",
        "owner_pending",
        "roadmap",
    }
    assert view["computed_at"], "a board with no timestamp cannot be aged"


async def test_one_unreadable_store_degrades_ONE_part_not_the_view(db, pulse_home, monkeypatch):
    """The property that makes an aggregate view safe to build at all.

    Blanking on a single failure would lose the four parts that were readable
    while still looking like a complete answer — which is the exact failure
    mode a "one surface for everything" design is supposed to remove.
    """

    async def _boom(*a, **kw):
        raise RuntimeError("store is down")

    monkeypatch.setattr(V, "_owner_pending", _boom)

    view = await V.build_view(db, now=datetime.now(UTC))

    assert view["owner_pending"]["status"] == V.STATUS_UNAVAILABLE
    assert "RuntimeError" in view["owner_pending"]["reason"]
    # ...and every other part still assembled.
    assert view["items_by_store"]["status"] == V.STATUS_OK
    assert view["roadmap"]["status"] == V.STATUS_OK
    assert view["gaps"]["status"] == V.STATUS_OK


async def test_a_missing_store_reads_unavailable_NOT_zero(db, pulse_home):
    """`no such table` must never render as a count.

    This is the real production shape while the detector's migration is
    unmerged: the table genuinely does not exist. "0 stranded" would be a lie
    that looks exactly like good news.
    """
    await db.execute("DROP TABLE zero_drop_findings")
    await db.commit()

    view = await V.build_view(db, now=datetime.now(UTC))

    assert view["gaps"]["status"] == V.STATUS_UNAVAILABLE
    assert view["items_by_store"]["stranded_work"]["status"] == V.STATUS_UNAVAILABLE
    assert "open" not in view["items_by_store"]["stranded_work"]


# ── Denominators ───────────────────────────────────────────────────────────


async def test_every_store_count_carries_its_denominator(db, pulse_home):
    """A bare numerator is not a measurement.

    `counts_by_status`'s own docstring calls the denominator the thing "every
    surface must render"; this is that requirement, enforced.
    """
    part = (await V.build_view(db, now=datetime.now(UTC)))["items_by_store"]

    assert {"open", "acked", "tracked"} <= set(part["stranded_work"])
    assert {"unresolved", "total"} <= set(part["ledger"])
    assert {"unresolved", "total"} <= set(part["follow_ups"])


async def test_every_part_obeys_the_denominator_rule_or_declares_an_exemption(db, pulse_home):
    """The RULE, enforced over every part — not sampled on the one we remember.

    The previous version of this file asserted the rule over `items_by_store`
    alone, so `owner_pending` shipped bare counts in the same commit whose
    module docstring forbids them. A rule checked on one part is a rule the
    next part will break.

    An exemption is legitimate — "things waiting on you" IS its own population,
    and pairing it with an all-time total would size it against a number nobody
    acts on — but it must be DECLARED in the payload, not inferred from its
    absence.
    """
    view = await V.build_view(db, now=datetime.now(UTC))

    # The exemption is the escape hatch this rule hands out, so the test has to
    # police the hatch too — otherwise adding `denominator_exempt` to a part is
    # a one-line way to make this test pass with every count bare, and the
    # docstring's claim that `owner_pending` is "the only one" is a sentence
    # nothing can falsify.
    exempt = sorted(
        n for n, p in view.items() if isinstance(p, dict) and p.get("denominator_exempt")
    )
    assert exempt == ["owner_pending"], (
        f"exactly ONE declared exemption is expected; found {exempt}. A new one "
        "is a decision to argue for, not a default to inherit."
    )

    # Universe DERIVED from the payload, never a literal beside it: a sixth part
    # added later must be checked by construction rather than remembered. (The
    # tab-registry test in this same change makes precisely this argument about
    # itself; it did not cross the file boundary.)
    for name, part in view.items():
        if not isinstance(part, dict) or name in ("gaps", "pr_pipeline", "roadmap"):
            continue
        if part.get("denominator_exempt"):
            continue
        for child, value in part.items():
            if child in ("status", "degraded_children") or not isinstance(value, dict):
                continue
            if value.get("status") == V.STATUS_UNAVAILABLE:
                continue
            assert {"total"} <= set(value) or {"tracked"} <= set(value), (
                f"{name}.{child} reports a count with no denominator and no declared exemption"
            )


async def test_the_follow_up_numerator_is_the_STORE_S_open_set_not_just_pending(db, pulse_home):
    """`failed` and `blocked` are the textbook stranded item this board names.

    Counting only `pending` reported them as zero — from a store that read
    perfectly, which is the worst kind of false zero. The numerator is derived
    from the store's own `_VALID_STATUS` minus the terminal state, so a status
    added later is included rather than silently dropped.
    """
    from genesis.db.crud import follow_ups as fu

    # The docstring above says the numerator is "derived from the store's own
    # _VALID_STATUS minus the terminal state, so a status added later is
    # included rather than silently dropped". That was a claim with nothing
    # behind it: the constant is a hand-written tuple, so a new status would be
    # silently EXCLUDED — the exact under-count it was written to prevent.
    # This is the assertion that makes the sentence true.
    assert set(V.FOLLOW_UP_OPEN_STATUSES) == fu._VALID_STATUS - {"completed"}, (
        "FOLLOW_UP_OPEN_STATUSES has drifted from the store's own vocabulary; a "
        "status added to _VALID_STATUS is otherwise dropped from the count in "
        "silence"
    )

    for i, status in enumerate(("pending", "in_progress", "failed", "blocked", "completed")):
        await fu.create(
            db, id=f"fu-{i}", content=f"item {i}", source="test", strategy="ego_judgment"
        )
        await fu.update_status(db, f"fu-{i}", status)
    await db.commit()

    part = (await V.build_view(db, now=datetime.now(UTC)))["items_by_store"]["follow_ups"]

    assert part["unresolved"] == 4, "pending + in_progress + failed + blocked all count as open"
    assert part["total"] == 5, "the denominator includes the completed one"


async def test_a_container_part_reports_DEGRADED_when_a_child_failed(db, pulse_home):
    """`ok` over three `unavailable`s is a false clean one level down.

    A consumer that trusts the part-level status — which is what the
    single-value parts train it to do — would render a failure as a success.
    """
    await db.execute("DROP TABLE zero_drop_findings")
    await db.commit()

    part = (await V.build_view(db, now=datetime.now(UTC)))["items_by_store"]

    assert part["status"] == V.STATUS_DEGRADED
    assert "stranded_work" in part["degraded_children"]


async def test_ego_proposals_are_partitioned_the_SAME_WAY_the_morning_report_does(db, pulse_home):
    """Two surfaces, one number.

    Informational eval rows are not approval work. Counting them here while the
    morning report excludes them would have the board and the report answer the
    same question with two different numbers on the same data — the exact drift
    the shared assembler exists to prevent.
    """
    from unittest.mock import AsyncMock, patch

    from genesis.ego.types import partition_informational

    # `action_type` is the field the partition keys on, and `j9_regression` is a
    # real member of INFORMATIONAL_ACTION_TYPES — not a plausible-looking
    # stand-in. The precondition assert below exists because the first version
    # of this fixture invented a field name and would have passed vacuously.
    rows = [
        {"id": "p1", "action_type": "investigate", "content": "real approval work"},
        {"id": "p2", "action_type": "j9_regression", "content": "informational"},
    ]
    approval_work, informational = partition_informational(rows)
    assert informational, "fixture precondition: at least one row must be informational"

    with patch("genesis.db.crud.ego.list_pending_proposals", AsyncMock(return_value=rows)):
        part = (await V.build_view(db, now=datetime.now(UTC)))["owner_pending"]

    assert part["ego_proposals"] == len(approval_work) < len(rows), (
        "the informational rows must be excluded, exactly as the morning report excludes them"
    )


async def test_the_ledger_count_spans_EVERY_session(db, pulse_home):
    """The gap this view closes.

    A ledger row is rendered by its own session's injection and by nothing
    else, so a dead session's open rows are unreachable — and until this view
    nothing counted them. A per-session count would report zero for exactly the
    rows that matter.
    """
    from genesis.db.crud import session_charters as sc

    await sc.upsert_stub(db, "session-alive")
    await sc.upsert_stub(db, "session-dead")
    await sc.ledger_add(db, session_id="session-alive", text="a row someone is carrying")
    await sc.ledger_add(db, session_id="session-dead", text="a row nobody can see any more")
    await db.commit()

    part = (await V.build_view(db, now=datetime.now(UTC)))["items_by_store"]

    assert part["ledger"]["unresolved"] == 2, "rows from BOTH sessions must be counted"
    assert part["ledger"]["total"] == 2


# ── The PR cache: freshness beats a number ────────────────────────────────


async def test_a_stale_pr_cache_reports_stale_and_WITHHOLDS_the_count(db, pulse_home):
    """A dead worker's snapshot must not be rendered as a live count.

    The count is withheld ON PURPOSE rather than shown beside its age: a number
    printed next to a caveat is still read as a number by anyone skimming.
    """
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    pulse_home({"version": 1, "computed_at": old, "prs": [{"number": 1}], "limit_hit": False})

    part = (await V.build_view(db, now=datetime.now(UTC)))["pr_pipeline"]

    assert part["status"] == V.STATUS_STALE
    assert "open_prs" not in part, "a stale cache must not report a count at all"
    assert part["age_seconds"] > 0


async def test_a_fresh_pr_cache_reports_its_count_AND_its_age(db, pulse_home):
    now = datetime.now(UTC)
    pulse_home(
        {
            "version": 1,
            "computed_at": (now - timedelta(hours=2)).isoformat(),
            "repo": "acme/widgets",
            "prs": [{"number": 1}, {"number": 2}],
            "limit_hit": False,
        }
    )

    part = (await V.build_view(db, now=now))["pr_pipeline"]

    assert part["status"] == V.STATUS_OK
    assert part["open_prs"] == 2
    assert part["count_is_floor"] is False
    assert part["age_seconds"] >= 7000, "the age is always rendered, fresh or not"


async def test_a_CAPPED_listing_is_reported_as_a_floor_not_a_total(db, pulse_home):
    """`limit_hit` means the fetch was truncated, so the count is a minimum.

    Reporting it as a total would be a measurement the data does not support —
    the same class of error as a zero with no denominator.
    """
    now = datetime.now(UTC)
    pulse_home(
        {
            "version": 1,
            "computed_at": now.isoformat(),
            "repo": "acme/widgets",
            "prs": [{"number": n} for n in range(50)],
            "limit_hit": True,
        }
    )

    part = (await V.build_view(db, now=now))["pr_pipeline"]

    assert part["count_is_floor"] is True
    assert "at least 50" in part["verdict"]


async def test_no_cache_at_all_is_unavailable_not_an_empty_pipeline(db, pulse_home):
    """A box where the pulse worker has never run has UNKNOWN open PRs, not zero."""
    part = (await V.build_view(db, now=datetime.now(UTC)))["pr_pipeline"]

    assert part["status"] == V.STATUS_UNAVAILABLE
    assert "open_prs" not in part


@pytest.mark.parametrize("body", ['{"prs": "not a list"}', "[]", "not json at all"])
async def test_a_MALFORMED_cache_is_unavailable_rather_than_a_crash(db, pulse_home, body):
    """The cache is a file on disk; nothing guarantees its shape.

    Each of these once had a plausible route to either an exception out of the
    view or a silent zero.
    """
    from genesis.session_awareness import repo_pulse

    path = repo_pulse.open_prs_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)

    part = (await V.build_view(db, now=datetime.now(UTC)))["pr_pipeline"]

    assert part["status"] == V.STATUS_UNAVAILABLE
    assert "open_prs" not in part


# ── Gaps: delegation, not re-derivation ───────────────────────────────────


async def test_gaps_carries_the_detector_freshness_verdict(db, pulse_home):
    """A zero in the gaps part is meaningless without the detector's age.

    The verdict string comes from the detector's own read surface rather than
    being re-derived here, so the two cannot drift — and a never-run detector
    says so in words rather than reporting a clean board.
    """
    gaps = (await V.build_view(db, now=datetime.now(UTC)))["gaps"]

    assert gaps["status"] == V.STATUS_OK
    assert gaps["open"] == 0
    assert "unverified" in gaps["detector"]["verdict"], (
        "a zero from a never-run detector must be labelled unverified, not clean"
    )
    assert gaps["detector"]["stale"] is True


async def test_the_gaps_listing_pages_but_the_counts_do_not(db, pulse_home):
    """`listed` is a page; `listed_of` is the population.

    A paged listing that reported its page size as the total would turn the
    denominator — the whole point — into a restatement of the limit.
    """
    from genesis.db.crud import zero_drop as zd

    present = [
        {"branch": f"feat/b{n}", "tip_sha": f"{n:040x}", "ahead_count": 1, "worktree_path": None}
        for n in range(5)
    ]
    await zd.apply_sweep(db, class_="unpushed_branch", present=present, run_id="r1")
    await db.commit()

    view = await V.build_view(db, now=datetime.now(UTC), findings_limit=2)
    gaps = view["gaps"]

    assert gaps["listed"] == 2, "the listing is paged"
    assert gaps["listed_of"] == 5, "the denominator is the full COUNT, not the page"
    assert len(gaps["findings"]) == 2


# ── Roadmap ────────────────────────────────────────────────────────────────


async def test_the_roadmap_names_what_the_board_does_NOT_cover(db, pulse_home):
    """A five-part board reads as complete unless it says otherwise.

    Naming the absent legs is the same discipline as rendering a denominator:
    both exist so a reader can tell the size of what they are looking at.
    """
    roadmap = (await V.build_view(db, now=datetime.now(UTC)))["roadmap"]

    assert roadmap["not_covered"], "the board must state its own boundaries"
    assert any("Beads" in item for item in roadmap["not_covered"]), (
        "the item-level carve-out is a deliberate boundary and must be visible"
    )


async def test_a_FUTURE_dated_pr_cache_WITHHOLDS_its_count(db, pulse_home):
    """The mirror of the stale case, and the half that was silent.

    A negative age is never greater than the TTL, so a cache whose
    `computed_at` is in the FUTURE sailed through the freshness gate and this
    part reported a live count off a snapshot that cannot be current. It is
    reachable from clock skew, a restored backup or a hand-edited file — and a
    future-dated record is the SIGNATURE of a wedged writer, so the one state
    where the count deserves least trust was the state that skipped the check.

    The detector fixed exactly this on its own run record; the family did not
    get swept one module out. The tolerance is IMPORTED rather than redefined,
    because two constants for one physical fact drift with nothing watching.
    """
    from genesis.session_awareness.zero_drop import FUTURE_SKEW_TOLERANCE

    now = datetime.now(UTC)
    ahead = (now + timedelta(days=30)).isoformat()
    pulse_home({"version": 1, "computed_at": ahead, "prs": [{"number": 1}], "limit_hit": False})

    part = (await V.build_view(db, now=now))["pr_pipeline"]

    assert part["status"] == V.STATUS_STALE, f"a future cache must not read as live: {part}"
    assert "open_prs" not in part, "the COUNT is the thing to withhold"
    assert "FUTURE" in part["verdict"]
    assert part["age_seconds"] < 0, "the negative age is the evidence, and it is rendered"

    # The tolerance is real, not a strict rejection: a record written moments
    # ago can sit a hair ahead of `now` through ordinary drift, and withholding
    # on that would be a false positive on the FRESHEST possible data.
    near = (now + FUTURE_SKEW_TOLERANCE / 2).isoformat()
    pulse_home(
        {
            "version": 1,
            "computed_at": near,
            "repo": "acme/widgets",
            "prs": [{"number": 1}],
            "limit_hit": False,
        }
    )

    ok = (await V.build_view(db, now=now))["pr_pipeline"]
    assert ok["status"] == V.STATUS_OK, f"drift inside the tolerance is not a wedged writer: {ok}"


async def test_the_stranded_counts_come_from_ONE_snapshot_not_two(db, pulse_home, monkeypatch):
    """Two sections of one board must not disagree about the same number.

    `items_by_store` used to issue its own `counts_by_status`, independent of
    the identical query the gaps part had already run. A detector committing a
    sweep between those two awaits would leave the SAME assembled board
    reporting different open/tracked totals in its two sections — and the
    morning report free to pair the later count with the earlier detector
    metadata. SQLite WAL gives snapshot isolation per read TRANSACTION, not
    across unrelated autocommit SELECTs, so proximity is not a defence.

    Driven by making the second call answer DIFFERENTLY: if the assembled view
    still shows one consistent number, the second call is not being used.
    """
    from genesis.db.crud import zero_drop as zd_crud

    real = zd_crud.counts_by_status
    calls = {"n": 0}

    async def _shifting_counts(conn):
        calls["n"] += 1
        out = await real(conn)
        if calls["n"] > 1:
            # A concurrent sweep landing between the two reads.
            return {**out, "open": out.get("open", 0) + 99}
        return out

    monkeypatch.setattr(zd_crud, "counts_by_status", _shifting_counts)

    view = await V.build_view(db, now=datetime.now(UTC))

    assert view["gaps"]["status"] == V.STATUS_OK
    assert view["items_by_store"]["stranded_work"]["open"] == view["gaps"]["open"], (
        "the two sections of one board must report the same number — "
        f"gaps={view['gaps']['open']} "
        f"items={view['items_by_store']['stranded_work']['open']}"
    )


async def test_DEFERRED_follow_ups_are_reported_not_silently_dropped(db, pulse_home):
    """A store of nothing but deferred work rendered `0 unresolved of 0 total`.

    `include_tabled=False` narrows the query to `WHERE kind = 'follow_up'`,
    which excludes the cold `tabled` lane AND the `idea` lane. Those rows exist
    and are tracked; they are simply in a lane nobody dispatches from. Counting
    them as zero is a FALSE ZERO on the board whose whole purpose is that a
    zero can be trusted.

    Folding them into `unresolved` would be the opposite error — tabled work is
    consciously NOT being done, and counting it as outstanding inflates every
    surface. So the actionable lane keeps its own numerator and denominator and
    the rest is reported BESIDE it.
    """
    from genesis.db.crud import follow_ups as fu_crud

    await fu_crud.create(
        db,
        id="cold-1",
        content="someday",
        source="test",
        strategy="ego_judgment",
        kind="tabled",
    )
    await db.commit()

    part = (await V.build_view(db, now=datetime.now(UTC)))["items_by_store"]["follow_ups"]

    assert part["deferred"] == 1, (
        f"a tracked row in a deferred lane must be COUNTED, not dropped: {part}"
    )
    assert part["unresolved"] == 0, (
        f"and must NOT inflate the actionable numerator — it is consciously not being done: {part}"
    )


async def test_the_pr_count_carries_the_repository_it_counted(db, pulse_home):
    """A count with no scope reads as correct for the wrong repository.

    The cache records which repo it listed. Dropping that meant a retargeted
    remote or a renamed repository left a still-FRESH cache rendering `N open`
    for PRs belonging to the previous repo — a plausible wrong count, which is
    worse here than an unavailable one.

    Surfaced rather than validated against the live repository: the cache
    stores `owner/name` while the live accessors expose the halves separately,
    and the owner half is empty on some installs — so asserting equality would
    mark a good cache unavailable forever. Rendering the scope lets a reader
    see a mismatch; asserting it would manufacture one.
    """
    now = datetime.now(UTC)
    pulse_home(
        {
            "version": 1,
            "computed_at": now.isoformat(),
            "repo": "someone/other-repo",
            "prs": [{"number": 1}, {"number": 2}],
            "limit_hit": False,
        }
    )

    part = (await V.build_view(db, now=now))["pr_pipeline"]

    assert part["status"] == V.STATUS_OK
    assert part["repo"] == "someone/other-repo", (
        f"the count must carry the repository it belongs to: {part}"
    )


async def test_an_UNSCOPED_pr_cache_withholds_its_count(db, pulse_home):
    """A count nobody can attribute is withheld, not rendered scope-less.

    The cache records WHICH repository it listed. Reporting `ok` with a number
    and no scope hands the reader a figure they cannot attribute — and a
    retargeted remote or a renamed repository then renders a still-FRESH count
    belonging to the previous repo, which reads as correct.

    This is the standard the part already applies to freshness (past the TTL,
    and on future skew, the count is withheld rather than shown beside a
    caveat). It was simply never applied to scope. Both directions are pinned:
    a missing field and a blank one, because `""` is the shape a truncated or
    hand-edited cache produces.

    NOT validated against the live repository, deliberately — presence is
    checkable, identity is not: the cache stores `owner/name` while the live
    accessors expose the halves separately and the owner half is empty on some
    installs, so an equality assertion would convert a rare wrong count into a
    permanent false negative.
    """
    now = datetime.now(UTC)
    fresh = (now - timedelta(hours=1)).isoformat()

    for label, record in (
        ("missing", {"version": 1, "computed_at": fresh, "prs": [{"number": 1}]}),
        ("blank", {"version": 1, "computed_at": fresh, "repo": "   ", "prs": [{"number": 1}]}),
        ("wrong type", {"version": 1, "computed_at": fresh, "repo": 42, "prs": [{"number": 1}]}),
    ):
        pulse_home(record)
        part = (await V.build_view(db, now=now))["pr_pipeline"]

        assert part["status"] == V.STATUS_UNAVAILABLE, f"{label} scope must withhold: {part}"
        assert "open_prs" not in part, f"{label}: the COUNT is the thing to withhold"
        assert "scope" in part["reason"], f"{label}: the reason must name WHY"


async def test_every_follow_up_figure_comes_from_ONE_read(db, pulse_home, monkeypatch):
    """Two sections of one board cannot disagree about one population.

    This took three attempts. Two separate reads were subtracted, which could
    yield a NEGATIVE deferred count; then the totals were unified while
    `by_status` was still read separately, so `unresolved` could exceed
    `total`. The connection is shared and releases its lock per database
    method, so any two SELECTs are two snapshots however close together they
    run.

    Driven by making a SECOND read of this store answer differently: if any
    figure still came from its own query, the assembled part would be
    internally inconsistent.
    """
    from genesis.db.crud import follow_ups as fu_crud

    for i in range(3):
        await fu_crud.create(
            db, id=f"act-{i}", content=f"a{i}", source="test", strategy="ego_judgment"
        )
    await fu_crud.create(
        db, id="cold", content="someday", source="test", strategy="ego_judgment", kind="tabled"
    )
    # A THIRD kind, and it is load-bearing rather than thoroughness. The
    # remainder's whole claim is that a kind the code does not name is still
    # counted — a fixture holding only `follow_up` and `tabled` cannot tell a
    # COMPLEMENT from a hand-written list of those two, so it would pass
    # against the very shape this is meant to forbid.
    await fu_crud.create(
        db,
        id="notion",
        content="someday-maybe",
        source="test",
        strategy="ego_judgment",
        kind="idea",
    )
    await fu_crud.update_status(db, "act-2", "completed")
    await db.commit()

    # Assert the SECOND READ DOES NOT EXIST, rather than counting the first.
    # An earlier version counted `get_lane_counts` calls — which stays at one
    # when a separate `get_summary_counts` is ADDED beside it, so the test
    # passed against the defect. And in a quiescent database two reads return
    # identical data, so the coherence assertions below cannot see the split
    # either. The only observable form of "one read" is that nothing else is
    # called.
    other = {"n": 0}
    real_summary = fu_crud.get_summary_counts

    async def _counted_summary(conn, **kw):
        other["n"] += 1
        return await real_summary(conn, **kw)

    monkeypatch.setattr(fu_crud, "get_summary_counts", _counted_summary)

    part = (await V.build_view(db, now=datetime.now(UTC)))["items_by_store"]["follow_ups"]

    assert other["n"] == 0, (
        "every figure must come from the single lane read — a second query "
        f"against this population is the defect, saw {other['n']}"
    )
    assert part["total"] == 3, f"actionable rows only: {part}"
    assert part["deferred"] == 2, f"BOTH non-actionable kinds are counted — tabled and idea: {part}"
    assert part["unresolved"] == 2, f"completed is terminal: {part}"
    assert part["unresolved"] <= part["total"], (
        f"unresolved can never exceed total — that shape was reachable when "
        f"by_status was read separately: {part}"
    )
    assert part["deferred"] >= 0, f"a subtracted remainder could go negative: {part}"
    assert sum(part["by_status"].values()) == part["total"], (
        f"by_status must add up to the same total it was read with: {part}"
    )

    # A TERMINAL row in the deferred lane is tracked but NOT outstanding. The
    # previous shape counted every deferred row as outstanding regardless of
    # status, and this is reachable rather than theoretical: neither
    # `update_status` nor the MCP update path filters on `kind`, so a tabled
    # row can be completed and would then have sat in the remainder forever.
    await fu_crud.update_status(db, "cold", "completed")
    await db.commit()

    after = (await V.build_view(db, now=datetime.now(UTC)))["items_by_store"]["follow_ups"]
    assert after["deferred"] == 2, f"still tracked in the lane: {after}"
    assert after["deferred_open"] == 1, f"but a completed deferred row is not outstanding: {after}"
    # The two lanes are separate POPULATIONS, each with its own numerator and
    # denominator. An earlier shape folded deferred rows into one status map
    # under a sentinel key, so every caller had to strip it before summing and
    # the deferred count shared a denominator it did not belong to.
    assert "by_status" in part and part["by_status"] == {"pending": 2, "completed": 1}, (
        f"by_status is the ACTIONABLE lane only: {part}"
    )
    assert part["deferred_open"] == 2, (
        f"the deferred lane reports its OWN numerator, not the actionable one: {part}"
    )
    assert part["deferred_open"] <= part["deferred"], (
        f"and its numerator cannot exceed its own denominator: {part}"
    )
