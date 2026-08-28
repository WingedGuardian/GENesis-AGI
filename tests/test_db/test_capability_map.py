"""Tests for capability_map CRUD and aggregation."""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.db.crud import capability_map as cap_crud


@pytest.fixture
async def db(tmp_path):
    """DB with capability_map table."""
    db_path = str(tmp_path / "test.db")
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("""
            CREATE TABLE capability_map (
                id              TEXT PRIMARY KEY,
                domain          TEXT NOT NULL UNIQUE,
                confidence      REAL NOT NULL DEFAULT 0.0,
                sample_size     INTEGER NOT NULL DEFAULT 0,
                trend           TEXT DEFAULT 'stable',
                evidence_summary TEXT,
                previous_confidence REAL,
                updated_at      TEXT NOT NULL
            )
        """)
        await conn.commit()
        yield conn


class TestUpsert:
    @pytest.mark.asyncio
    async def test_insert_new_domain(self, db):
        cid = await cap_crud.upsert(
            db, domain="investigate", confidence=0.85, sample_size=12,
            evidence_summary="journal:85%(12)",
        )
        assert isinstance(cid, str)

        entry = await cap_crud.get_by_domain(db, "investigate")
        assert entry is not None
        assert entry["confidence"] == 0.85
        assert entry["sample_size"] == 12

    @pytest.mark.asyncio
    async def test_update_existing_domain(self, db):
        await cap_crud.upsert(
            db, domain="outreach", confidence=0.5, sample_size=8,
        )
        await cap_crud.upsert(
            db, domain="outreach", confidence=0.65, sample_size=15,
            trend="improving",
        )
        entry = await cap_crud.get_by_domain(db, "outreach")
        assert entry["confidence"] == 0.65
        assert entry["sample_size"] == 15
        assert entry["trend"] == "improving"


class TestGetAll:
    @pytest.mark.asyncio
    async def test_ordered_by_confidence_desc(self, db):
        await cap_crud.upsert(db, domain="low", confidence=0.3, sample_size=5)
        await cap_crud.upsert(db, domain="high", confidence=0.9, sample_size=10)
        await cap_crud.upsert(db, domain="mid", confidence=0.6, sample_size=8)

        entries = await cap_crud.get_all(db)
        assert len(entries) == 3
        assert entries[0]["domain"] == "high"
        assert entries[1]["domain"] == "mid"
        assert entries[2]["domain"] == "low"

    @pytest.mark.asyncio
    async def test_empty_table(self, db):
        entries = await cap_crud.get_all(db)
        assert entries == []


class TestGetByDomain:
    @pytest.mark.asyncio
    async def test_found(self, db):
        await cap_crud.upsert(db, domain="research", confidence=0.75, sample_size=6)
        entry = await cap_crud.get_by_domain(db, "research")
        assert entry is not None
        assert entry["domain"] == "research"

    @pytest.mark.asyncio
    async def test_not_found(self, db):
        entry = await cap_crud.get_by_domain(db, "nonexistent")
        assert entry is None



class TestGetWeakest:
    @pytest.mark.asyncio
    async def test_weakest_first_below_threshold(self, db):
        await cap_crud.upsert(db, domain="strong", confidence=0.9, sample_size=10)
        await cap_crud.upsert(db, domain="weak1", confidence=0.2, sample_size=8)
        await cap_crud.upsert(db, domain="weak2", confidence=0.35, sample_size=8)

        weak = await cap_crud.get_weakest(db, max_confidence=0.5)
        assert [e["domain"] for e in weak] == ["weak1", "weak2"]

    @pytest.mark.asyncio
    async def test_min_sample_size_filters_flukes(self, db):
        await cap_crud.upsert(db, domain="fluke", confidence=0.1, sample_size=1)
        await cap_crud.upsert(db, domain="real", confidence=0.3, sample_size=5)

        weak = await cap_crud.get_weakest(db, max_confidence=0.5, min_sample_size=3)
        assert [e["domain"] for e in weak] == ["real"]

    @pytest.mark.asyncio
    async def test_limit_caps_results(self, db):
        for i in range(5):
            await cap_crud.upsert(
                db, domain=f"d{i}", confidence=0.1 + i * 0.05, sample_size=5,
            )
        weak = await cap_crud.get_weakest(db, max_confidence=0.5, limit=2)
        assert len(weak) == 2
        assert weak[0]["domain"] == "d0"  # lowest confidence first

    @pytest.mark.asyncio
    async def test_empty_when_all_strong(self, db):
        await cap_crud.upsert(db, domain="strong", confidence=0.95, sample_size=10)
        weak = await cap_crud.get_weakest(db, max_confidence=0.5)
        assert weak == []


async def _insert_aged(db, domain, *, confidence, sample_size, days_ago,
                       anchor="2026-08-27T13:00:00+00:00"):
    """Insert a row whose updated_at sits *days_ago* behind *anchor*.

    Written straight to SQL because ``upsert`` always stamps ``now`` — the
    recency window is only observable with controlled timestamps.
    """
    await db.execute(
        "INSERT INTO capability_map (id, domain, confidence, sample_size, "
        "trend, evidence_summary, updated_at) VALUES (?, ?, ?, ?, 'stable', "
        "'', datetime(?, ?))",
        (f"id-{domain}", domain, confidence, sample_size, anchor,
         f"-{days_ago} days"),
    )
    await db.commit()


class TestRecencyWindow:
    """Stale rows are hidden from the prompt-facing reads.

    A row stops being rewritten when its domain no longer clears the
    aggregator's gates, so a long-stale row asserts present-tense capability on
    evidence that may be months old. The window is anchored on the freshest row
    rather than wall-clock, so a dead refresh job cannot silently blank the map.
    """

    @pytest.mark.asyncio
    async def test_stale_row_hidden_from_prompt_read(self, db):
        await _insert_aged(db, "fresh", confidence=0.5, sample_size=5, days_ago=0)
        await _insert_aged(db, "ancient", confidence=0.9, sample_size=9, days_ago=90)

        domains = [e["domain"] for e in await cap_crud.get_prompt_rows(db)]
        assert "fresh" in domains
        assert "ancient" not in domains

    @pytest.mark.asyncio
    async def test_recently_quiet_row_is_kept(self, db):
        """A domain that merely dipped under a noise gate for a week stays.

        This is the false-positive guard: 6 days behind is the observed shape of
        a live-but-quiet domain, not a dead one.
        """
        await _insert_aged(db, "fresh", confidence=0.5, sample_size=5, days_ago=0)
        await _insert_aged(db, "quiet", confidence=0.8, sample_size=4, days_ago=6)

        domains = [e["domain"] for e in await cap_crud.get_prompt_rows(db)]
        assert "quiet" in domains

    @pytest.mark.asyncio
    async def test_none_disables_the_window(self, db):
        await _insert_aged(db, "fresh", confidence=0.5, sample_size=5, days_ago=0)
        await _insert_aged(db, "ancient", confidence=0.9, sample_size=9, days_ago=90)

        entries = await cap_crud.get_prompt_rows(db, stale_after_days=None)
        assert {e["domain"] for e in entries} == {"fresh", "ancient"}

    @pytest.mark.asyncio
    async def test_dead_refresh_job_hides_nothing(self, db):
        """The MAX-anchor property, and the reason it is not wall-clock.

        Every row is 90 days old — a refresh job that died three months ago.
        A wall-clock window would return NOTHING and silently blank the ego's
        self-model; anchoring on the freshest row returns everything, because
        the table is uniformly old rather than selectively stale.
        """
        for name in ("alpha", "beta", "gamma"):
            await _insert_aged(db, name, confidence=0.7, sample_size=5, days_ago=90)

        entries = await cap_crud.get_prompt_rows(db)
        assert {e["domain"] for e in entries} == {"alpha", "beta", "gamma"}

    @pytest.mark.asyncio
    async def test_get_weakest_skips_stale_domains(self, db):
        """The measured live harm: a months-dead domain driving the scanner."""
        await _insert_aged(db, "dead_executor", confidence=0.0, sample_size=3,
                           days_ago=93)
        await _insert_aged(db, "live_weak", confidence=0.35, sample_size=8,
                           days_ago=0)

        weak = await cap_crud.get_weakest(db)
        domains = [e["domain"] for e in weak]
        assert "dead_executor" not in domains
        assert "live_weak" in domains

    @pytest.mark.asyncio
    async def test_get_weakest_none_disables_the_window(self, db):
        await _insert_aged(db, "dead_executor", confidence=0.0, sample_size=3,
                           days_ago=93)
        await _insert_aged(db, "live_weak", confidence=0.35, sample_size=8,
                           days_ago=0)

        weak = await cap_crud.get_weakest(db, stale_after_days=None)
        assert weak[0]["domain"] == "dead_executor"

    @pytest.mark.asyncio
    async def test_get_by_domain_is_never_filtered(self, db):
        """A targeted read must bypass the window.

        The ego's focused-deficiency lookup asks for one domain BECAUSE it is
        weak; that read must resolve even when the domain is stale, or the
        advisory silently loses the deficiency it was written to name.
        """
        await _insert_aged(db, "fresh", confidence=0.5, sample_size=5, days_ago=0)
        await _insert_aged(db, "ancient", confidence=0.9, sample_size=9, days_ago=90)

        entry = await cap_crud.get_by_domain(db, "ancient")
        assert entry is not None
        assert entry["domain"] == "ancient"


class TestSampleSizeFloor:
    """``get_prompt_rows`` hides rows too thin to be a capability.

    The aggregator refuses to WRITE such rows, but the table still holds ones
    written before that floor existed. Since this read is confidence-ordered and
    the callers render a top-N, a single-sample row would otherwise displace a
    domain backed by dozens of samples in the ego's own self-portrait.
    """

    @pytest.mark.asyncio
    async def test_thin_row_hidden_from_prompt_read(self, db):
        await cap_crud.upsert(db, domain="solid", confidence=0.5, sample_size=40)
        await cap_crud.upsert(db, domain="one_off", confidence=0.95, sample_size=1)

        domains = [e["domain"] for e in await cap_crud.get_prompt_rows(db)]
        assert "solid" in domains
        assert "one_off" not in domains

    @pytest.mark.asyncio
    async def test_boundary_three_samples_is_kept(self, db):
        await cap_crud.upsert(db, domain="two", confidence=0.9, sample_size=2)
        await cap_crud.upsert(db, domain="three", confidence=0.9, sample_size=3)

        domains = [e["domain"] for e in await cap_crud.get_prompt_rows(db)]
        assert "three" in domains
        assert "two" not in domains

    @pytest.mark.asyncio
    async def test_none_disables_the_sample_floor(self, db):
        await cap_crud.upsert(db, domain="one_off", confidence=0.95, sample_size=1)

        entries = await cap_crud.get_prompt_rows(db, min_sample_size=None)
        assert [e["domain"] for e in entries] == ["one_off"]

    @pytest.mark.asyncio
    async def test_thin_row_cannot_displace_a_well_evidenced_one(self, db):
        """The measured live shape: a lone procedure outranking real evidence."""
        await cap_crud.upsert(db, domain="inbox_evaluation", confidence=0.91,
                              sample_size=70)
        await cap_crud.upsert(db, domain="pip_editable_worktree_safety",
                              confidence=0.90, sample_size=1)

        entries = await cap_crud.get_prompt_rows(db)
        assert [e["domain"] for e in entries] == ["inbox_evaluation"]


class TestGetAllIsUnfiltered:
    """``get_all`` is a plain accessor — the bars live in ``get_prompt_rows``.

    It currently has no production caller: every prompt-facing reader was moved
    to ``get_prompt_rows``. It is kept as the unfiltered CRUD read, and pinned
    here because the two functions sit one line apart — a future edit that
    "helpfully" filtered ``get_all`` would silently apply ego-prompt policy to
    whatever reads it next.
    """

    @pytest.mark.asyncio
    async def test_returns_stale_and_thin_rows(self, db):
        await _insert_aged(db, "fresh", confidence=0.5, sample_size=5, days_ago=0)
        await _insert_aged(db, "ancient", confidence=0.9, sample_size=9, days_ago=90)
        await cap_crud.upsert(db, domain="one_off", confidence=0.95, sample_size=1)

        domains = {e["domain"] for e in await cap_crud.get_all(db)}
        assert domains == {"fresh", "ancient", "one_off"}


class TestStaleWindowBoundary:
    """Pins the threshold itself.

    Without this the constant could be retuned to anything from 7 to 89 days
    with the whole suite still green — the other fixtures only probe 0/6/90/93,
    so they prove "a window exists", not which one.
    """

    @pytest.mark.asyncio
    async def test_thirteen_days_kept_fifteen_hidden(self, db):
        await _insert_aged(db, "fresh", confidence=0.5, sample_size=5, days_ago=0)
        await _insert_aged(db, "d13", confidence=0.5, sample_size=5, days_ago=13)
        await _insert_aged(db, "d15", confidence=0.5, sample_size=5, days_ago=15)

        domains = {e["domain"] for e in await cap_crud.get_prompt_rows(db)}
        assert "d13" in domains
        assert "d15" not in domains

    @pytest.mark.asyncio
    async def test_get_weakest_shares_the_same_boundary(self, db):
        await _insert_aged(db, "fresh", confidence=0.9, sample_size=5, days_ago=0)
        await _insert_aged(db, "w13", confidence=0.2, sample_size=5, days_ago=13)
        await _insert_aged(db, "w15", confidence=0.1, sample_size=5, days_ago=15)

        domains = [e["domain"] for e in await cap_crud.get_weakest(db)]
        assert "w13" in domains
        assert "w15" not in domains


class TestAnchorIsClamped:
    """A future-dated row must not be able to blank the map.

    The window anchors on the freshest row, and MAX() is unbounded ABOVE. One
    row stamped ahead of real time therefore defines the window for every other
    row and can hide all of them — silently, since nothing logs it. Worse, a
    poisoned anchor is self-perpetuating: once the skewed domain stops being
    emitted nothing rewrites it, so the anchor never returns on its own.

    Not hypothetical in this repo — the heartbeat GC had the same clock-skew
    shape (a future row starving liveness) and was fixed for it.
    """

    @staticmethod
    async def _insert_at(db, domain, offset, *, confidence=0.5, sample_size=5,
                         anchor="2026-08-27T13:00:00+00:00"):
        await db.execute(
            "INSERT INTO capability_map (id, domain, confidence, sample_size, "
            "trend, evidence_summary, updated_at) "
            "VALUES (?, ?, ?, ?, 'stable', '', datetime(?, ?))",
            (f"id-{domain}", domain, confidence, sample_size, anchor, offset),
        )
        await db.commit()

    @pytest.mark.asyncio
    async def test_future_row_does_not_hide_healthy_rows(self, db):
        for i in range(4):
            await self._insert_at(db, f"healthy{i}", "-0 days")
        await self._insert_at(db, "skewed", "+100 days")

        domains = {e["domain"] for e in await cap_crud.get_prompt_rows(db)}
        assert {f"healthy{i}" for i in range(4)} <= domains, (
            "a single clock-skewed row blanked the healthy rows"
        )

    @pytest.mark.asyncio
    async def test_future_row_does_not_monopolise_get_weakest(self, db):
        """The scanner must not be handed only the poisoned domain."""
        await self._insert_at(db, "real_weak", "-0 days", confidence=0.2)
        await self._insert_at(db, "skewed", "+100 days", confidence=0.1)

        domains = [e["domain"] for e in await cap_crud.get_weakest(db)]
        assert "real_weak" in domains

    @pytest.mark.asyncio
    async def test_clamp_does_not_break_the_dead_job_property(self, db):
        """Clamping must not reintroduce wall-clock behaviour.

        Every row far in the past = a refresh job that died. The table is
        uniformly old, so nothing should be hidden.
        """
        for i in range(3):
            await self._insert_at(db, f"old{i}", "-400 days")

        domains = {e["domain"] for e in await cap_crud.get_prompt_rows(db)}
        assert domains == {"old0", "old1", "old2"}


class TestProductionTimestampFormat:
    """Rows written the way ``upsert`` writes them, not the way fixtures do.

    ``upsert`` stores ``datetime.now(UTC).isoformat()`` — ``T``-separated with a
    ``+00:00`` offset. The other fixtures here use SQLite's own
    ``datetime(?, ?)`` output, which is space-separated. A lexical comparison
    happens to work on the fixture shape, so the ``datetime()`` normalisation in
    the clause is inert under those tests while being load-bearing in
    production: ``'…T12:00:00+00:00' >= '… 13:00:00'`` is lexically TRUE because
    ``T`` sorts above a space.
    """

    @pytest.mark.asyncio
    async def test_window_works_on_upsert_written_rows(self, db):
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        await cap_crud.upsert(db, domain="fresh", confidence=0.5, sample_size=5)
        await cap_crud.upsert(db, domain="ancient", confidence=0.9, sample_size=9)
        # Age one row using the SAME isoformat shape upsert produces.
        await db.execute(
            "UPDATE capability_map SET updated_at = ? WHERE domain = 'ancient'",
            ((now - timedelta(days=90)).isoformat(),),
        )
        await db.commit()

        domains = {e["domain"] for e in await cap_crud.get_prompt_rows(db)}
        assert "fresh" in domains
        assert "ancient" not in domains

    @pytest.mark.asyncio
    async def test_boundary_is_exact_at_fourteen_days(self, db):
        """`>=` not `>`: exactly 14 days behind the anchor is still shown."""
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        await cap_crud.upsert(db, domain="anchor", confidence=0.5, sample_size=5)
        await cap_crud.upsert(db, domain="exactly14", confidence=0.5, sample_size=5)
        await db.execute(
            "UPDATE capability_map SET updated_at = ? WHERE domain = 'exactly14'",
            ((now - timedelta(days=14)).isoformat(),),
        )
        await db.commit()

        domains = {e["domain"] for e in await cap_crud.get_prompt_rows(db)}
        assert "exactly14" in domains


class TestConfidenceTieBreak:
    """Ties on confidence resolve toward the better-evidenced domain.

    Confidence is a ratio, so well-exercised domains pile up at exactly 1.0 —
    measured at 19 such rows on a live install, more than filling a 15-row
    table. Without a secondary key SQLite returns an arbitrary subset, and an
    n=3 row can displace one with n=3276. The sample floor cannot fix this:
    every tied row already clears it.
    """

    @pytest.mark.asyncio
    async def test_tied_confidence_orders_by_sample_size(self, db):
        await cap_crud.upsert(db, domain="thin_tie", confidence=1.0, sample_size=3)
        await cap_crud.upsert(db, domain="rich_tie", confidence=1.0, sample_size=3276)

        entries = await cap_crud.get_prompt_rows(db)
        assert [e["domain"] for e in entries] == ["rich_tie", "thin_tie"]

    @pytest.mark.asyncio
    async def test_confidence_still_dominates_sample_size(self, db):
        """The tiebreak is secondary — it must not reorder distinct scores."""
        await cap_crud.upsert(db, domain="high_thin", confidence=0.9, sample_size=3)
        await cap_crud.upsert(db, domain="low_rich", confidence=0.2, sample_size=5000)

        entries = await cap_crud.get_prompt_rows(db)
        assert [e["domain"] for e in entries] == ["high_thin", "low_rich"]

    @pytest.mark.asyncio
    async def test_get_weakest_tie_prefers_better_evidence(self, db):
        await cap_crud.upsert(db, domain="weak_thin", confidence=0.2, sample_size=3)
        await cap_crud.upsert(db, domain="weak_rich", confidence=0.2, sample_size=900)

        weak = await cap_crud.get_weakest(db, limit=1)
        assert [e["domain"] for e in weak] == ["weak_rich"]


class TestNegativeWindowRejected:
    @pytest.mark.asyncio
    async def test_negative_stale_after_days_raises(self, db):
        """Silently returning an empty map would look like a healthy filter."""
        with pytest.raises(ValueError, match="stale_after_days"):
            await cap_crud.get_prompt_rows(db, stale_after_days=-5)


class TestEmptyTableFailDirection:
    """An empty table must return empty, not raise.

    The clamped anchor uses SQLite's scalar ``min()``, which returns NULL if any
    argument is NULL — and ``MAX(updated_at)`` over an empty table IS NULL. That
    makes the whole comparison NULL and filters everything, which is the right
    answer here (there was nothing to show) but is worth pinning: it is the one
    input where the clause evaluates to NULL rather than a boolean.
    """

    @pytest.mark.asyncio
    async def test_prompt_rows_on_empty_table(self, db):
        assert await cap_crud.get_prompt_rows(db) == []

    @pytest.mark.asyncio
    async def test_weakest_on_empty_table(self, db):
        assert await cap_crud.get_weakest(db) == []

    @pytest.mark.asyncio
    async def test_single_row_is_its_own_anchor(self, db):
        """One row: it is both MAX and the candidate, so it must survive."""
        await cap_crud.upsert(db, domain="only", confidence=0.5, sample_size=5)
        assert [e["domain"] for e in await cap_crud.get_prompt_rows(db)] == ["only"]


class TestWindowExcludesInBothDirections:
    """Every window test above asserts a row IS returned. That cannot fail.

    The bug this guards is OVER-inclusion, and an assert-included test is blind
    to it: a lexical string compare instead of ``julianday()`` leaves the suite
    fully green while wrongly keeping stale rows. The collision band is narrow —
    same calendar date as the threshold, earlier clock time — because
    ``upsert`` writes ``'…T12:00:00+00:00'`` while SQLite renders
    ``'… 13:00:00'``, and ``T`` (0x54) sorts above a space (0x20).

    So this asserts ABSENCE, which is the only direction that kills the mutant.
    """

    @pytest.mark.asyncio
    async def test_same_date_earlier_clock_time_is_excluded(self, db):
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        await cap_crud.upsert(db, domain="anchor", confidence=0.5, sample_size=5)
        await cap_crud.upsert(db, domain="just_stale", confidence=0.5, sample_size=5)
        # 14d and 30min behind: past the window, but on the same calendar date
        # as the threshold and at an earlier clock time — the band where a
        # lexical compare gives the wrong answer.
        await db.execute(
            "UPDATE capability_map SET updated_at = ? WHERE domain = 'just_stale'",
            ((now - timedelta(days=14, minutes=30)).isoformat(),),
        )
        await db.commit()

        domains = {e["domain"] for e in await cap_crud.get_prompt_rows(db)}
        assert "anchor" in domains
        assert "just_stale" not in domains, (
            "stale row kept — the timestamp comparison is not normalising"
        )

    @pytest.mark.asyncio
    async def test_get_weakest_excludes_in_the_same_band(self, db):
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        await cap_crud.upsert(db, domain="anchor", confidence=0.9, sample_size=5)
        await cap_crud.upsert(db, domain="stale_weak", confidence=0.1, sample_size=5)
        await db.execute(
            "UPDATE capability_map SET updated_at = ? WHERE domain = 'stale_weak'",
            ((now - timedelta(days=14, minutes=30)).isoformat(),),
        )
        await db.commit()

        assert "stale_weak" not in [e["domain"] for e in await cap_crud.get_weakest(db)]

    @pytest.mark.asyncio
    async def test_unparseable_timestamp_does_not_hide_every_row(self, db):
        """The COALESCE fail-direction — the one input no other fixture reaches.

        ``julianday('garbage')`` is NULL, and ``MAX()`` picks that row lexically
        because 'g' sorts above any ISO leading digit. Scalar ``min()`` returns
        NULL if ANY argument is NULL, so without COALESCE the whole predicate is
        NULL and EVERY row disappears — silently. The empty-table fixture passes
        identically with or without the guard, so it pins nothing; only a
        non-empty table with one bad row does.
        """
        for i in range(3):
            await cap_crud.upsert(
                db, domain=f"healthy{i}", confidence=0.9, sample_size=50
            )
        await db.execute(
            "INSERT INTO capability_map (id, domain, confidence, sample_size, "
            "trend, evidence_summary, updated_at) "
            "VALUES ('bad', 'bad_domain', 0.5, 50, 'stable', '', 'garbage')"
        )
        await db.commit()

        domains = {e["domain"] for e in await cap_crud.get_prompt_rows(db)}
        assert domains >= {"healthy0", "healthy1", "healthy2"}, (
            "one unparseable updated_at blanked the map — COALESCE is missing"
        )
        assert await cap_crud.get_weakest(db, max_confidence=1.0), (
            "get_weakest blanked by the same NULL anchor"
        )


class TestMalformedAnchorDuringOutage:
    """The corruption case the COALESCE fallback was added to tolerate.

    ``MAX(updated_at)`` is a LEXICAL max over text, so a malformed value like
    ``zzzz-not-a-time`` outranks every ISO timestamp. ``julianday()`` then
    returns NULL for it, COALESCE substitutes wall-clock now, and every
    uniformly-old row falls outside the window — defeating the dead-refresh-job
    guarantee in exactly the corruption case the fallback exists to tolerate.

    The earlier fixture paired a malformed row only with FRESH rows, where
    substituting now happens to give the right answer, so it could not see this.
    """

    @staticmethod
    async def _insert(db, domain, updated_at, *, confidence=0.7, sample_size=9):
        await db.execute(
            "INSERT INTO capability_map (id, domain, confidence, sample_size, "
            "trend, evidence_summary, updated_at) "
            "VALUES (?, ?, ?, ?, 'stable', '', ?)",
            (f"id-{domain}", domain, confidence, sample_size, updated_at),
        )
        await db.commit()

    @pytest.mark.asyncio
    async def test_dead_job_plus_malformed_row_still_hides_nothing(self, db):
        from datetime import UTC, datetime, timedelta

        old_stamp = (datetime.now(UTC) - timedelta(days=120)).isoformat()
        for i in range(3):
            await self._insert(db, f"aged{i}", old_stamp)
        # Lexically greater than any ISO timestamp, and unparseable.
        await self._insert(db, "corrupt", "zzzz-not-a-time")

        domains = {e["domain"] for e in await cap_crud.get_prompt_rows(db)}
        assert {"aged0", "aged1", "aged2"} <= domains, (
            "a malformed anchor collapsed the dead-refresh guarantee — every "
            "uniformly-old row was hidden"
        )

    @pytest.mark.asyncio
    async def test_malformed_row_does_not_shift_a_healthy_window(self, db):
        """The anchor must come from the newest PARSEABLE row, not the garbage."""
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        await self._insert(db, "fresh", now.isoformat())
        await self._insert(db, "stale", (now - timedelta(days=90)).isoformat())
        await self._insert(db, "corrupt", "zzzz-not-a-time")

        domains = {e["domain"] for e in await cap_crud.get_prompt_rows(db)}
        assert "fresh" in domains
        assert "stale" not in domains, "the window stopped excluding stale rows"
