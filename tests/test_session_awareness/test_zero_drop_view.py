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

    for name in ("items_by_store", "owner_pending"):
        part = view[name]
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
