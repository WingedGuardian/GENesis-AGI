"""Tests for the capability aggregator."""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.ego.capability_aggregator import compute_capability_map


@pytest.fixture
async def db(tmp_path):
    """DB with minimal tables for aggregation testing."""
    db_path = str(tmp_path / "test.db")
    async with aiosqlite.connect(db_path) as conn:
        # intervention_journal
        await conn.execute("""
            CREATE TABLE intervention_journal (
                id TEXT PRIMARY KEY, ego_source TEXT, proposal_id TEXT,
                cycle_id TEXT, action_type TEXT NOT NULL,
                action_summary TEXT NOT NULL, expected_outcome TEXT DEFAULT '',
                actual_outcome TEXT, outcome_status TEXT DEFAULT 'pending',
                user_response TEXT, confidence REAL DEFAULT 0.0,
                created_at TEXT NOT NULL, resolved_at TEXT
            )
        """)
        # ego_proposals
        await conn.execute("""
            CREATE TABLE ego_proposals (
                id TEXT PRIMARY KEY, action_type TEXT NOT NULL,
                action_category TEXT DEFAULT '', content TEXT NOT NULL,
                rationale TEXT DEFAULT '', confidence REAL DEFAULT 0.0,
                urgency TEXT DEFAULT 'normal',
                status TEXT DEFAULT 'pending',
                user_response TEXT, cycle_id TEXT, batch_id TEXT,
                created_at TEXT NOT NULL, resolved_at TEXT,
                expires_at TEXT, rank INTEGER,
                execution_plan TEXT, recurring INTEGER DEFAULT 0,
                memory_basis TEXT DEFAULT ''
            )
        """)
        # autonomy_state
        await conn.execute("""
            CREATE TABLE autonomy_state (
                id TEXT PRIMARY KEY, person_id TEXT,
                category TEXT NOT NULL, current_level INTEGER DEFAULT 1,
                earned_level INTEGER DEFAULT 1,
                context_ceiling INTEGER,
                consecutive_corrections INTEGER DEFAULT 0,
                total_successes INTEGER DEFAULT 0,
                total_corrections INTEGER DEFAULT 0,
                last_correction_at TEXT, last_regression_at TEXT,
                regression_reason TEXT, updated_at TEXT
            )
        """)
        # procedural_memory
        await conn.execute("""
            CREATE TABLE procedural_memory (
                id TEXT PRIMARY KEY, task_type TEXT NOT NULL,
                confidence REAL DEFAULT 0.0, deprecated INTEGER DEFAULT 0,
                quarantined INTEGER DEFAULT 0, draft INTEGER DEFAULT 1,
                success_count INTEGER DEFAULT 0, failure_count INTEGER DEFAULT 0,
                invocation_count INTEGER DEFAULT 0,
                activation_tier TEXT DEFAULT 'DORMANT',
                tool_trigger TEXT,
                created_at TEXT, updated_at TEXT
            )
        """)
        # cc_sessions
        await conn.execute("""
            CREATE TABLE cc_sessions (
                id TEXT PRIMARY KEY, session_type TEXT NOT NULL,
                model TEXT NOT NULL, effort TEXT DEFAULT 'medium',
                status TEXT, source_tag TEXT DEFAULT 'foreground',
                cost_usd REAL, started_at TEXT NOT NULL,
                completed_at TEXT, metadata TEXT
            )
        """)
        # outcome_events (6th source — tier-1 ground truth, surplus-scoped)
        await conn.execute("""
            CREATE TABLE outcome_events (
                id TEXT PRIMARY KEY, source TEXT NOT NULL,
                ref_type TEXT NOT NULL, ref_id TEXT NOT NULL, domain TEXT,
                signal_type TEXT NOT NULL,
                signal_class TEXT NOT NULL DEFAULT 'implicit',
                signal_tier INTEGER NOT NULL,
                polarity TEXT, value REAL, stated_confidence REAL,
                prediction_error REAL, reason TEXT, reason_text TEXT,
                metadata TEXT, harvested_from TEXT,
                occurred_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE (source, ref_type, ref_id, signal_type)
            )
        """)
        await conn.commit()
        yield conn


async def _add_surplus_outcomes(db, domain, *, positive, negative):
    """Seed N tier-1 surplus execution-outcome rows for a domain (recent)."""
    from datetime import UTC, datetime
    now = datetime.now(UTC).isoformat()
    i = 0
    for pol, count in (("positive", positive), ("negative", negative)):
        for _ in range(count):
            await db.execute(
                "INSERT INTO outcome_events "
                "(id, source, ref_type, ref_id, domain, signal_type, "
                " signal_tier, polarity, value, occurred_at) "
                "VALUES (?, 'surplus', 'task', ?, ?, 'execution_outcome', "
                " 1, ?, ?, ?)",
                (f"oe-{domain}-{i}", f"t-{domain}-{i}", domain, pol,
                 1.0 if pol == "positive" else 0.0, now),
            )
            i += 1
    await db.commit()


class TestComputeCapabilityMap:
    @pytest.mark.asyncio
    async def test_empty_tables_returns_empty(self, db):
        results = await compute_capability_map(db)
        assert results == []

    @pytest.mark.asyncio
    async def test_journal_data_only(self, db):
        """Intervention journal data produces capability entries."""
        from datetime import UTC, datetime
        now = datetime.now(UTC).isoformat()
        # 3 investigate proposals: 2 approved, 1 rejected
        for i, status in enumerate(["approved", "approved", "rejected"]):
            await db.execute(
                "INSERT INTO intervention_journal "
                "(id, ego_source, proposal_id, cycle_id, action_type, "
                "action_summary, outcome_status, confidence, created_at, resolved_at) "
                "VALUES (?, 'user_ego', ?, ?, 'investigate', 'test', ?, 0.8, ?, ?)",
                (f"j{i}", f"p{i}", f"c{i}", status, now, now),
            )
        await db.commit()

        results = await compute_capability_map(db)
        assert len(results) >= 1
        investigate = next((r for r in results if r["domain"] == "investigate"), None)
        assert investigate is not None
        # 2/3 approved → ~67%
        assert 0.6 <= investigate["confidence"] <= 0.7

    @pytest.mark.asyncio
    async def test_multiple_sources_weighted(self, db):
        """Multiple data sources for same domain are weighted by sample size."""
        from datetime import UTC, datetime
        now = datetime.now(UTC).isoformat()

        # Journal: 1 approved out of 1 → 100% but small sample
        await db.execute(
            "INSERT INTO intervention_journal "
            "(id, ego_source, action_type, action_summary, outcome_status, "
            "confidence, created_at, resolved_at) "
            "VALUES ('j1', 'user_ego', 'investigate', 'test', 'approved', 0.8, ?, ?)",
            (now, now),
        )
        # Proposals: 5 approved out of 10 → 50% with larger sample
        for i in range(10):
            status = "approved" if i < 5 else "rejected"
            await db.execute(
                "INSERT INTO ego_proposals "
                "(id, action_type, content, status, created_at) "
                "VALUES (?, 'investigate', 'test', ?, ?)",
                (f"p{i}", status, now),
            )
        await db.commit()

        results = await compute_capability_map(db)
        investigate = next((r for r in results if r["domain"] == "investigate"), None)
        assert investigate is not None
        # Inverse confidence weighting: journal (100%, weight=1.0) and
        # proposals (50%, weight=1.5). The weaker signal gets more influence,
        # pulling toward 50%. Result is ~0.7 (sample size not used in ICW).
        assert 0.65 <= investigate["confidence"] <= 0.75

    @pytest.mark.asyncio
    async def test_autonomy_state_contributes(self, db):
        """Autonomy Bayesian posteriors contribute to the map."""
        await db.execute(
            "INSERT INTO autonomy_state "
            "(id, category, total_successes, total_corrections) "
            "VALUES ('a1', 'outreach', 8, 2)",
        )
        await db.commit()

        results = await compute_capability_map(db)
        outreach = next((r for r in results if r["domain"] == "outreach"), None)
        assert outreach is not None
        # Posterior = (8+1)/(8+2+2) = 9/12 = 0.75
        assert 0.7 <= outreach["confidence"] <= 0.8

    @pytest.mark.asyncio
    async def test_results_sorted_by_confidence(self, db):
        """Results come back sorted highest confidence first."""
        await db.execute(
            "INSERT INTO autonomy_state (id, category, total_successes, total_corrections) "
            "VALUES ('a1', 'low_domain', 1, 8)"
        )
        await db.execute(
            "INSERT INTO autonomy_state (id, category, total_successes, total_corrections) "
            "VALUES ('a2', 'high_domain', 9, 1)"
        )
        await db.commit()

        results = await compute_capability_map(db)
        assert len(results) == 2
        assert results[0]["domain"] == "high_domain"
        assert results[1]["domain"] == "low_domain"


class TestOutcomeBusSource:
    """The 6th source (Outcome Bus tier-1) is gated behind the default-OFF
    ``outcome_bus_capability_feed`` ego flag. OFF = behaviour-neutral; ON folds
    surplus-scoped ground truth in. ``load_ego_config`` is patched so the tests
    are hermetic regardless of the machine's ego.yaml."""

    def _patch_flag(self, monkeypatch, enabled):
        from genesis.ego.types import EgoConfig
        monkeypatch.setattr(
            "genesis.ego.config.load_ego_config",
            lambda *a, **k: EgoConfig(outcome_bus_capability_feed=enabled),
        )

    @pytest.mark.asyncio
    async def test_flag_off_no_surplus_signal(self, db, monkeypatch):
        """Default OFF: surplus rows exist but contribute nothing."""
        self._patch_flag(monkeypatch, False)
        await _add_surplus_outcomes(db, "code_audit", positive=7, negative=1)

        results = await compute_capability_map(db)
        # surplus-only domain must NOT appear, and no 'outcomes:' evidence anywhere.
        assert not any(r["domain"] == "code_audit" for r in results)
        assert not any("outcomes:" in r.get("evidence", "") for r in results)

    @pytest.mark.asyncio
    async def test_flag_off_output_identical_to_baseline(self, db, monkeypatch):
        """OFF output is byte-identical to the no-surplus baseline — proves the
        6th source is truly inert when disabled."""
        await db.execute(
            "INSERT INTO autonomy_state (id, category, total_successes, "
            "total_corrections) VALUES ('a1', 'outreach', 8, 2)"
        )
        await db.commit()

        self._patch_flag(monkeypatch, False)
        baseline = await compute_capability_map(db)
        # Now add surplus rows; with the flag OFF the result must not change.
        await _add_surplus_outcomes(db, "code_audit", positive=7, negative=1)
        after = await compute_capability_map(db)
        assert after == baseline

    @pytest.mark.asyncio
    async def test_flag_on_adds_surplus_domain(self, db, monkeypatch):
        """ON: a surplus-only domain appears with 'outcomes:' evidence and the
        correct success rate (7/8 = 87.5%)."""
        self._patch_flag(monkeypatch, True)
        await _add_surplus_outcomes(db, "code_audit", positive=7, negative=1)

        results = await compute_capability_map(db)
        ca = next((r for r in results if r["domain"] == "code_audit"), None)
        assert ca is not None
        assert "outcomes:" in ca["evidence"]
        # Single source → composite == the surplus rate 7/8 = 0.875.
        assert abs(ca["confidence"] - 0.875) < 0.01
        assert ca["sample_size"] == 8

    @pytest.mark.asyncio
    async def test_flag_on_noise_gate_skips_thin_domains(self, db, monkeypatch):
        """ON: a domain with n < 3 surplus rows is skipped (mirrors the
        cc_sessions HAVING total >= 3 gate)."""
        self._patch_flag(monkeypatch, True)
        await _add_surplus_outcomes(db, "thin", positive=2, negative=0)   # n=2
        await _add_surplus_outcomes(db, "thick", positive=3, negative=0)  # n=3

        results = await compute_capability_map(db)
        assert not any(r["domain"] == "thin" for r in results)
        assert any(r["domain"] == "thick" for r in results)

    @pytest.mark.asyncio
    async def test_flag_on_combines_with_existing_source(self, db, monkeypatch):
        """ON: when a domain has BOTH a non-surplus source and surplus rows, the
        composite reflects both signals (evidence carries both tags)."""
        from datetime import UTC, datetime
        now = datetime.now(UTC).isoformat()
        # 10 'investigate' proposals, 5 approved → proposals signal 50%.
        for i in range(10):
            status = "approved" if i < 5 else "rejected"
            await db.execute(
                "INSERT INTO ego_proposals (id, action_type, content, status, "
                "created_at) VALUES (?, 'investigate', 'test', ?, ?)",
                (f"p{i}", status, now),
            )
        await db.commit()
        self._patch_flag(monkeypatch, True)
        await _add_surplus_outcomes(db, "investigate", positive=8, negative=0)

        results = await compute_capability_map(db)
        inv = next((r for r in results if r["domain"] == "investigate"), None)
        assert inv is not None
        assert "proposals:" in inv["evidence"]
        assert "outcomes:" in inv["evidence"]


class TestVoiceRowExclusion:
    @pytest.mark.asyncio
    async def test_voice_rows_never_form_a_domain(self, db):
        """Voice conversation rows (source_tag='voice') are transcript-index
        entries, always 'completed' — including them would fabricate a
        100%-complete phantom capability domain."""
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()
        for i in range(5):
            await db.execute(
                "INSERT INTO cc_sessions (id, session_type, model, status, "
                "source_tag, started_at) "
                "VALUES (?, 'foreground', 'voice', 'completed', 'voice', ?)",
                (f"v{i}", now),
            )
        for i in range(4):
            await db.execute(
                "INSERT INTO cc_sessions (id, session_type, model, status, "
                "source_tag, started_at) "
                "VALUES (?, 'background_task', 'sonnet', 'completed', 'sentinel', ?)",
                (f"s{i}", now),
            )
        await db.commit()

        results = await compute_capability_map(db)
        assert not any(r["domain"] == "voice" for r in results)
        assert any(r["domain"] == "sentinel" for r in results)


class TestMinimumSampleFloor:
    """A domain needs enough evidence to be a domain at all.

    Sources 5 and 6 already refuse to emit a signal below 3 samples, but the
    journal / proposals / autonomy / procedural sources had no such floor — so a
    single stored procedure surfaced as a full capability domain and, being
    sorted by confidence, could outrank domains backed by dozens of samples in
    the top-15 the ego actually reads. The floor is applied to the COMBINED
    sample size so it cannot be dodged by spreading thin evidence across
    sources.
    """

    def _patch_flag(self, monkeypatch, enabled=False):
        from genesis.ego.types import EgoConfig
        monkeypatch.setattr(
            "genesis.ego.config.load_ego_config",
            lambda *a, **k: EgoConfig(outcome_bus_capability_feed=enabled),
        )

    async def _add_procedures(self, db, task_type, count, confidence=0.9):
        for i in range(count):
            await db.execute(
                "INSERT INTO procedural_memory (id, task_type, confidence, "
                "deprecated, quarantined) VALUES (?, ?, ?, 0, 0)",
                (f"pm-{task_type}-{i}", task_type, confidence),
            )
        await db.commit()

    @pytest.mark.asyncio
    async def test_single_procedure_is_not_a_capability(self, db, monkeypatch):
        """The live-data shape: one procedure must not become a 90% domain."""
        self._patch_flag(monkeypatch)
        await self._add_procedures(db, "pip_editable_worktree_safety", 1)

        results = await compute_capability_map(db)
        assert not any(
            r["domain"] == "pip_editable_worktree_safety" for r in results
        )

    @pytest.mark.asyncio
    async def test_two_samples_still_below_floor(self, db, monkeypatch):
        self._patch_flag(monkeypatch)
        await self._add_procedures(db, "thin_domain", 2)

        results = await compute_capability_map(db)
        assert not any(r["domain"] == "thin_domain" for r in results)

    @pytest.mark.asyncio
    async def test_three_samples_clears_the_floor(self, db, monkeypatch):
        """Boundary: the floor is >= 3, not > 3."""
        self._patch_flag(monkeypatch)
        await self._add_procedures(db, "thick_domain", 3)

        results = await compute_capability_map(db)
        assert any(r["domain"] == "thick_domain" for r in results)

    @pytest.mark.asyncio
    async def test_floor_applies_to_combined_sample_size(self, db, monkeypatch):
        """Two sources, each individually thin, together clear the floor.

        Guards against implementing the floor per-source: 1 procedure + a 2-row
        journal is 3 samples of real evidence and must be emitted.
        """
        from datetime import UTC, datetime
        self._patch_flag(monkeypatch)
        now = datetime.now(UTC).isoformat()
        await self._add_procedures(db, "combined", 1)
        for i, status in enumerate(["approved", "rejected"]):
            await db.execute(
                "INSERT INTO intervention_journal (id, ego_source, action_type, "
                "action_summary, outcome_status, confidence, created_at, "
                "resolved_at) VALUES (?, 'user_ego', 'combined', 'test', ?, "
                "0.8, ?, ?)",
                (f"jc{i}", status, now, now),
            )
        await db.commit()

        results = await compute_capability_map(db)
        combined = next((r for r in results if r["domain"] == "combined"), None)
        assert combined is not None
        assert combined["sample_size"] == 3


class TestProposalJournalDoubleCount:
    """One proposal must not count as two observations.

    Creating a proposal batch writes an `ego_proposals` row AND a matching
    `intervention_journal` row for each proposal (ego/session.py). The
    aggregator reads both tables under the same action_type, so a single
    proposal contributes twice — two resolved proposals reach total_weight 4 and
    clear the three-sample floor while representing only two observations. That
    is exactly the thin domain the floor exists to suppress.
    """

    def _patch_flag(self, monkeypatch, enabled=False):
        from genesis.ego.types import EgoConfig
        monkeypatch.setattr(
            "genesis.ego.config.load_ego_config",
            lambda *a, **k: EgoConfig(outcome_bus_capability_feed=enabled),
        )

    @pytest.mark.asyncio
    async def test_two_proposals_do_not_clear_the_floor(self, db, monkeypatch):
        from datetime import UTC, datetime

        self._patch_flag(monkeypatch)
        now = datetime.now(UTC).isoformat()
        # Two proposals, each ALSO journalled — the real write pattern.
        for i, status in enumerate(("approved", "rejected")):
            pid = f"p{i}"
            await db.execute(
                "INSERT INTO ego_proposals (id, action_type, content, status, "
                "created_at) VALUES (?, 'thin_domain', 'x', ?, ?)",
                (pid, status, now),
            )
            await db.execute(
                "INSERT INTO intervention_journal (id, ego_source, proposal_id, "
                "action_type, action_summary, outcome_status, confidence, "
                "created_at, resolved_at) "
                "VALUES (?, 'user_ego', ?, 'thin_domain', 'x', ?, 0.8, ?, ?)",
                (f"j{i}", pid, status, now, now),
            )
        await db.commit()

        results = await compute_capability_map(db)
        assert not any(r["domain"] == "thin_domain" for r in results), (
            "two proposals cleared the three-sample floor by being counted twice"
        )

    @pytest.mark.asyncio
    async def test_an_old_journalled_proposal_still_counts_once(self, db, monkeypatch):
        """Deduplication must not silently drop the journal's long history.

        The journal has no time window and the proposals source has 30 days, so
        a proposal older than the window is represented ONLY by the journal. It
        must still contribute.
        """
        from datetime import UTC, datetime, timedelta

        self._patch_flag(monkeypatch)
        old = (datetime.now(UTC) - timedelta(days=200)).isoformat()
        for i in range(3):
            pid = f"old{i}"
            await db.execute(
                "INSERT INTO ego_proposals (id, action_type, content, status, "
                "created_at) VALUES (?, 'historic', 'x', 'approved', ?)",
                (pid, old),
            )
            await db.execute(
                "INSERT INTO intervention_journal (id, ego_source, proposal_id, "
                "action_type, action_summary, outcome_status, confidence, "
                "created_at, resolved_at) "
                "VALUES (?, 'user_ego', ?, 'historic', 'x', 'approved', 0.8, ?, ?)",
                (f"jo{i}", pid, old, old),
            )
        await db.commit()

        results = await compute_capability_map(db)
        historic = next((r for r in results if r["domain"] == "historic"), None)
        assert historic is not None, "the journal's long history was dropped"
        assert historic["sample_size"] == 3, (
            f"expected 3 distinct observations, got {historic['sample_size']}"
        )
