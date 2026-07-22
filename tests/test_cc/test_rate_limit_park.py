"""Tests for rate_limit_park — park orchestration the CC call sites delegate to.

Covers dedup-key stability, caller_context lineage parsing, mode-gated foreground
parking + copy, fresh background parking, the re-limit-in-place lineage (the
architect-#1 fix), resumed-close, and backoff math. Uses the real in-memory db.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.cc import rate_limit_park as rlp
from genesis.cc.direct_session import DirectSessionRequest, DirectSessionRunner
from genesis.cc.exceptions import CCQuotaExhaustedError, CCRateLimitError
from genesis.cc.rate_limit_resume_config import load_config
from genesis.cc.types import DeliveryMode
from genesis.db.crud import cc_rate_limit_parks as parks

NOW = datetime(2026, 7, 22, 14, 0, 0, tzinfo=UTC)


def test_dedup_key_stable_and_scoped():
    a = rlp.dedup_key("conversation", "s1", "hello")
    assert a == rlp.dedup_key("conversation", "s1", "hello")
    assert a != rlp.dedup_key("conversation", "s2", "hello")  # origin-scoped
    assert a != rlp.dedup_key("direct_session", "s1", "hello")  # kind-scoped
    assert a != rlp.dedup_key("conversation", "s1", "world")  # prompt-scoped


def test_parse_park_id():
    assert rlp.parse_park_id("rate_limit_resume:rlp-abc") == "rlp-abc"
    assert rlp.parse_park_id("ego_proposal:x") is None
    assert rlp.parse_park_id(None) is None
    assert rlp.parse_park_id("rate_limit_resume:") is None


def test_backoff_is_exponential_and_capped():
    cfg = load_config()
    a0 = rlp.backoff_next_attempt(0, NOW, cfg)
    a1 = rlp.backoff_next_attempt(1, NOW, cfg)
    a_big = rlp.backoff_next_attempt(20, NOW, cfg)
    assert a0 == (NOW + timedelta(minutes=30)).isoformat()
    assert a1 == (NOW + timedelta(minutes=60)).isoformat()
    assert a_big == (NOW + timedelta(minutes=240)).isoformat()  # capped at 4h


async def test_park_conversation_off_mode_does_not_park(db):
    exc = CCRateLimitError("limit", raw_text="resets 5pm")
    outcome = await rlp.park_conversation(
        db,
        prompt="hi",
        origin_session_id="s1",
        exc=exc,
        now=NOW,
        mode="off",
    )
    assert outcome.parked is False
    assert "try again later" in outcome.copy
    assert await parks.count_open(db) == 0


async def test_park_conversation_live_parks_with_reset(db):
    exc = CCRateLimitError("limit", raw_text="Session limit — resets 5pm")
    outcome = await rlp.park_conversation(
        db,
        prompt="deep research please",
        origin_session_id="s1",
        exc=exc,
        now=NOW,
        mode="live",
    )
    assert outcome.parked is True
    assert outcome.park_id is not None
    assert "auto-resume" in outcome.copy
    row = await parks.get_by_id(db, outcome.park_id)
    assert row["kind"] == "conversation"
    assert row["origin_session_id"] == "s1"
    assert row["limit_kind"] == "session"
    assert row["reset_at"] == NOW.replace(hour=17, minute=0).isoformat()
    # payload carries a bounded resume profile + the prompt
    import json

    payload = json.loads(row["payload_json"])
    assert payload["prompt"] == "deep research please"
    assert payload["profile"] == "research"


async def test_park_conversation_quota_error_also_parks(db):
    exc = CCQuotaExhaustedError("usage limit", raw_text="resets in 1h")
    outcome = await rlp.park_conversation(
        db,
        prompt="x",
        origin_session_id="s2",
        exc=exc,
        now=NOW,
        mode="live",
    )
    assert outcome.parked is True


def _bg_request(caller_context=None, prompt="bg work"):
    return SimpleNamespace(
        prompt=prompt,
        profile="research",
        model="sonnet",
        effort="high",
        timeout_s=3600,
        roster_model=None,
        origin_session_id="orig1",
        caller_context=caller_context,
    )


async def test_park_direct_session_fresh(db):
    exc = CCRateLimitError("limit", raw_text="resets in 30m")
    pid = await rlp.park_direct_session(db, request=_bg_request(), exc=exc, now=NOW, mode="live")
    assert pid is not None
    row = await parks.get_by_id(db, pid)
    assert row["kind"] == "direct_session"
    assert row["attempts"] == 0


async def test_park_direct_session_off_returns_none(db):
    exc = CCRateLimitError("limit", raw_text="resets in 30m")
    pid = await rlp.park_direct_session(db, request=_bg_request(), exc=exc, now=NOW, mode="off")
    assert pid is None
    assert await parks.count_open(db) == 0


async def test_relimit_updates_same_park_not_a_new_one(db):
    """A resumed session that re-limits must update its OWN park (attempts+1),
    not mint a fresh one — the architect-#1 lineage fix."""
    # Seed a park + claim it (as the resume engine would).
    exc = CCRateLimitError("limit", raw_text="resets in 30m")
    pid = await rlp.park_direct_session(db, request=_bg_request(), exc=exc, now=NOW, mode="live")
    await parks.claim(db, pid)
    assert await parks.count_open(db) == 1
    # The resumed retry re-limits: caller_context carries the park id.
    resumed_req = _bg_request(caller_context=f"rate_limit_resume:{pid}")
    exc2 = CCRateLimitError("limit", raw_text="resets in 1h")
    returned = await rlp.park_direct_session(
        db,
        request=resumed_req,
        exc=exc2,
        now=NOW,
        mode="live",
    )
    assert returned == pid  # same park, not a new one
    assert await parks.count_open(db) == 1  # NOT 2
    row = await parks.get_by_id(db, pid)
    assert row["attempts"] == 1
    assert row["status"] == "parked"  # re-opened for another attempt


async def test_relimit_escalates_to_needs_user(db):
    exc = CCRateLimitError("limit", raw_text="resets in 30m")
    pid = await rlp.park_direct_session(db, request=_bg_request(), exc=exc, now=NOW, mode="live")
    # Force attempts near threshold by direct writes, then re-limit once.
    await db.execute(
        "UPDATE cc_rate_limit_parks SET attempts = 39, status = 'resuming' WHERE id = ?",
        (pid,),
    )
    await db.commit()
    resumed = _bg_request(caller_context=f"rate_limit_resume:{pid}")
    await rlp.park_direct_session(db, request=resumed, exc=exc, now=NOW, mode="live")
    row = await parks.get_by_id(db, pid)
    assert row["attempts"] == 40
    assert row["status"] == "needs_user"


async def test_relimit_missing_lineage_reparks_fresh_not_dropped(db):
    """A resumed request whose park row is gone/terminal must re-park fresh, not
    return None → generic failure (reviewer NOTE #2)."""
    resumed = _bg_request(caller_context="rate_limit_resume:rlp-doesnotexist")
    exc = CCRateLimitError("limit", raw_text="resets in 30m")
    pid = await rlp.park_direct_session(db, request=resumed, exc=exc, now=NOW, mode="live")
    assert pid is not None
    assert pid != "rlp-doesnotexist"
    assert await parks.count_open(db) == 1


async def test_mark_resumed_if_lineage(db):
    exc = CCRateLimitError("limit", raw_text="resets in 30m")
    pid = await rlp.park_direct_session(db, request=_bg_request(), exc=exc, now=NOW, mode="live")
    await parks.claim(db, pid)
    await rlp.mark_resumed_if_lineage(db, f"rate_limit_resume:{pid}")
    assert (await parks.get_by_id(db, pid))["status"] == "resumed"
    # No-op for a non-resume caller_context.
    await rlp.mark_resumed_if_lineage(db, "ego_proposal:x")  # must not raise


def _park_runner(db):
    """A DirectSessionRunner whose runtime exposes the real in-memory db."""
    rt = MagicMock()
    rt._db = db
    runner = DirectSessionRunner(
        invoker=AsyncMock(),
        session_manager=AsyncMock(),
        config_builder=AsyncMock(),
        runtime=rt,
    )
    runner._build_invocation = MagicMock(return_value=MagicMock(claude_code_tmpdir=None))
    runner._store_result = AsyncMock()
    runner._record_proposal_outcome = AsyncMock()
    runner._session_manager.fail = AsyncMock()
    runner._deliver_result_to_origin = AsyncMock()
    runner._notify = AsyncMock()
    return runner


class TestRunSessionParkWiring:
    async def test_rate_limit_parks_instead_of_failing(self, db, monkeypatch):
        # Default mode is off (dormant until PR-2b) — force live to exercise the
        # park path.
        monkeypatch.setattr("genesis.cc.rate_limit_resume_config.effective_mode", lambda: "live")
        runner = _park_runner(db)
        runner._invoker.run_streaming = AsyncMock(
            side_effect=CCRateLimitError("limit", raw_text="resets in 30m")
        )
        req = DirectSessionRequest(
            prompt="bg", delivery_mode=DeliveryMode.RESULT, origin_session_id="o1"
        )
        # Parks gracefully — does NOT raise and does NOT deliver a failure.
        result = await runner._run_session(req, "sess-park")
        assert result.success is False
        assert "parked for resume" in (result.error or "")
        runner._deliver_result_to_origin.assert_not_awaited()
        runner._session_manager.fail.assert_awaited_with("sess-park", reason="rate_limited_parked")
        assert await parks.count_open(db) == 1

    async def test_disabled_falls_through_to_failure(self, db, monkeypatch):
        monkeypatch.setenv("GENESIS_RATE_LIMIT_RESUME_DISABLED", "1")
        runner = _park_runner(db)
        runner._invoker.run_streaming = AsyncMock(
            side_effect=CCRateLimitError("limit", raw_text="resets in 30m")
        )
        req = DirectSessionRequest(
            prompt="bg", delivery_mode=DeliveryMode.RESULT, origin_session_id="o1"
        )
        # mode=off → current behavior: re-raises AND delivers the failure to origin.
        with pytest.raises(CCRateLimitError):
            await runner._run_session(req, "sess-fail")
        runner._deliver_result_to_origin.assert_awaited_once()
        assert await parks.count_open(db) == 0
