"""Tests for genesis.reflection.context_gatherer."""

import uuid
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from genesis.db.crud import outreach as outreach_crud
from genesis.db.schema import create_all_tables, seed_data
from genesis.reflection.context_gatherer import ContextGatherer


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await create_all_tables(conn)
        await seed_data(conn)
        yield conn


@pytest.fixture
def gatherer():
    return ContextGatherer()


class TestDetectPendingWork:
    @pytest.mark.asyncio
    async def test_no_pending_work_empty_db(self, db, gatherer):
        pending = await gatherer.detect_pending_work(db)
        assert not pending.memory_consolidation
        # Cost reconciliation removed (2026-06): cost is observability, not a
        # reflection job — the field no longer exists on PendingWorkSummary.
        assert not hasattr(pending, "cost_reconciliation")

    @pytest.mark.asyncio
    async def test_memory_consolidation_below_threshold(self, db, gatherer):
        """Less than 10 unresolved obs → no memory consolidation."""
        now = datetime.now(UTC).isoformat()
        for _i in range(5):
            await db.execute(
                # WS-3: first_party so detect_pending_work (origin-gated) counts them.
                "INSERT INTO observations (id, source, type, content, priority, created_at, origin_class) "
                "VALUES (?, 'test', 'test', 'content', 'low', ?, 'first_party')",
                (str(uuid.uuid4()), now),
            )
        await db.commit()
        pending = await gatherer.detect_pending_work(db)
        assert not pending.memory_consolidation
        assert pending.observation_backlog == 5

    @pytest.mark.asyncio
    async def test_memory_consolidation_above_threshold(self, db, gatherer):
        """10+ unresolved obs → memory consolidation needed."""
        now = datetime.now(UTC).isoformat()
        for _i in range(12):
            await db.execute(
                # WS-3: first_party so detect_pending_work (origin-gated) counts them.
                "INSERT INTO observations (id, source, type, content, priority, created_at, origin_class) "
                "VALUES (?, 'test', 'test', 'content', 'low', ?, 'first_party')",
                (str(uuid.uuid4()), now),
            )
        await db.commit()
        pending = await gatherer.detect_pending_work(db)
        assert pending.memory_consolidation
        assert pending.observation_backlog == 12

    @pytest.mark.asyncio
    async def test_cognitive_state_stale(self, db, gatherer):
        """Cognitive state older than 24h → regeneration needed."""
        old_time = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
        await db.execute(
            "INSERT INTO cognitive_state (id, content, section, generated_by, created_at) "
            "VALUES ('cog1', 'old state', 'active_context', 'test', ?)",
            (old_time,),
        )
        await db.commit()
        pending = await gatherer.detect_pending_work(db)
        assert pending.cognitive_regeneration

    @pytest.mark.asyncio
    async def test_cognitive_state_fresh(self, db, gatherer):
        """Recent cognitive state → no regeneration needed."""
        now = datetime.now(UTC).isoformat()
        await db.execute(
            "INSERT INTO cognitive_state (id, content, section, generated_by, created_at) "
            "VALUES ('cog1', 'fresh state', 'active_context', 'test', ?)",
            (now,),
        )
        await db.commit()
        pending = await gatherer.detect_pending_work(db)
        assert not pending.cognitive_regeneration

    @pytest.mark.asyncio
    async def test_lessons_extraction_with_observations(self, db, gatherer):
        """Any unresolved observations → lessons extraction possible."""
        now = datetime.now(UTC).isoformat()
        await db.execute(
            "INSERT INTO observations (id, source, type, content, priority, created_at, origin_class) "
            "VALUES ('obs1', 'test', 'test', 'content', 'low', ?, 'first_party')",
            (now,),
        )
        await db.commit()
        pending = await gatherer.detect_pending_work(db)
        assert pending.lessons_extraction


class TestGather:
    @pytest.mark.asyncio
    async def test_gather_empty_db(self, db, gatherer):
        bundle = await gatherer.gather(db)
        assert bundle.recent_observations == []
        assert bundle.intelligence_digest != ""  # always has a message

    @pytest.mark.asyncio
    async def test_gather_with_data(self, db, gatherer):
        now = datetime.now(UTC).isoformat()
        for _i in range(3):
            await db.execute(
                # WS-3: first-party origin so the fail-closed read gate surfaces
                # these legit Genesis observations (NULL/external are excluded).
                "INSERT INTO observations (id, source, type, content, priority, created_at, origin_class) "
                "VALUES (?, 'test', 'test', 'content', 'low', ?, 'first_party')",
                (str(uuid.uuid4()), now),
            )
        await db.commit()
        bundle = await gatherer.gather(db)
        assert len(bundle.recent_observations) == 3

    @pytest.mark.asyncio
    async def test_gather_excludes_resolved_observations(self, db, gatherer):
        """Resolved observations must not appear in recent_observations (telephone game fix)."""
        now = datetime.now(UTC).isoformat()
        # Create 3 unresolved and 2 resolved observations
        for i in range(3):
            await db.execute(
                "INSERT INTO observations (id, source, type, content, priority, created_at, resolved, origin_class) "
                "VALUES (?, 'test', 'test', 'unresolved content', 'low', ?, 0, 'first_party')",
                (f"unresolved-{i}", now),
            )
        for i in range(2):
            await db.execute(
                "INSERT INTO observations (id, source, type, content, priority, created_at, resolved, origin_class) "
                "VALUES (?, 'test', 'test', 'resolved content', 'low', ?, 1, 'first_party')",
                (f"resolved-{i}", now),
            )
        await db.commit()
        bundle = await gatherer.gather(db)
        # Only unresolved observations should be returned
        assert len(bundle.recent_observations) == 3
        for obs in bundle.recent_observations:
            assert obs.get("resolved", 0) == 0


class TestGatherForAssessment:
    @pytest.mark.asyncio
    async def test_returns_six_dimensions(self, db, gatherer):
        data = await gatherer.gather_for_assessment(db)
        assert "reflection_quality" in data
        assert "procedure_effectiveness" in data
        assert "outreach_calibration" in data
        assert "learning_velocity" in data
        assert "resource_efficiency" in data
        assert "blind_spots" in data


class TestReflectionQualityCohort:
    """Dimension 1 uses an age-fair 3-10 day maturity cohort (not a LIMIT-50
    recency window), counts influence only among retrieved observations, and
    abstains when the cohort is too small to score."""

    async def _insert_deep_obs(self, db, *, days_ago, retrieved=0, influenced=0):
        created = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
        await db.execute(
            # WS-3: first_party so the origin-gated reflection_quality cohort
            # counts these (a forged external cc_reflection_deep is now excluded).
            "INSERT INTO observations "
            "(id, source, type, content, priority, created_at, "
            "retrieved_count, influenced_action, origin_class) "
            "VALUES (?, 'cc_reflection_deep', 'reflection', 'c', 'medium', ?, ?, ?, 'first_party')",
            (str(uuid.uuid4()), created, retrieved, influenced),
        )

    @pytest.mark.asyncio
    async def test_cohort_excludes_too_new_and_too_old(self, db, gatherer):
        for _ in range(6):
            await self._insert_deep_obs(db, days_ago=5, retrieved=1)
        await self._insert_deep_obs(db, days_ago=1, retrieved=1)   # too new (<3d)
        await self._insert_deep_obs(db, days_ago=20, retrieved=1)  # too old (>10d)
        await db.commit()

        rq = (await gatherer.gather_for_assessment(db))["reflection_quality"]
        assert rq["cohort_size"] == 6
        assert rq["retrieved_count"] == 6
        assert rq["data_available"] is True
        assert rq["maturity_window_days"] == "3-10"

    @pytest.mark.asyncio
    async def test_abstains_below_min_cohort(self, db, gatherer):
        for _ in range(4):  # below the 5-row floor
            await self._insert_deep_obs(db, days_ago=5, retrieved=1)
        await db.commit()

        rq = (await gatherer.gather_for_assessment(db))["reflection_quality"]
        assert rq["cohort_size"] == 4
        assert rq["data_available"] is False

    @pytest.mark.asyncio
    async def test_influence_counted_only_among_retrieved(self, db, gatherer):
        await self._insert_deep_obs(db, days_ago=5, retrieved=1, influenced=1)  # counts
        await self._insert_deep_obs(db, days_ago=5, retrieved=1, influenced=1)  # counts
        await self._insert_deep_obs(db, days_ago=5, retrieved=0, influenced=1)  # influenced but NOT retrieved → excluded
        await self._insert_deep_obs(db, days_ago=5, retrieved=1, influenced=0)  # retrieved only
        await self._insert_deep_obs(db, days_ago=5, retrieved=0, influenced=0)  # neither
        await db.commit()

        rq = (await gatherer.gather_for_assessment(db))["reflection_quality"]
        assert rq["cohort_size"] == 5
        assert rq["retrieved_count"] == 3
        assert rq["influenced_count"] == 2  # only the 2 retrieved-AND-influenced
        assert rq["data_available"] is True


class TestGatherForCalibration:
    @pytest.mark.asyncio
    async def test_returns_expected_keys(self, db, gatherer):
        data = await gatherer.gather_for_calibration(db)
        assert "procedure_stats" in data
        assert "recent_assessments" in data


class TestProcedureStats:
    @pytest.mark.asyncio
    async def test_empty_procedures(self, db, gatherer):
        bundle = await gatherer.gather(db)
        assert bundle.procedure_stats.total_active == 0

    @pytest.mark.asyncio
    async def test_with_procedures(self, db, gatherer):
        now = datetime.now(UTC).isoformat()
        await db.execute(
            "INSERT INTO procedural_memory "
            "(id, task_type, principle, steps, tools_used, context_tags, "
            "success_count, failure_count, confidence, created_at) "
            "VALUES ('p1', 'test', 'do it', '[]', '[]', '[]', 8, 2, 0.8, ?)",
            (now,),
        )
        await db.commit()
        bundle = await gatherer.gather(db)
        assert bundle.procedure_stats.total_active == 1
        assert bundle.procedure_stats.avg_success_rate == 0.8

    @pytest.mark.asyncio
    async def test_low_performers_detected(self, db, gatherer):
        now = datetime.now(UTC).isoformat()
        await db.execute(
            "INSERT INTO procedural_memory "
            "(id, task_type, principle, steps, tools_used, context_tags, "
            "success_count, failure_count, confidence, created_at) "
            "VALUES ('p1', 'test', 'do it', '[]', '[]', '[]', 1, 4, 0.2, ?)",
            (now,),
        )
        await db.commit()
        bundle = await gatherer.gather(db)
        assert len(bundle.procedure_stats.low_performers) == 1
        assert bundle.procedure_stats.low_performers[0]["success_rate"] == 0.2


class TestCostSummaryRemoved:
    @pytest.mark.asyncio
    async def test_no_cost_summary_field(self, db, gatherer):
        """Cost summary fully removed from ContextBundle."""
        bundle = await gatherer.gather(db)
        assert not hasattr(bundle, "cost_summary")


class TestEngagementByCategoryExcludesOwner:
    @pytest.mark.asyncio
    async def test_owner_channels_excluded_from_category_breakdown(self, db, gatherer):
        """_engagement_by_category must exclude owner-facing channels, matching
        _outreach_stats — otherwise self-assessment sees an external-only rate
        beside an owner-polluted category view (contradictory evidence). An
        external discord 'content' engagement stays; an owner telegram 'approval'
        engagement is dropped."""
        now = datetime.now(UTC).isoformat()
        await outreach_crud.create(
            db, id="ext-c", signal_type="content", topic="post",
            category="content", salience_score=0.8, channel="discord",
            message_content="Shipped X", created_at=now,
        )
        await outreach_crud.record_delivery(db, "ext-c", delivered_at=now)
        await outreach_crud.record_engagement(
            db, "ext-c", engagement_outcome="useful", engagement_signal="user_reply",
        )
        await outreach_crud.create(
            db, id="own-a", signal_type="approval", topic="A",
            category="approval", salience_score=0.8, channel="telegram",
            message_content="Approve?", created_at=now,
        )
        await outreach_crud.record_delivery(db, "own-a", delivered_at=now)
        await outreach_crud.record_engagement(
            db, "own-a", engagement_outcome="acted_on", engagement_signal="user_reply",
        )

        result = await gatherer._engagement_by_category(db)
        assert "content" in result
        assert "approval" not in result


class TestOriginExclusion:
    """WS-3: external/unknown-origin observations must not reach the deep-
    reflection prompt (the laundering path into first-party user_model_updates)."""

    @staticmethod
    async def _mk(db, oid, typ, origin, source="s", when=None):
        when = when or datetime.now(UTC).isoformat()
        await db.execute(
            "INSERT INTO observations (id, source, type, content, priority, "
            "created_at, origin_class) VALUES (?,?,?,?,?,?,?)",
            (oid, source, typ, f"content-{oid}", "low", when, origin),
        )
        await db.commit()

    @pytest.mark.asyncio
    async def test_eval_context_excludes_external_and_null(self, db, gatherer):
        await self._mk(db, "us_owner", "user_signal", "owner")
        await self._mk(db, "us_fp", "user_signal", "first_party")
        await self._mk(db, "us_ext", "user_signal", "external_untrusted")
        await self._mk(db, "us_null", "user_signal", None)
        await self._mk(db, "ai_ext", "architecture_insight", "external_untrusted")
        await self._mk(db, "ai_fp", "architecture_insight", "first_party")
        # inbox digest content is external by construction
        await self._mk(db, "ib_ext", "finding", "external_untrusted", source="inbox_evaluation")

        ctx = await gatherer.gather_evaluation_context(db)

        us_contents = {s["content"] for s in ctx["user_signals"]}
        assert us_contents == {"content-us_owner", "content-us_fp"}, us_contents
        ai_contents = {s["content"] for s in ctx["architecture_insights"]}
        assert ai_contents == {"content-ai_fp"}, ai_contents
        # external inbox finding excluded (until Fix B, intended)
        assert ctx["inbox_findings"] == []

    @pytest.mark.asyncio
    async def test_recent_observations_excludes_external_and_null(self, db, gatherer):
        await self._mk(db, "ro_owner", "insight", "owner")
        await self._mk(db, "ro_ext", "insight", "external_untrusted")
        await self._mk(db, "ro_null", "insight", None)

        rows = await gatherer._recent_observations(db)
        ids = {r["id"] for r in rows}
        assert "ro_owner" in ids
        assert "ro_ext" not in ids
        assert "ro_null" not in ids


class TestIntelligenceIntake:
    """The digest and the intake count read ``knowledge_units`` by its real
    columns (``ingested_at`` / ``source_pipeline``), and a failed read is
    reported as a failure, never as "nothing was ingested" (issue #2337)."""

    async def _insert_unit(
        self, db, *, concept, pipeline, ingested_at, source_doc="intake:test",
    ):
        await db.execute(
            "INSERT INTO knowledge_units "
            "(id, project_type, domain, source_doc, concept, body, "
            "ingested_at, source_pipeline) "
            "VALUES (?, 'genesis', 'intel', ?, ?, 'body', ?, ?)",
            (str(uuid.uuid4()), source_doc, concept, ingested_at, pipeline),
        )

    async def _insert_last_deep(self, db, created_at):
        await db.execute(
            "INSERT INTO observations "
            "(id, source, type, content, priority, created_at, origin_class) "
            "VALUES (?, 'cc_reflection_deep', 'reflection', 'c', 'medium', ?, 'first_party')",
            (str(uuid.uuid4()), created_at),
        )

    @pytest.mark.asyncio
    async def test_digest_counts_recent_units_by_pipeline(self, db, gatherer):
        now = datetime.now(UTC).isoformat()
        old = (datetime.now(UTC) - timedelta(days=5)).isoformat()
        await self._insert_unit(db, concept="a", pipeline="github_landscape", ingested_at=now)
        await self._insert_unit(db, concept="b", pipeline="github_landscape", ingested_at=now)
        await self._insert_unit(db, concept="c", pipeline="model_intelligence", ingested_at=now)
        await self._insert_unit(db, concept="d", pipeline=None, ingested_at=now)
        await self._insert_unit(db, concept="e", pipeline="github_landscape", ingested_at=old)
        await db.commit()

        digest = await gatherer._intelligence_digest(db)
        assert "**4 intelligence items**" in digest
        assert "- github_landscape: 2 items" in digest
        assert "- model_intelligence: 1 items" in digest
        assert "- unknown: 1 items" in digest

        assert await gatherer._intake_items_since_last_deep(db) == 4

    @pytest.mark.asyncio
    async def test_only_intake_pipeline_rows_are_counted(self, db, gatherer):
        """knowledge_units is shared: uploads, saved references and manual
        ingests write it too. Only rows the intake pipeline produced
        (source_doc 'intake:...') are intelligence intake."""
        now = datetime.now(UTC).isoformat()
        await self._insert_unit(db, concept="a", pipeline="model_intelligence", ingested_at=now)
        await self._insert_unit(
            db, concept="b", pipeline="reference_store", ingested_at=now,
            source_doc="reference:x",
        )
        await self._insert_unit(
            db, concept="c", pipeline="curated", ingested_at=now, source_doc="upload:y",
        )
        await db.commit()

        digest = await gatherer._intelligence_digest(db)
        assert "**1 intelligence items**" in digest
        assert "reference_store" not in digest and "curated" not in digest
        assert await gatherer._intake_items_since_last_deep(db) == 1

    @pytest.mark.asyncio
    async def test_unrecognised_pipeline_label_is_not_rendered(self, db, gatherer):
        """source_pipeline is caller-writable (knowledge_ingest provenance), and
        the digest goes into the Deep-reflection prompt: a label outside the
        intake vocabulary is counted as 'other' and its text never rendered."""
        now = datetime.now(UTC).isoformat()
        crafted = "model_intelligence\n## INJECTED-HEADING-MARKER"
        await self._insert_unit(db, concept="a", pipeline=crafted, ingested_at=now)
        await self._insert_unit(db, concept="b", pipeline="model_intelligence", ingested_at=now)
        await db.commit()

        digest = await gatherer._intelligence_digest(db)
        assert "INJECTED-HEADING-MARKER" not in digest
        assert "- other: 1 items" in digest
        assert "- model_intelligence: 1 items" in digest
        assert "**2 intelligence items**" in digest

    @pytest.mark.asyncio
    async def test_fallback_window_boundary_is_iso_consistent(self, db, gatherer):
        """With no prior Deep cycle the window is 2 days. A row ingested 2 days
        and 1 hour ago is outside it, even on the boundary calendar day."""
        stale = (datetime.now(UTC) - timedelta(days=2, hours=1)).isoformat()
        await self._insert_unit(db, concept="a", pipeline="recon", ingested_at=stale)
        await db.commit()
        assert await gatherer._intake_items_since_last_deep(db) == 0

    @pytest.mark.asyncio
    async def test_window_starts_at_last_deep_reflection(self, db, gatherer):
        last_deep = datetime.now(UTC) - timedelta(hours=6)
        await self._insert_last_deep(db, last_deep.isoformat())
        before = (last_deep - timedelta(hours=1)).isoformat()
        after = (last_deep + timedelta(hours=1)).isoformat()
        await self._insert_unit(db, concept="a", pipeline="recon", ingested_at=before)
        await self._insert_unit(db, concept="b", pipeline="recon", ingested_at=after)
        await db.commit()

        digest = await gatherer._intelligence_digest(db)
        assert "**1 intelligence items**" in digest
        assert await gatherer._intake_items_since_last_deep(db) == 1

    @pytest.mark.asyncio
    async def test_genuinely_empty_reports_none_ingested(self, db, gatherer):
        digest = await gatherer._intelligence_digest(db)
        assert digest == "No intelligence items ingested since last Deep cycle."
        assert await gatherer._intake_items_since_last_deep(db) == 0

    @pytest.mark.asyncio
    async def test_failed_read_is_not_reported_as_empty(self, db, gatherer, caplog):
        await db.execute("DROP TABLE knowledge_units")
        await db.commit()

        with caplog.at_level("WARNING", logger="genesis.reflection.context_gatherer"):
            digest = await gatherer._intelligence_digest(db)
            count = await gatherer._intake_items_since_last_deep(db)

        assert "No intelligence items ingested" not in digest
        assert "unavailable" in digest
        assert count is None
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) >= 2
