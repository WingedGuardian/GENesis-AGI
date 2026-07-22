"""Tests for the background-session delivery model.

Covers DeliveryMode derivation, origin-target resolution, the framework
delivery helper (_deliver_result_to_origin), and the _run_session wiring that
routes a RESULT-mode outcome (success AND failure) back to its origin thread.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.cc.direct_session import (
    DirectSessionRequest,
    DirectSessionResult,
    DirectSessionRunner,
)
from genesis.cc.types import CCOutput, DeliveryMode
from genesis.db.crud import cc_sessions

# ---------------------------------------------------------------------------
# DeliveryMode derivation — legacy callers must be behaviorally unchanged
# ---------------------------------------------------------------------------


class TestDeliveryModeDerivation:
    def test_from_legacy_maps_bools(self):
        assert DeliveryMode.from_legacy(False, False) is DeliveryMode.SILENT
        assert DeliveryMode.from_legacy(True, False) is DeliveryMode.FAILURE_ONLY
        # notify_on_failure_only is a no-op today; both notify=True combos collapse.
        assert DeliveryMode.from_legacy(True, True) is DeliveryMode.FAILURE_ONLY

    def test_request_derives_mode_from_notify(self):
        assert DirectSessionRequest(prompt="x", notify=True).delivery_mode is (
            DeliveryMode.FAILURE_ONLY
        )
        assert DirectSessionRequest(prompt="x", notify=False).delivery_mode is (DeliveryMode.SILENT)

    def test_explicit_result_mode_preserved(self):
        r = DirectSessionRequest(
            prompt="x",
            delivery_mode=DeliveryMode.RESULT,
            origin_session_id="sess-abc",
        )
        assert r.delivery_mode is DeliveryMode.RESULT
        assert r.origin_session_id == "sess-abc"


# ---------------------------------------------------------------------------
# Origin-target resolution (pure static method)
# ---------------------------------------------------------------------------


class TestResolveOriginTarget:
    f = staticmethod(DirectSessionRunner._resolve_origin_target)

    # --- Preferred path: persisted chat_id (all new sessions) ---
    def test_dm_uses_persisted_chat_id(self):
        assert self.f("telegram", "12345678", "tg-12345678", None, "-100999") == (
            "12345678",
            None,
        )

    def test_group_uses_persisted_chat_id(self):
        # A group chat (chat.id != user.id, no thread) delivers to the GROUP, not
        # the user's DM — the exact misroute the persisted chat_id closes.
        assert self.f("telegram", "-100555", "tg-12345678", None, "-100999") == (
            "-100555",
            None,
        )

    def test_forum_uses_persisted_chat_id_and_thread(self):
        assert self.f("telegram", "-100999", "tg-12345678", "110", "-100999") == (
            "-100999",
            110,
        )

    def test_non_telegram_unaddressable(self):
        assert self.f("terminal", "999", "tg-1", None, "-100999") == (None, None)
        assert self.f(None, "999", "tg-1", None, "-100999") == (None, None)

    def test_bad_thread_id_unaddressable(self):
        assert self.f("telegram", "999", "tg-1", "not-an-int", "-100999") == (None, None)

    # --- Legacy fallback: chat_id absent (rows predating capture) ---
    def test_legacy_dm_reconstructs_from_user_id(self):
        assert self.f("telegram", None, "tg-12345678", None, "-100999") == ("12345678", None)

    def test_legacy_forum_uses_forum_chat_id(self):
        assert self.f("telegram", None, "tg-1", "110", "-100999") == ("-100999", 110)

    def test_legacy_forum_without_chat_unaddressable(self):
        assert self.f("telegram", None, "tg-1", "110", None) == (None, None)

    def test_legacy_non_numeric_user_unaddressable(self):
        assert self.f("telegram", None, "tg-notanumber", None, "-100999") == (None, None)


# ---------------------------------------------------------------------------
# _deliver_result_to_origin — framework delivery helper
# ---------------------------------------------------------------------------


def _runner_with_pipeline(db):
    """A runner whose runtime exposes a capturing outreach pipeline + the db."""
    pipeline = MagicMock()
    pipeline.submit_urgent = AsyncMock(return_value=None)
    pipeline._forum_chat_id = "-1002000"
    rt = MagicMock()
    rt._outreach_pipeline = pipeline
    rt._db = db
    runner = DirectSessionRunner(
        invoker=AsyncMock(),
        session_manager=AsyncMock(),
        config_builder=AsyncMock(),
        runtime=rt,
    )
    return runner, pipeline


async def _make_origin(db, *, session_id, channel, user_id, thread_id=None, chat_id=None):
    await cc_sessions.create(
        db,
        id=session_id,
        session_type="foreground",
        model="opus",
        started_at="2026-07-22T00:00:00+00:00",
        last_activity_at="2026-07-22T00:00:00+00:00",
        user_id=user_id,
        channel=channel,
        thread_id=thread_id,
        chat_id=chat_id,
    )


class TestDeliverResultToOrigin:
    async def test_dm_delivery(self, db):
        await _make_origin(db, session_id="o-dm", channel="telegram", user_id="tg-12345678")
        runner, pipeline = _runner_with_pipeline(db)
        req = DirectSessionRequest(
            prompt="x", delivery_mode=DeliveryMode.RESULT, origin_session_id="o-dm"
        )
        res = DirectSessionResult(session_id="s1", success=True, output_text="the findings")
        await runner._deliver_result_to_origin(req, res)

        pipeline.submit_urgent.assert_awaited_once()
        sent = pipeline.submit_urgent.call_args.args[0]
        assert sent.target_chat_id == "12345678"
        assert sent.target_thread_id is None
        assert sent.channel == "telegram"
        assert sent.verbatim is True
        assert "the findings" in sent.context
        assert sent.topic == "bg_result:s1"

    async def test_forum_delivery(self, db):
        await _make_origin(
            db, session_id="o-fx", channel="telegram", user_id="tg-1", thread_id="110"
        )
        runner, pipeline = _runner_with_pipeline(db)
        req = DirectSessionRequest(
            prompt="x", delivery_mode=DeliveryMode.RESULT, origin_session_id="o-fx"
        )
        res = DirectSessionResult(session_id="s2", success=True, output_text="report")
        await runner._deliver_result_to_origin(req, res)

        sent = pipeline.submit_urgent.call_args.args[0]
        assert sent.target_chat_id == "-1002000"  # the forum supergroup
        assert sent.target_thread_id == 110

    async def test_group_chat_delivery_uses_persisted_chat_id(self, db):
        # A group-chat origin (chat.id != user.id, no thread) must deliver to the
        # GROUP, not the requesting user's DM. Regression guard for the Codex P2.
        await _make_origin(
            db,
            session_id="o-grp",
            channel="telegram",
            user_id="tg-12345678",
            chat_id="-100555",
        )
        runner, pipeline = _runner_with_pipeline(db)
        req = DirectSessionRequest(
            prompt="x", delivery_mode=DeliveryMode.RESULT, origin_session_id="o-grp"
        )
        res = DirectSessionResult(session_id="sg", success=True, output_text="group report")
        await runner._deliver_result_to_origin(req, res)

        sent = pipeline.submit_urgent.call_args.args[0]
        assert sent.target_chat_id == "-100555"  # the group, NOT tg-12345678's DM
        assert sent.target_thread_id is None

    async def test_failure_delivered_to_origin(self, db):
        await _make_origin(db, session_id="o-f", channel="telegram", user_id="tg-1")
        runner, pipeline = _runner_with_pipeline(db)
        req = DirectSessionRequest(
            prompt="x", delivery_mode=DeliveryMode.RESULT, origin_session_id="o-f"
        )
        res = DirectSessionResult(session_id="s3", success=False, error="boom")
        await runner._deliver_result_to_origin(req, res)

        sent = pipeline.submit_urgent.call_args.args[0]
        assert "boom" in sent.context
        assert "✗" in sent.context

    async def test_oversized_output_writes_file_and_pointers(self, db, tmp_path, monkeypatch):
        # Redirect the artifact dir to a temp path.
        import genesis.cc.direct_session as ds

        monkeypatch.setattr(ds.Path, "home", lambda: tmp_path)
        await _make_origin(db, session_id="o-big", channel="telegram", user_id="tg-1")
        runner, pipeline = _runner_with_pipeline(db)
        big = "A" * 9000
        req = DirectSessionRequest(
            prompt="x", delivery_mode=DeliveryMode.RESULT, origin_session_id="o-big"
        )
        res = DirectSessionResult(session_id="s4", success=True, output_text=big)
        await runner._deliver_result_to_origin(req, res)

        sent = pipeline.submit_urgent.call_args.args[0]
        assert len(sent.context) < 9000  # truncated for delivery
        assert "truncated" in sent.context
        artifact = tmp_path / ".genesis" / "output" / "bg-session-s4.md"
        assert artifact.exists()
        assert artifact.read_text() == big  # full raw content saved

    async def test_unaddressable_origin_falls_back_not_dropped(self, db):
        # A non-telegram origin can't be targeted → deliver to the default surface.
        await _make_origin(db, session_id="o-term", channel="terminal", user_id="tg-1")
        runner, pipeline = _runner_with_pipeline(db)
        req = DirectSessionRequest(
            prompt="x", delivery_mode=DeliveryMode.RESULT, origin_session_id="o-term"
        )
        res = DirectSessionResult(session_id="s5", success=True, output_text="done")
        await runner._deliver_result_to_origin(req, res)

        pipeline.submit_urgent.assert_awaited_once()
        sent = pipeline.submit_urgent.call_args.args[0]
        assert sent.target_chat_id is None
        assert sent.channel is None  # default channel selection
        assert "could not be addressed" in sent.context

    async def test_no_origin_id_skips_without_error(self, db):
        runner, pipeline = _runner_with_pipeline(db)
        req = DirectSessionRequest(prompt="x", delivery_mode=DeliveryMode.RESULT)
        res = DirectSessionResult(session_id="s6", success=True, output_text="x")
        await runner._deliver_result_to_origin(req, res)
        pipeline.submit_urgent.assert_not_awaited()


# ---------------------------------------------------------------------------
# _run_session wiring — RESULT routes both terminal states to origin delivery
# ---------------------------------------------------------------------------


def _cc_output(*, is_error=False, text="hi"):
    return CCOutput(
        session_id="cc-1",
        text=text,
        model_used="sonnet",
        cost_usd=0.0,
        input_tokens=1,
        output_tokens=1,
        duration_ms=10,
        exit_code=0,
        is_error=is_error,
    )


class TestRunSessionDeliveryWiring:
    async def test_success_result_mode_delivers(self):
        runner = DirectSessionRunner(
            invoker=AsyncMock(),
            session_manager=AsyncMock(),
            config_builder=AsyncMock(),
            runtime=MagicMock(),
        )
        runner._build_invocation = MagicMock(return_value=MagicMock(claude_code_tmpdir=None))
        runner._invoker.run_streaming = AsyncMock(return_value=_cc_output())
        runner._store_result = AsyncMock()
        runner._record_proposal_outcome = AsyncMock()
        runner._session_manager.complete = AsyncMock()
        runner._deliver_result_to_origin = AsyncMock()

        req = DirectSessionRequest(
            prompt="x", delivery_mode=DeliveryMode.RESULT, origin_session_id="o1"
        )
        await runner._run_session(req, "sess-1")
        runner._deliver_result_to_origin.assert_awaited_once()

    async def test_failure_result_mode_delivers_not_broadcast(self):
        runner = DirectSessionRunner(
            invoker=AsyncMock(),
            session_manager=AsyncMock(),
            config_builder=AsyncMock(),
            runtime=MagicMock(),
        )
        runner._build_invocation = MagicMock(return_value=MagicMock(claude_code_tmpdir=None))
        runner._invoker.run_streaming = AsyncMock(side_effect=RuntimeError("kaboom"))
        runner._store_result = AsyncMock()
        runner._record_proposal_outcome = AsyncMock()
        runner._session_manager.fail = AsyncMock()
        runner._deliver_result_to_origin = AsyncMock()
        runner._notify = AsyncMock()

        req = DirectSessionRequest(
            prompt="x", delivery_mode=DeliveryMode.RESULT, origin_session_id="o1"
        )
        with pytest.raises(RuntimeError):
            await runner._run_session(req, "sess-2")
        runner._deliver_result_to_origin.assert_awaited_once()
        runner._notify.assert_not_awaited()  # RESULT replaces the broadcast alert

    async def test_failure_legacy_mode_broadcasts_not_origin(self):
        runner = DirectSessionRunner(
            invoker=AsyncMock(),
            session_manager=AsyncMock(),
            config_builder=AsyncMock(),
            runtime=MagicMock(),
        )
        runner._build_invocation = MagicMock(return_value=MagicMock(claude_code_tmpdir=None))
        runner._invoker.run_streaming = AsyncMock(side_effect=RuntimeError("kaboom"))
        runner._store_result = AsyncMock()
        runner._record_proposal_outcome = AsyncMock()
        runner._session_manager.fail = AsyncMock()
        runner._deliver_result_to_origin = AsyncMock()
        runner._notify = AsyncMock()

        # Legacy caller (notify=True → FAILURE_ONLY): broadcast, not origin.
        req = DirectSessionRequest(prompt="x", notify=True)
        with pytest.raises(RuntimeError):
            await runner._run_session(req, "sess-3")
        runner._notify.assert_awaited_once()
        runner._deliver_result_to_origin.assert_not_awaited()
