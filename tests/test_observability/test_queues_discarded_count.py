"""The discarded-count contract the queues snapshot PRODUCES.

Scope note: this pins the producer only. The frontend regression — the actual
2026-08-30 bug — is pinned in tests/test_dashboard/test_webui_js_integrity.py,
because `queues.py` was already correct and these tests pass unchanged on the
buggy revision.

`queues()` measures discarded work two ways that can disagree: an unbounded
COUNT of the depth, and a LIMIT-20 review sample. It now reconciles them ONCE
and publishes a single `discarded` object (`total`, `sample`, `sample_truncated`,
`known`); the flat `discarded_count` / `discarded_items` keys remain for the
depth alarm and pass-through consumers, derived from that object.

Origin (2026-08-30): the overview panel and the attention strip both rendered
the sample length, so a queue holding 148 discarded rows reported "20 discarded
items" — a truncation limit read as a total. Publishing both numbers put that
choice at every render site, and across three review rounds several chose wrong
in different ways. Reconciling here is what makes those states unrepresentable
rather than merely currently-correct.

These tests pin the producer's half: that the depth is independent of the sample
cap, that each of the two queries can fail without corrupting the other's
result, that `known` distinguishes a measured zero from an unmeasured one, and
that the flat keys are derived rather than separately computed. The frontend
half — that no surface reads the raw keys, and that all three normalisers agree
with each other and with expected values — is in
tests/test_dashboard/test_webui_js_integrity.py.
"""

import importlib
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from genesis.db.schema import create_all_tables

q = importlib.import_module("genesis.observability.snapshots.queues")

# Deliberately > the LIMIT 20 sample cap, so count and sample MUST diverge.
_DISCARDED = 25


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


async def _seed(db, *, discarded: int = 0, expired: int = 0) -> None:
    # Seed relative to now: absolute dates become wall-clock time bombs.
    _seeded_at = datetime.now(UTC) - timedelta(days=1)
    for i in range(discarded + expired):
        await db.execute(
            "INSERT INTO deferred_work_queue "
            "(id, work_type, call_site_id, payload_json, deferred_at, "
            " deferred_reason, attempts, error_message, status, "
            " created_at, completed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"seed-{i}",
                "entity_adjudication",
                "entity_adjudication",
                "{}",
                _seeded_at.isoformat(),
                "fuzzy norm_name match at entity creation",
                5,
                "exhausted 5 attempts",
                "discarded" if i < discarded else "expired",
                _seeded_at.isoformat(),
                # timedelta, not f"00:{i:02d}" — that produced "00:60:00" above
                # 59 rows, which fromisoformat rejects inside queues.py's loop
                # where the except swallows it, yielding a baffling empty sample.
                (_seeded_at + timedelta(minutes=i)).isoformat(),
            ),
        )
    await db.commit()


async def test_discarded_count_is_the_true_depth_not_the_sample_length(db):
    """The bug, pinned: count must exceed the capped sample."""
    await _seed(db, discarded=_DISCARDED)

    result = await q.queues(db, None, None, None)

    assert result["discarded_count"] == _DISCARDED, (
        f"discarded_count must be the unbounded COUNT, got {result['discarded_count']}"
    )
    # The sample is deliberately capped; this is correct, not a bug.
    assert len(result["discarded_items"]) == 20


async def test_discarded_count_includes_expired_like_the_sample_query(db):
    """Both halves filter status IN ('discarded','expired') — keep them aligned.

    If the count and sample ever filtered differently, the frontend's
    `discarded_count || items.length` fallback would swap between two
    populations depending on which happened to be truthy.
    """
    await _seed(db, discarded=3, expired=4)

    result = await q.queues(db, None, None, None)

    assert result["discarded_count"] == 7
    assert len(result["discarded_items"]) == 7


async def test_discarded_count_is_zero_on_an_empty_queue(db):
    """Empty state: a fresh install must report 0, not a missing key."""
    result = await q.queues(db, None, None, None)

    assert result["discarded_count"] == 0
    assert result["discarded_items"] == []


class TestDiscardedIsReconciledOnce:
    """The producer emits ONE reconciled object; nothing downstream re-derives it.

    The panel used to receive an uncapped `discarded_count` beside a LIMIT-20
    `discarded_items` sample, and four render surfaces each decided how to
    reconcile two numbers that can disagree. Every one of those decisions was a
    chance to pick wrong, and across three review rounds several did — printing
    the sample size as the total, hiding a backlog behind an empty sample,
    disabling the control that clears rows it was simultaneously listing.

    Reconciling once, here, is what makes those states unrepresentable rather
    than merely currently-correct: the frontends receive `total`, `sample`,
    `sample_truncated` and `known`, and have nothing left to choose between.

    These run against a real sqlite connection (the module fixture), so they
    exercise the actual queries rather than a fake that could drift from them.
    """

    async def test_total_is_the_honest_value_when_count_exceeds_the_sample(self, db):
        """`total` must never under-report — the original bug, on the new shape."""
        await _seed(db, discarded=_DISCARDED)
        d = (await q.queues(db, None, None, None))["discarded"]

        assert d["known"] is True
        assert d["total"] == _DISCARDED
        assert len(d["sample"]) == 20, "the sample stays capped; only the total is uncapped"
        assert d["sample_truncated"] is True

    async def test_untruncated_when_everything_fits(self, db):
        await _seed(db, discarded=3)
        d = (await q.queues(db, None, None, None))["discarded"]

        assert d["total"] == 3
        assert d["sample_truncated"] is False, (
            "claimed truncation with every row on screen — the panel would offer "
            "a 'showing 3 of 3' label that reads as though rows were withheld"
        )

    async def test_empty_queue_is_known_and_zero(self, db):
        d = (await q.queues(db, None, None, None))["discarded"]

        assert d == {"total": 0, "sample": [], "sample_truncated": False, "known": True}, (
            "an empty queue must be reported as a MEASURED zero — the panel "
            "distinguishes it from an unknown one, and gets that from `known`"
        )

    async def test_a_failed_count_is_unknown_not_a_confident_zero(self, db, monkeypatch):
        """A failed COUNT must never render as 'nothing awaiting review'."""
        await _seed(db, discarded=_DISCARDED)
        real = db.execute

        async def _fail_the_count(sql, *a, **k):
            if "COUNT(*)" in sql and "discarded" in sql:
                raise RuntimeError("count query exploded")
            return await real(sql, *a, **k)

        monkeypatch.setattr(db, "execute", _fail_the_count)
        d = (await q.queues(db, None, None, None))["discarded"]

        assert d["known"] is False, (
            "a failed count was published as a known depth, so the panel would "
            "state a number it never measured"
        )
        assert d["total"] == len(d["sample"]), "rows already in hand should still be reported"

    async def test_a_failed_sample_does_not_discard_a_good_count(self, db, monkeypatch):
        """Two independent try blocks, not one.

        The single-try version assigned the count and then ran the sample loop
        inside the same block, so one unparseable row threw away a correct count
        and the panel claimed the queue was empty.
        """
        await _seed(db, discarded=_DISCARDED)
        real = db.execute

        async def _fail_the_sample(sql, *a, **k):
            if "SELECT id, work_type" in sql:
                raise RuntimeError("sample query exploded")
            return await real(sql, *a, **k)

        monkeypatch.setattr(db, "execute", _fail_the_sample)
        d = (await q.queues(db, None, None, None))["discarded"]

        assert d["known"] is True, "the count succeeded and must stand on its own"
        assert d["total"] == _DISCARDED
        assert d["sample"] == []
        assert d["sample_truncated"] is True, (
            "rows exist and none are shown — the panel must say so rather than "
            "imply it is displaying the whole queue"
        )

    async def test_the_flat_fields_are_derived_even_when_they_diverge(self, db, monkeypatch):
        """The legacy keys stay, but as DERIVED values, not a second source.

        Asserted under DIVERGENCE, deliberately: on the happy path the raw
        counter and the reconciled total are equal, so computing the flat key
        separately passes and the test proves nothing. The failed-count case
        separates them — the raw counter is 0 while rows are in hand.

        This is not cosmetic. `mcp/health/errors.py` raises its >100 depth alarm
        from `discarded_count`, so a flat key wired to the raw counter would
        silently under-report to the alarm in exactly the state where the
        producer already knows better.
        """
        await _seed(db, discarded=3)
        real = db.execute

        async def _fail_the_count(sql, *a, **k):
            if "COUNT(*)" in sql and "discarded" in sql:
                raise RuntimeError("count query exploded")
            return await real(sql, *a, **k)

        monkeypatch.setattr(db, "execute", _fail_the_count)
        stats = await q.queues(db, None, None, None)

        assert stats["discarded"]["total"] == 3
        assert stats["discarded_count"] == 3, (
            "the flat key was computed separately from the object — it reports "
            "the raw counter (0 here) while the object reports the 3 rows in "
            "hand, so the depth alarm reads a number the producer knows is wrong"
        )
        assert stats["discarded_items"] == stats["discarded"]["sample"]

    async def test_a_queue_exactly_at_the_cap_is_not_truncated(self, db):
        """Exactly `cap` rows is a COMPLETE read, and must not claim otherwise.

        The first attempt at the cap-blindness fix treated "the sample filled
        its cap" as truncation outright, so a queue holding exactly 20 rows
        rendered "showing 20 of 20" — asserting rows were withheld when none
        were. Truncation is now decided by fetching ONE row past the cap: if the
        extra row does not come back, the sample is the whole queue.
        """
        await _seed(db, discarded=q._DISCARDED_SAMPLE_CAP)
        d = (await q.queues(db, None, None, None))["discarded"]

        assert d["known"] is True
        assert d["total"] == q._DISCARDED_SAMPLE_CAP
        assert len(d["sample"]) == q._DISCARDED_SAMPLE_CAP, "the extra probe row must not be shipped"
        assert d["sample_truncated"] is False, (
            "a queue holding exactly the cap was reported as truncated — the "
            "panel would say 'showing 20 of 20' as though rows were withheld"
        )

    async def test_truncation_is_decided_without_the_count(self, db):
        """One row past the cap proves truncation, even with the COUNT dead.

        This is the case a comparison cannot see. With the COUNT failing,
        `total` falls back to the sample length, so `total > len(sample)` is
        False at exactly the moment more rows exist. The sentinel row settles it
        from the same statement that produced the sample, so the verdict does
        not depend on the count being available — or on the count and the sample
        having been read at the same instant.

        Scope, stated honestly: the OLD cap-equality heuristic also returned
        True for this input. What only the sentinel can do is tell "filled the
        cap" apart from "more exist", which is
        `test_a_queue_exactly_at_the_cap_is_not_truncated`'s job. This test
        pins the half that survives the count being gone.
        """
        await _seed(db, discarded=q._DISCARDED_SAMPLE_CAP + 1)
        real = db.execute

        async def _fail_the_count(sql, *a, **k):
            if "COUNT(*)" in sql and "discarded" in sql:
                raise RuntimeError("count query exploded")
            return await real(sql, *a, **k)

        db.execute = _fail_the_count
        try:
            d = (await q.queues(db, None, None, None))["discarded"]
        finally:
            db.execute = real

        assert d["known"] is False, "the count failed and must be reported unknown"
        assert len(d["sample"]) == q._DISCARDED_SAMPLE_CAP, "the sample stays capped"
        assert d["sample_truncated"] is True, (
            "more rows exist than were shipped and the count is unavailable — "
            "the sentinel establishes this without the count, and must"
        )

    async def test_the_sentinel_row_is_never_shipped(self, db):
        """The probe row is evidence, not payload.

        The sample is fetched at cap+1 to detect truncation; shipping that extra
        row would silently widen every payload by one and make the rendered
        "showing N" disagree with the list beneath it.
        """
        await _seed(db, discarded=q._DISCARDED_SAMPLE_CAP + 5)
        d = (await q.queues(db, None, None, None))["discarded"]

        assert len(d["sample"]) == q._DISCARDED_SAMPLE_CAP
        assert d["sample_truncated"] is True
