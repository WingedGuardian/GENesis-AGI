"""Tests for feedback/harvest.py — folding existing signals into the bus."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from genesis.db.crud import ego as ego_crud
from genesis.db.crud import outcome_events as oe
from genesis.feedback.harvest import (
    BACKFILL_MARKER,
    BACKFILL_VERSION,
    OutcomeHarvester,
    _marker_version,
    _parse_execution_suffix,
)


@pytest.fixture
async def db(tmp_path):
    """Full schema (ego_proposals, outreach_history, ego_state, outcome_events)."""
    from genesis.db.schema import create_all_tables

    path = str(tmp_path / "harvest.db")
    async with aiosqlite.connect(path) as conn:
        await create_all_tables(conn)
        # Live prod carries engagement_outcome values ('acted_on', 'acknowledged',
        # '') that violate the current CHECK — the harvester must handle data that
        # already exists. Disable CHECK enforcement so the fixture can reproduce
        # that real-world condition.
        await conn.execute("PRAGMA ignore_check_constraints = ON")
        await conn.commit()
        yield conn


def _recent(*, hours: float) -> str:
    """An ISO timestamp `hours` in the past. Relative (not hardcoded) so the
    windowed run() always sees default rows — fixed-date defaults silently
    "expire" relative to the rolling window and break this suite as time passes.
    """
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat()


async def _add_proposal(
    db, *, pid, status, user_response=None, confidence=0.8,
    action_type="investigate", cycle_id=None,
    created_at=None, resolved_at=None,
):
    created_at = created_at or _recent(hours=2)
    resolved_at = resolved_at or _recent(hours=1)
    await db.execute(
        "INSERT INTO ego_proposals "
        "(id, action_type, content, status, confidence, user_response, "
        " cycle_id, created_at, resolved_at) "
        "VALUES (?, ?, 'content', ?, ?, ?, ?, ?, ?)",
        (pid, action_type, status, confidence, user_response, cycle_id,
         created_at, resolved_at),
    )
    await db.commit()


async def _add_outreach(
    db, *, oid, engagement_outcome, engagement_signal=None, user_response=None,
    category="notification", prediction_error=None, delivered_at=None,
):
    delivered_at = delivered_at or _recent(hours=1)
    await db.execute(
        "INSERT INTO outreach_history "
        "(id, signal_type, topic, category, salience_score, channel, "
        " message_content, engagement_outcome, engagement_signal, user_response, "
        " prediction_error, delivered_at, created_at) "
        "VALUES (?, 'sig', 'topic', ?, 0.5, 'telegram', 'msg', ?, ?, ?, ?, ?, ?)",
        (oid, category, engagement_outcome, engagement_signal, user_response,
         prediction_error, delivered_at, _recent(hours=2)),
    )
    await db.commit()


async def _add_surplus(
    db, *, tid, status, task_type="code_audit", failure_reason=None,
    completed_at=None, created_at=None, outcome_quality=None,
):
    created_at = created_at or _recent(hours=2)
    completed_at = completed_at if completed_at is not None else _recent(hours=1)
    await db.execute(
        "INSERT INTO surplus_tasks "
        "(id, task_type, compute_tier, priority, drive_alignment, status, "
        " failure_reason, created_at, completed_at, outcome_quality) "
        "VALUES (?, ?, 'local', 0.5, 'curiosity', ?, ?, ?, ?, ?)",
        (tid, task_type, status, failure_reason, created_at, completed_at,
         outcome_quality),
    )
    await db.commit()


# --------------------------------------------------------------------------- #
# Suffix parser
# --------------------------------------------------------------------------- #
class TestSuffixParser:
    def test_session_prefixed_completed(self):
        assert _parse_execution_suffix("session:abc|completed:did it") == (
            "positive", 1.0, "did it",
        )

    def test_bare_completed(self):
        assert _parse_execution_suffix("|completed:done") == ("positive", 1.0, "done")

    def test_failed(self):
        polarity, value, summary = _parse_execution_suffix("sid|failed:broke")
        assert polarity == "negative" and value == 0.0 and summary == "broke"

    def test_no_suffix(self):
        assert _parse_execution_suffix("just a user reason") is None
        assert _parse_execution_suffix(None) is None
        assert _parse_execution_suffix("") is None


# --------------------------------------------------------------------------- #
# Proposal harvest
# --------------------------------------------------------------------------- #
class TestHarvestProposals:
    @pytest.mark.asyncio
    async def test_executed_with_suffix_is_t1_ground_truth(self, db):
        await _add_proposal(
            db, pid="p1", status="executed",
            user_response="session:s1|completed:shipped the fix",
            confidence=0.9, cycle_id="cyc1",
        )
        await OutcomeHarvester(db).run_backfill()
        rows = await oe.recent(db, limit=10)
        assert len(rows) == 1
        row = rows[0]
        assert row["signal_type"] == "execution_outcome"
        assert row["signal_tier"] == 1
        assert row["polarity"] == "positive"
        assert row["value"] == 1.0
        assert row["domain"] == "investigate"
        assert row["stated_confidence"] == 0.9
        assert row["reason_text"] == "shipped the fix"
        assert row["metadata"] == '{"cycle_id": "cyc1"}'
        assert row["harvested_from"] == "ego_proposals"

    @pytest.mark.asyncio
    async def test_failed_is_t1_negative(self, db):
        await _add_proposal(db, pid="p2", status="failed")
        await OutcomeHarvester(db).run_backfill()
        row = (await oe.recent(db, limit=1))[0]
        assert row["signal_type"] == "execution_outcome"
        assert row["signal_tier"] == 1
        assert row["polarity"] == "negative"
        assert row["value"] == 0.0

    @pytest.mark.asyncio
    async def test_executed_without_suffix_is_coverage(self, db):
        await _add_proposal(db, pid="p3", status="executed", user_response="session:s3")
        await OutcomeHarvester(db).run_backfill()
        row = (await oe.recent(db, limit=1))[0]
        assert row["signal_type"] == "dispatch"
        assert row["signal_tier"] == 3

    @pytest.mark.asyncio
    async def test_rejected_with_reason_is_t2(self, db):
        await _add_proposal(
            db, pid="p4", status="rejected", user_response="not now, bad timing",
        )
        await OutcomeHarvester(db).run_backfill()
        row = (await oe.recent(db, limit=1))[0]
        assert row["signal_type"] == "user_decision"
        assert row["signal_tier"] == 2  # rationale present → informative
        assert row["polarity"] == "negative"
        assert row["reason_text"] == "not now, bad timing"

    @pytest.mark.asyncio
    async def test_tabled_and_withdrawn_are_lifecycle_t3(self, db):
        await _add_proposal(db, pid="p5", status="tabled")
        await _add_proposal(db, pid="p6", status="withdrawn")
        await OutcomeHarvester(db).run_backfill()
        by_type = await oe.count_by_signal_type(db)
        assert by_type == {"lifecycle_tabled": 1, "lifecycle_withdrawn": 1}

    @pytest.mark.asyncio
    async def test_pending_is_skipped(self, db):
        await _add_proposal(db, pid="p7", status="pending")
        await OutcomeHarvester(db).run_backfill()
        assert await oe.count(db) == 0

    @pytest.mark.asyncio
    async def test_expired_is_lifecycle_t3(self, db):
        # A timeout is NOT disapproval — coverage only.
        await _add_proposal(db, pid="pe", status="expired")
        await OutcomeHarvester(db).run_backfill()
        row = (await oe.recent(db, limit=1))[0]
        assert row["signal_type"] == "lifecycle_expired"
        assert row["signal_tier"] == 3
        assert row["polarity"] == "neutral"


# --------------------------------------------------------------------------- #
# Outreach harvest
# --------------------------------------------------------------------------- #
class TestHarvestOutreach:
    @pytest.mark.asyncio
    async def test_useful_reply_is_t2_positive(self, db):
        await _add_outreach(
            db, oid="o1", engagement_outcome="useful", user_response="thanks!",
            prediction_error=0.2,
        )
        await OutcomeHarvester(db).run_backfill()
        row = (await oe.recent(db, limit=1))[0]
        assert row["signal_type"] == "outreach_reply"
        assert row["signal_tier"] == 2
        assert row["polarity"] == "positive"
        assert row["reason_text"] == "thanks!"
        assert row["prediction_error"] == 0.2
        assert row["source"] == "outreach"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "outcome, exp_type, exp_polarity",
        [
            ("useful", "outreach_reply", "positive"),
            ("engaged", "outreach_reply", "positive"),        # dashboard /engage writer
            ("acted_on", "outreach_reply", "positive"),       # was mislabeled negative
            ("acknowledged", "outreach_reply", "positive"),   # was mislabeled negative
            ("not_useful", "outreach_reply", "negative"),
            ("ambivalent", "outreach_implicit", "neutral"),
            ("ignored", "outreach_implicit", "negative"),
        ],
    )
    async def test_engagement_polarity_mapping(self, db, outcome, exp_type, exp_polarity):
        await _add_outreach(db, oid=f"o-{outcome}", engagement_outcome=outcome)
        await OutcomeHarvester(db).run_backfill()
        row = (await oe.recent(db, limit=1))[0]
        assert row["signal_type"] == exp_type
        assert row["polarity"] == exp_polarity

    @pytest.mark.asyncio
    async def test_empty_string_outcome_is_skipped(self, db):
        # Live data has 15 empty-string rows — they are NOT a real signal.
        await _add_outreach(db, oid="empty", engagement_outcome="")
        await _add_outreach(db, oid="real", engagement_outcome="useful")
        await OutcomeHarvester(db).run_backfill()
        assert await oe.count(db) == 1  # only the real one

    @pytest.mark.asyncio
    async def test_noreply_timeout_is_skipped(self, db):
        # A 24h no-reply (outcome='ignored', signal='timeout') carries no value
        # signal — silence is not a negative (WS-0). It must NOT harvest a row.
        await _add_outreach(
            db, oid="nr", engagement_outcome="ignored", engagement_signal="timeout",
        )
        await _add_outreach(db, oid="real", engagement_outcome="useful")
        await OutcomeHarvester(db).run_backfill()
        assert await oe.count(db) == 1  # only the useful one

    @pytest.mark.asyncio
    async def test_explicit_ignored_nontimeout_stays_negative(self, db):
        # An explicit dismissal (non-'timeout' signal) is still a real negative.
        await _add_outreach(
            db, oid="dis", engagement_outcome="ignored", engagement_signal="user_dismiss",
        )
        await OutcomeHarvester(db).run_backfill()
        row = (await oe.recent(db, limit=1))[0]
        assert row["signal_type"] == "outreach_implicit"
        assert row["polarity"] == "negative"


# --------------------------------------------------------------------------- #
# Surplus-task harvest (LC3-A — the tier-1 execution-outcome producer)
# --------------------------------------------------------------------------- #
class TestHarvestSurplusTasks:
    @pytest.mark.asyncio
    async def test_completed_is_t1_positive(self, db):
        await _add_surplus(db, tid="s1", status="completed", task_type="code_audit")
        await OutcomeHarvester(db).run_backfill()
        row = (await oe.recent(db, limit=1))[0]
        assert row["signal_type"] == "execution_outcome"
        assert row["signal_tier"] == 1
        assert row["polarity"] == "positive"
        assert row["value"] == 1.0
        assert row["domain"] == "code_audit"
        assert row["source"] == "surplus"
        assert row["harvested_from"] == "surplus_tasks"
        assert row["reason_text"] is None
        # surplus tasks carry no pre-execution confidence → excluded from calibration
        assert row["stated_confidence"] is None

    @pytest.mark.asyncio
    async def test_failed_is_t1_negative_with_reason(self, db):
        # completed_at=None exercises the COALESCE(completed_at, created_at) path.
        await _add_surplus(
            db, tid="s2", status="failed", task_type="infrastructure_monitor",
            failure_reason="probe timed out", completed_at=None,
        )
        await OutcomeHarvester(db).run_backfill()
        row = (await oe.recent(db, limit=1))[0]
        assert row["signal_type"] == "execution_outcome"
        assert row["signal_tier"] == 1
        assert row["polarity"] == "negative"
        assert row["value"] == 0.0
        assert row["reason_text"] == "probe timed out"
        assert row["domain"] == "infrastructure_monitor"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["pending", "running", "cancelled"])
    async def test_non_terminal_is_skipped(self, db, status):
        # A cancel/in-flight task is not an execution outcome — no row.
        await _add_surplus(db, tid=f"s-{status}", status=status)
        await OutcomeHarvester(db).run_backfill()
        assert await oe.count(db) == 0

    @pytest.mark.asyncio
    async def test_idempotent_on_unique_key(self, db):
        await _add_surplus(db, tid="s3", status="completed")
        await OutcomeHarvester(db).run()
        await OutcomeHarvester(db).run()  # re-scan same window
        assert await oe.count(db) == 1  # unique key dedupes

    @pytest.mark.asyncio
    async def test_incremental_window_excludes_old_but_backfill_includes(self, db):
        await _add_surplus(
            db, tid="s-old", status="completed",
            created_at="2026-05-01T00:00:00+00:00",
            completed_at="2026-05-01T00:00:00+00:00",
        )
        run_result = await OutcomeHarvester(db).run(window_days=2)
        assert run_result["surplus"] == 0
        assert await oe.count(db) == 0
        await OutcomeHarvester(db).run_backfill()
        assert await oe.count(db) == 1


# --------------------------------------------------------------------------- #
# Surplus verified-correctness (#4 — the second, orthogonal axis)
# --------------------------------------------------------------------------- #
class TestSurplusVerifiedCorrectness:
    @pytest.mark.asyncio
    async def test_hollow_emits_positive_AND_verification_failed(self, db):
        # A completed insight task whose output was hollow gets BOTH the usual
        # EXECUTION_OUTCOME positive ("it ran") and an additional, discriminative
        # VERIFICATION_FAILED negative ("output was useless").
        await _add_surplus(
            db, tid="h1", status="completed", task_type="brainstorm_self",
            outcome_quality="hollow",
        )
        await OutcomeHarvester(db).run_backfill()

        assert await oe.count_by_signal_type(db) == {
            "execution_outcome": 1, "verification_failed": 1,
        }
        rows = await oe.recent(db, limit=10)
        vf = next(r for r in rows if r["signal_type"] == "verification_failed")
        assert vf["signal_tier"] == 1
        assert vf["polarity"] == "negative"
        assert vf["value"] == 0.0
        assert vf["domain"] == "brainstorm_self"
        assert vf["source"] == "surplus"
        assert vf["harvested_from"] == "surplus_tasks"
        assert vf["stated_confidence"] is None  # excluded from calibration
        eo = next(r for r in rows if r["signal_type"] == "execution_outcome")
        assert eo["polarity"] == "positive" and eo["value"] == 1.0

    @pytest.mark.asyncio
    async def test_useful_is_positive_only(self, db):
        await _add_surplus(
            db, tid="u1", status="completed", task_type="brainstorm_self",
            outcome_quality="useful",
        )
        await OutcomeHarvester(db).run_backfill()
        assert await oe.count_by_signal_type(db) == {"execution_outcome": 1}

    @pytest.mark.asyncio
    async def test_legacy_null_is_positive_only(self, db):
        # Legacy / action / intake-failure / empty rows (NULL outcome_quality)
        # keep the original positive-only behaviour — no retroactive negatives.
        await _add_surplus(
            db, tid="l1", status="completed", task_type="code_audit",
            outcome_quality=None,
        )
        await OutcomeHarvester(db).run_backfill()
        assert await oe.count_by_signal_type(db) == {"execution_outcome": 1}

    @pytest.mark.asyncio
    async def test_failed_hollow_does_not_double_emit(self, db):
        # Defensive: outcome_quality is only ever set on completed rows, but a
        # (failed, hollow) row must NOT add a verification_failed — a failure is
        # already the negative; verification applies to COMPLETED work only.
        await _add_surplus(
            db, tid="f1", status="failed", task_type="code_audit",
            outcome_quality="hollow", failure_reason="boom",
        )
        await OutcomeHarvester(db).run_backfill()
        assert await oe.count_by_signal_type(db) == {"execution_outcome": 1}

    @pytest.mark.asyncio
    async def test_hollow_idempotent_across_reruns(self, db):
        # The two signals coexist under the (source, ref_type, ref_id, signal_type)
        # unique key and neither dup-suppresses the other on incremental re-scan.
        await _add_surplus(
            db, tid="h2", status="completed", task_type="code_audit",
            outcome_quality="hollow",
        )
        await OutcomeHarvester(db).run()
        await OutcomeHarvester(db).run()  # re-scan same window
        assert await oe.count(db) == 2
        assert await oe.count_by_signal_type(db) == {
            "execution_outcome": 1, "verification_failed": 1,
        }


# --------------------------------------------------------------------------- #
# Versioned backfill marker (LC3-A — re-backfill when a new source is added)
# --------------------------------------------------------------------------- #
class TestVersionedBackfillMarker:
    def test_marker_version_parsing(self):
        assert _marker_version(None) == 0
        assert _marker_version("") == 0
        assert _marker_version("2026-06-25T12:45:02+00:00") == 1  # legacy bare ISO
        assert _marker_version("v2:2026-06-29T00:00:00+00:00") == 2
        assert _marker_version("v3:2026-07-01T00:00:00+00:00") == 3
        assert _marker_version("vX:garbage") == 1  # unparseable → legacy fallback
        assert _marker_version("v0:x") == 1         # sub-v1 clamps to legacy (no loop)
        assert _marker_version("v-1:x") == 1

    @pytest.mark.asyncio
    async def test_legacy_marker_triggers_rerun_and_restamps(self, db):
        # Pre-LC3-A install: marker present as a bare ISO (v1), so the new
        # surplus source was never backfilled. The version bump must re-run it.
        await ego_crud.set_state(
            db, key=BACKFILL_MARKER, value="2026-06-25T12:45:02+00:00",
        )
        await _add_surplus(
            db, tid="hist", status="completed",
            created_at="2026-05-01T00:00:00+00:00",
            completed_at="2026-05-01T00:00:00+00:00",
        )
        result = await OutcomeHarvester(db).run_backfill()
        assert result["skipped"] is False        # v1 < BACKFILL_VERSION → re-run
        assert result["surplus"] == 1             # historical surplus now harvested
        stored = await ego_crud.get_state(db, BACKFILL_MARKER)
        assert _marker_version(stored) == BACKFILL_VERSION  # re-stamped, not bare ISO
        assert stored.startswith(f"v{BACKFILL_VERSION}:")

    @pytest.mark.asyncio
    async def test_current_version_marker_skips(self, db):
        await ego_crud.set_state(
            db, key=BACKFILL_MARKER,
            value=f"v{BACKFILL_VERSION}:2026-06-29T00:00:00+00:00",
        )
        await _add_surplus(db, tid="s", status="completed")
        result = await OutcomeHarvester(db).run_backfill()
        assert result == {"skipped": True}
        assert await oe.count(db) == 0  # already current → no work, no rows

    @pytest.mark.asyncio
    async def test_marker_not_set_when_surplus_source_fails(self, db):
        # Parallel to the outreach reliability guard: a surplus failure must NOT
        # lock the gate (else its 2k+ historical rows are lost forever). surplus
        # is in the failures-counting loop, NOT wrapped in _safe.
        await _add_surplus(db, tid="s", status="completed")
        await db.execute("DROP TABLE surplus_tasks")
        await db.commit()
        result = await OutcomeHarvester(db).run_backfill()
        assert result.get("incomplete") is True
        assert await ego_crud.get_state(db, BACKFILL_MARKER) is None  # NOT locked


# --------------------------------------------------------------------------- #
# Idempotency / window / marker
# --------------------------------------------------------------------------- #
class TestIdempotencyAndScheduling:
    @pytest.mark.asyncio
    async def test_backfill_is_idempotent_and_guarded(self, db):
        await _add_proposal(
            db, pid="p1", status="executed",
            user_response="s|completed:x",
        )
        first = await OutcomeHarvester(db).run_backfill()
        assert first["skipped"] is False
        assert first["proposals"] == 1
        # Marker set
        assert await ego_crud.get_state(db, BACKFILL_MARKER) is not None
        # Second run is guarded → skipped, no new rows
        second = await OutcomeHarvester(db).run_backfill()
        assert second["skipped"] is True
        assert await oe.count(db) == 1

    @pytest.mark.asyncio
    async def test_backfill_marker_not_set_when_a_source_fails(self, db):
        # Reliability guard: a source exception must NOT lock the backfill gate,
        # else the historical rows are lost forever. (Reproduce by dropping a
        # source table — the startup-race / missing-table scenario.)
        await _add_proposal(
            db, pid="p1", status="executed", user_response="s|completed:x",
        )
        await db.execute("DROP TABLE outreach_history")
        await db.commit()
        result = await OutcomeHarvester(db).run_backfill()
        assert result.get("incomplete") is True
        assert result["proposals"] == 1  # per-source isolation: proposals still ran
        assert await ego_crud.get_state(db, BACKFILL_MARKER) is None  # NOT locked

    @pytest.mark.asyncio
    async def test_incremental_run_is_idempotent_on_unique_key(self, db):
        # _add_proposal defaults to a recent timestamp (see _recent), so the row
        # always falls inside run()'s default 2-day incremental window.
        await _add_proposal(
            db, pid="p1", status="executed", user_response="s|completed:x",
        )
        await OutcomeHarvester(db).run()
        await OutcomeHarvester(db).run()  # re-scan same window
        assert await oe.count(db) == 1  # unique key dedupes

    @pytest.mark.asyncio
    async def test_incremental_window_excludes_old_but_backfill_includes(self, db):
        await _add_proposal(
            db, pid="old", status="executed", user_response="s|completed:x",
            created_at="2026-05-01T00:00:00+00:00",
            resolved_at="2026-05-01T00:00:00+00:00",
        )
        # Recent window misses the old row...
        run_result = await OutcomeHarvester(db).run(window_days=2)
        assert run_result["proposals"] == 0
        assert await oe.count(db) == 0
        # ...but backfill (all history) captures it.
        await OutcomeHarvester(db).run_backfill()
        assert await oe.count(db) == 1
