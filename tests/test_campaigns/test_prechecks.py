"""Tests for campaign pre-check registry."""

from __future__ import annotations

from datetime import UTC
from unittest.mock import MagicMock

import pytest

from genesis.campaigns.prechecks import (
    check_budget,
    check_rate_limit,
    check_slots_available,
    run_prechecks,
)


class TestRateLimit:
    @pytest.mark.anyio
    async def test_passes_when_no_last_run(self):
        campaign = {"last_run_at": None, "cron_cadence": "0 */8 * * *"}
        ok, reason = await check_rate_limit(campaign, ctx={})
        assert ok is True

    @pytest.mark.anyio
    async def test_passes_when_enough_time_elapsed(self):
        campaign = {
            "last_run_at": "2026-06-06T00:00:00Z",
            "cron_cadence": "0 */8 * * *",
        }
        ok, reason = await check_rate_limit(campaign, ctx={})
        assert ok is True  # Last run was over a day ago

    @pytest.mark.anyio
    async def test_fails_when_too_recent(self):
        from datetime import datetime

        now = datetime.now(UTC).isoformat()
        campaign = {"last_run_at": now, "cron_cadence": "0 */1 * * *"}
        ok, reason = await check_rate_limit(campaign, ctx={})
        assert ok is False
        assert "rate_limit" in (reason or "").lower() or "too recent" in (reason or "").lower()


class TestBudget:
    @pytest.mark.anyio
    async def test_passes_under_budget(self):
        campaign = {"id": "c1", "max_daily_cost_usd": 1.0}
        ctx = {"daily_cost": 0.50}
        ok, reason = await check_budget(campaign, ctx=ctx)
        assert ok is True

    @pytest.mark.anyio
    async def test_fails_over_budget(self):
        campaign = {"id": "c1", "max_daily_cost_usd": 1.0}
        ctx = {"daily_cost": 1.50}
        ok, reason = await check_budget(campaign, ctx=ctx)
        assert ok is False
        assert "budget" in (reason or "").lower()


class TestSlotsAvailable:
    @pytest.mark.anyio
    async def test_passes_when_slots_free(self):
        runner = MagicMock()
        runner.active_count.return_value = 0
        runner._MAX_CONCURRENT = 2
        campaign = {}
        ok, reason = await check_slots_available(campaign, ctx={"session_runner": runner})
        assert ok is True

    @pytest.mark.anyio
    async def test_fails_when_slots_full(self):
        runner = MagicMock()
        runner.active_count.return_value = 2
        runner._MAX_CONCURRENT = 2
        campaign = {}
        ok, reason = await check_slots_available(campaign, ctx={"session_runner": runner})
        assert ok is False
        assert "slots" in (reason or "").lower()


class TestRunPrechecks:
    @pytest.mark.anyio
    async def test_runs_all_checks_and_passes(self):
        campaign = {
            "id": "c1",
            "last_run_at": None,
            "cron_cadence": "0 */8 * * *",
            "max_daily_cost_usd": 1.0,
            "pre_checks": '["rate_limit", "budget"]',
        }
        ctx = {"daily_cost": 0.0}
        ok, reason = await run_prechecks(campaign, ctx)
        assert ok is True
        assert reason is None

    @pytest.mark.anyio
    async def test_stops_on_first_failure(self):
        campaign = {
            "id": "c1",
            "last_run_at": None,
            "cron_cadence": "0 */8 * * *",
            "max_daily_cost_usd": 0.01,
            "pre_checks": '["rate_limit", "budget"]',
        }
        ctx = {"daily_cost": 5.0}
        ok, reason = await run_prechecks(campaign, ctx)
        assert ok is False
        assert "budget" in (reason or "").lower()

    @pytest.mark.anyio
    async def test_unknown_check_skipped(self):
        campaign = {
            "id": "c1",
            "last_run_at": None,
            "cron_cadence": "0 */8 * * *",
            "max_daily_cost_usd": 1.0,
            "pre_checks": '["rate_limit", "nonexistent_check"]',
        }
        ctx = {"daily_cost": 0.0}
        ok, reason = await run_prechecks(campaign, ctx)
        assert ok is True  # Unknown checks are skipped, not failures


class TestGithubActivityPending:
    """C2b digest gate: dispatch the LLM digest only when the account-activity
    monitor recorded NEW unresolved ``github_account_activity`` observations
    since this campaign's last run — so a quiet 6h window costs nothing.

    Uses the real observations store (mocking ``observations`` crud is guarded
    against by conftest). Plain ``async def`` because the ``db`` fixture is an
    asyncio-mode fixture (matches tests/test_db/test_observations.py).
    """

    # last_run_at is written as datetime.now(UTC).isoformat() → +00:00 suffix;
    # the monitor's obs created_at is _now_z() → Z suffix. The count does a
    # lexical created_at > since compare; these tests pin that the format
    # mismatch does not break the window (both share the YYYY-MM-DDTHH:MM:SS
    # prefix, so lexical order == chronological order across the boundary).
    _SINCE = "2026-08-06T00:00:00.000000+00:00"

    @staticmethod
    async def _mk(
        db,
        *,
        oid,
        created_at,
        resolved=False,
        type="github_account_activity",
        source="recon",
    ):
        from genesis.db.crud import observations

        await observations.create(
            db,
            id=oid,
            source=source,
            type=type,
            content="{}",
            priority="medium",
            created_at=created_at,
        )
        if resolved:
            await observations.resolve(
                db, oid, resolved_at="2026-08-06T13:00:00Z", resolution_notes="digested"
            )

    async def test_gates_when_no_activity(self, db):
        from genesis.campaigns.prechecks import check_github_activity_pending

        ok, reason = await check_github_activity_pending(
            {"last_run_at": self._SINCE}, ctx={"db": db}
        )
        assert ok is False
        assert "github" in (reason or "").lower()

    async def test_passes_when_new_activity_since(self, db):
        from genesis.campaigns.prechecks import check_github_activity_pending

        await self._mk(db, oid="a1", created_at="2026-08-06T12:00:00Z")
        ok, reason = await check_github_activity_pending(
            {"last_run_at": self._SINCE}, ctx={"db": db}
        )
        assert ok is True
        assert reason is None

    async def test_gates_when_activity_predates_since(self, db):
        from genesis.campaigns.prechecks import check_github_activity_pending

        # created BEFORE last_run — already covered by a prior tick
        await self._mk(db, oid="old", created_at="2026-08-05T12:00:00Z")
        ok, reason = await check_github_activity_pending(
            {"last_run_at": self._SINCE}, ctx={"db": db}
        )
        assert ok is False

    async def test_boundary_same_second_counts_as_new(self, db):
        # The format mismatch only ever diverges WITHIN one wall-clock second.
        # Here last_run_at falls mid-second (…12:00:00.500000+00:00) and the obs
        # created_at is the same second (…12:00:00Z). Lexically `Z`(0x5A) > `.`
        # (0x2E), so the same-second obs is counted as new. This is the harmless
        # false-POSITIVE direction (at worst one extra dispatch that the digest's
        # resolve-on-consume makes idempotent) — pin it so the documented bias is
        # real and can't silently flip.
        from genesis.campaigns.prechecks import check_github_activity_pending

        await self._mk(db, oid="tie", created_at="2026-08-06T12:00:00Z")
        ok, reason = await check_github_activity_pending(
            {"last_run_at": "2026-08-06T12:00:00.500000+00:00"}, ctx={"db": db}
        )
        assert ok is True

    async def test_boundary_just_after_worstcase_since_not_missed(self, db):
        # The guarantee that matters: NO false-negatives. Worst case for a
        # missed signal is a `since` with maximal sub-second (…00.999999+00:00)
        # and an obs one second later (…01Z). Even here the lexical compare keeps
        # it (`…12:00:01Z` > `…12:00:00.999999+00:00`), so a genuinely-new obs is
        # never dropped by the Z-vs-+00:00 format difference.
        from genesis.campaigns.prechecks import check_github_activity_pending

        await self._mk(db, oid="after", created_at="2026-08-06T12:00:01Z")
        ok, reason = await check_github_activity_pending(
            {"last_run_at": "2026-08-06T12:00:00.999999+00:00"}, ctx={"db": db}
        )
        assert ok is True
        assert reason is None

    async def test_ignores_resolved_activity(self, db):
        from genesis.campaigns.prechecks import check_github_activity_pending

        # a NEW but already-digested (resolved) row must NOT re-trigger a tick
        await self._mk(db, oid="done", created_at="2026-08-06T12:00:00Z", resolved=True)
        ok, reason = await check_github_activity_pending(
            {"last_run_at": self._SINCE}, ctx={"db": db}
        )
        assert ok is False

    async def test_ignores_other_types_and_sources(self, db):
        from genesis.campaigns.prechecks import check_github_activity_pending

        # unresolved + recent, but the actor-seen marker and foreign sources are
        # NOT the digest signal — only github_account_activity@recon counts.
        await self._mk(db, oid="seen", created_at="2026-08-06T12:00:00Z", type="github_actor_seen")
        await self._mk(db, oid="other", created_at="2026-08-06T12:00:00Z", source="cc_cap_monitor")
        ok, reason = await check_github_activity_pending(
            {"last_run_at": self._SINCE}, ctx={"db": db}
        )
        assert ok is False

    async def test_fails_open_when_db_missing(self, db):
        from genesis.campaigns.prechecks import check_github_activity_pending

        # older runner without db in ctx → never gate on missing plumbing
        ok, reason = await check_github_activity_pending({"last_run_at": self._SINCE}, ctx={})
        assert ok is True
        assert reason is None

    async def test_fails_open_when_no_last_run(self, db):
        from genesis.campaigns.prechecks import check_github_activity_pending

        # first run (no last_run_at) → let it through; the digest no-ops if empty
        ok, reason = await check_github_activity_pending({"last_run_at": None}, ctx={"db": db})
        assert ok is True
        assert reason is None

    async def test_registered_in_registry(self):
        from genesis.campaigns.prechecks import (
            PRECHECK_REGISTRY,
            check_github_activity_pending,
        )

        assert PRECHECK_REGISTRY.get("github_activity_pending") is check_github_activity_pending

    async def test_run_prechecks_gates_via_registry(self, db):
        # end-to-end through run_prechecks: registry lookup resolves the name and
        # the db-in-ctx plumbing reaches the check.
        campaign = {
            "id": "c1",
            "last_run_at": self._SINCE,
            "max_daily_cost_usd": 1.0,
            "pre_checks": '["github_activity_pending"]',
        }
        ok, reason = await run_prechecks(campaign, {"daily_cost": 0.0, "db": db})
        assert ok is False
        assert "github" in (reason or "").lower()
