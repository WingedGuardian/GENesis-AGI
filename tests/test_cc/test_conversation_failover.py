"""Tests for Phase 3 conversation failover orchestration (ConversationLoop).

Covers the STICKY failover flow at the rate-limit call sites: failover to a peer
before contingency, the account-wide state + per-session sticky persistence, the
streaming double-send guard, recovery detection, and the sticky-resume +
stale-resume paths. Roster resolution is monkeypatched so these exercise the
ORCHESTRATION only (peer selection itself is unit-tested in test_roster.py).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.cc import fallback_state, roster
from genesis.cc.conversation import ConversationLoop
from genesis.cc.exceptions import CCError, CCRateLimitError
from genesis.cc.invoker import CCInvoker
from genesis.cc.system_prompt import SystemPromptAssembler
from genesis.cc.types import CCInvocation, CCOutput, ChannelType, StreamEvent
from genesis.db.crud import cc_sessions


def _output(text="reply", session_id="cc-1", **kw):
    d = dict(
        session_id=session_id, text=text, model_used="sonnet",
        roster_model="glm-5.2", cost_usd=0.0, input_tokens=1, output_tokens=1,
        duration_ms=10, exit_code=0, is_error=False,
    )
    d.update(kw)
    return CCOutput(**d)


_PEER_INV = CCInvocation(
    prompt="x", model_id_override="glm-5.2",
    anthropic_base_url="https://glm", anthropic_auth_token="sk",
    roster_eligible=True,
)


@pytest.fixture(autouse=True)
def _hermetic_home(tmp_path, monkeypatch):
    # fallback_state writes under GENESIS_HOME — isolate from the real ~/.genesis.
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path))


@pytest.fixture
def assembler(tmp_path):
    (tmp_path / "SOUL.md").write_text("You are Genesis.")
    (tmp_path / "CONVERSATION.md").write_text("Be concise.")
    return SystemPromptAssembler(identity_dir=tmp_path)


@pytest.fixture
def invoker():
    return AsyncMock(spec=CCInvoker)


@pytest.fixture
async def loop(db, invoker, assembler, monkeypatch):
    lp = ConversationLoop(db=db, invoker=invoker, assembler=assembler, day_boundary_hour=0)
    # Deterministic roster: claude home, single glm peer (no config/key dependency).
    monkeypatch.setattr(roster, "active_model", lambda *a, **k: "claude")
    monkeypatch.setattr(
        roster, "failover_invocations",
        lambda home, base, *a, **k: [("glm-5.2", _PEER_INV)],
    )
    # ALERTs go through the runtime singleton — stub so tests don't need a runtime.
    lp._fire_fallback_alert = AsyncMock()
    return lp


@pytest.mark.asyncio
async def test_failover_returns_peer_reply_and_records_state(loop, invoker, db):
    # Home rate-limited → peer (GLM) succeeds → reply is the peer's, state recorded.
    invoker.run = AsyncMock(side_effect=[
        CCRateLimitError("limit"), _output(text="GLM reply", session_id="glm-1"),
    ])
    result = await loop.handle_message("hi", user_id="u1", channel=ChannelType.TERMINAL)

    assert "GLM reply" in result
    loop._fire_fallback_alert.assert_awaited()  # switch ALERT
    assert fallback_state.read().is_fallback is True
    # Per-session sticky continuation persisted (NOT the account-wide flag).
    row = await cc_sessions.get_active_foreground(db, user_id="u1", channel="terminal")
    sticky = ConversationLoop._session_fallback_session(row)
    assert sticky == {"cc_session_id": "glm-1", "roster_model": "glm-5.2"}


@pytest.mark.asyncio
async def test_failover_reassembles_identity_on_resume(loop, invoker, monkeypatch):
    # A RESUME turn carries system_prompt=None; the fresh peer session must get
    # Genesis identity re-assembled, not run as a vanilla agent.
    captured: dict = {}

    def _spy(home, base, *a, **k):
        captured["system_prompt"] = base.system_prompt
        return [("glm-5.2", _PEER_INV)]

    # Turn 1: establish a resumable session (home=claude success, no routing persist).
    invoker.run = AsyncMock(return_value=_output(
        text="hi", session_id="cc-1", roster_model="claude",
    ))
    await loop.handle_message("hello", user_id="u1", channel=ChannelType.TERMINAL)

    # Turn 2: the resume turn is rate-limited (system_prompt=None) → failover.
    monkeypatch.setattr(roster, "failover_invocations", _spy)
    invoker.run = AsyncMock(side_effect=[
        CCRateLimitError("limit"), _output(text="peer reply", session_id="glm-1"),
    ])
    result = await loop.handle_message("again", user_id="u1", channel=ChannelType.TERMINAL)
    assert "peer reply" in result
    assert captured["system_prompt"] is not None  # identity re-assembled
    assert "You are Genesis." in captured["system_prompt"]


@pytest.mark.asyncio
async def test_failover_skips_sticky_persist_without_session_id(loop, invoker, db):
    # A peer that returns no session_id must NOT persist an unusable sticky entry.
    invoker.run = AsyncMock(side_effect=[
        CCRateLimitError("limit"), _output(text="peer", session_id=""),
    ])
    result = await loop.handle_message("hi", user_id="u1", channel=ChannelType.TERMINAL)
    assert "peer" in result
    row = await cc_sessions.get_active_foreground(db, user_id="u1", channel="terminal")
    assert ConversationLoop._session_fallback_session(row) is None
    assert fallback_state.read().is_fallback is True  # account-wide flag still set


@pytest.mark.asyncio
async def test_all_peers_fail_falls_through_to_contingency(loop, invoker):
    # Home AND peer rate-limited → no successful switch → degraded path, no state.
    invoker.run = AsyncMock(side_effect=CCRateLimitError("limit"))
    result = await loop.handle_message("hi", user_id="u1", channel=ChannelType.TERMINAL)
    assert "Rate limit reached" in result  # contingency=None → degraded message
    assert fallback_state.read().is_fallback is False
    loop._fire_fallback_alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_streaming_guard_skips_failover_after_text_streamed(loop, invoker, monkeypatch):
    # If answer text already streamed, failover must NOT run (would double-output).
    fi = MagicMock(return_value=[("glm-5.2", _PEER_INV)])
    monkeypatch.setattr(roster, "failover_invocations", fi)

    async def _stream(inv, on_event=None):
        await on_event(StreamEvent(event_type="text", text="partial answer"))
        raise CCRateLimitError("limit")

    invoker.run_streaming = AsyncMock(side_effect=_stream)
    result = await loop.handle_message_streaming(
        "hi", user_id="u1", channel=ChannelType.TERMINAL, on_event=AsyncMock(),
    )
    assert "Rate limit reached" in result
    fi.assert_not_called()  # failover skipped because text already streamed


@pytest.mark.asyncio
async def test_streaming_failover_when_nothing_streamed(loop, invoker):
    # No text streamed before the rate-limit → failover proceeds on the peer.
    # First streaming call (home) raises with no text; second (peer) succeeds.
    calls = {"n": 0}

    async def _stream(inv, on_event=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise CCRateLimitError("limit")  # home, nothing streamed
        if on_event:
            await on_event(StreamEvent(event_type="text", text="GLM streamed"))
        return _output(text="GLM streamed", session_id="glm-2")

    invoker.run_streaming = AsyncMock(side_effect=_stream)
    result = await loop.handle_message_streaming(
        "hi", user_id="u1", channel=ChannelType.TERMINAL, on_event=AsyncMock(),
    )
    assert "GLM streamed" in result
    assert fallback_state.read().is_fallback is True


@pytest.mark.asyncio
async def test_recovery_clears_state_and_alerts(loop, invoker):
    # Pre-existing fallback → a successful HOME turn clears it + fires recovery ALERT.
    fallback_state.enter("claude", "glm-5.2", "rate_limit")
    assert fallback_state.read().is_fallback is True
    invoker.run = AsyncMock(return_value=_output(
        text="home reply", roster_model="claude", session_id="cc-home",
    ))
    result = await loop.handle_message("hi", user_id="u1", channel=ChannelType.TERMINAL)
    assert "home reply" in result
    assert fallback_state.read().is_fallback is False
    loop._fire_fallback_alert.assert_awaited()  # recovery ALERT


@pytest.mark.asyncio
async def test_run_failover_peer_resumes_sticky_session(loop, invoker):
    invoker.run = AsyncMock(return_value=_output(session_id="glm-1"))
    out = await loop._run_failover_peer(
        "glm-5.2", _PEER_INV,
        sticky={"roster_model": "glm-5.2", "cc_session_id": "glm-prev"},
        on_event=None,
    )
    assert out.session_id == "glm-1"
    used = invoker.run.call_args[0][0]
    assert used.resume_session_id == "glm-prev"  # resumed for continuity


@pytest.mark.asyncio
async def test_run_failover_peer_stale_resume_retries_fresh(loop, invoker):
    invoker.run = AsyncMock(side_effect=[CCError("stale"), _output(session_id="glm-new")])
    out = await loop._run_failover_peer(
        "glm-5.2", _PEER_INV,
        sticky={"roster_model": "glm-5.2", "cc_session_id": "glm-prev"},
        on_event=None,
    )
    assert out.session_id == "glm-new"
    assert invoker.run.call_count == 2
    fresh = invoker.run.call_args_list[1][0][0]
    assert fresh.resume_session_id is None  # retried fresh after stale resume


@pytest.mark.asyncio
async def test_run_failover_peer_rate_limit_propagates(loop, invoker):
    # A rate-limited peer must raise (so the caller tries the next peer), not retry.
    invoker.run = AsyncMock(side_effect=CCRateLimitError("limit"))
    with pytest.raises(CCRateLimitError):
        await loop._run_failover_peer("glm-5.2", _PEER_INV, sticky=None, on_event=None)
    assert invoker.run.call_count == 1  # no fresh retry on rate-limit
