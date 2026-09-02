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

from genesis.cc import fallback_state, peer_availability, roster
from genesis.cc.conversation import ConversationLoop
from genesis.cc.exceptions import CCError, CCNetworkOfflineError, CCRateLimitError
from genesis.cc.invoker import CCInvoker
from genesis.cc.system_prompt import SystemPromptAssembler
from genesis.cc.types import (
    CCInvocation,
    CCModel,
    CCOutput,
    ChannelType,
    EffortLevel,
    StreamEvent,
)
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
async def test_recovery_clears_state_and_alerts(loop, invoker, monkeypatch):
    # Pre-existing fallback → a successful HOME turn clears it + fires recovery ALERT.
    # Recovery now routes through the shared helper (foreground/background/probe all
    # share genesis.cc.fallback_recovery), so assert on that module function.
    fallback_state.enter("claude", "glm-5.2", "rate_limit")
    assert fallback_state.read().is_fallback is True
    fired = AsyncMock()
    monkeypatch.setattr("genesis.cc.fallback_recovery.fire_fallback_alert", fired)
    invoker.run = AsyncMock(return_value=_output(
        text="home reply", roster_model="claude", session_id="cc-home",
    ))
    result = await loop.handle_message("hi", user_id="u1", channel=ChannelType.TERMINAL)
    assert "home reply" in result
    assert fallback_state.read().is_fallback is False
    fired.assert_awaited()  # recovery ALERT via shared helper


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


# ── CAVEAT A: network-offline on a RESUME turn must NOT fail the live session ──

@pytest.mark.asyncio
async def test_network_offline_on_resume_fast_reraises(loop, invoker):
    # A CCNetworkOfflineError on a resume turn is the internet being down, NOT a
    # stale resume. It must fast-re-raise WITHOUT _recover_stale_resume (which
    # would mark the live CC session failed and retry fresh).
    invoker.run = AsyncMock(side_effect=CCNetworkOfflineError("offline"))
    loop._recover_stale_resume = AsyncMock()
    inv = CCInvocation(prompt="x", resume_session_id="cc-live")
    with pytest.raises(CCNetworkOfflineError):
        await loop._try_invoke(
            inv, session={"id": "cc-live"}, was_resume=True, prompt_text="x",
            model=CCModel.SONNET, effort=EffortLevel.MEDIUM,
            user_id="u1", channel=ChannelType.TERMINAL, thread_id=None,
        )
    loop._recover_stale_resume.assert_not_awaited()
    assert invoker.run.await_count == 1  # no fresh retry


@pytest.mark.asyncio
async def test_network_offline_on_resume_streaming_fast_reraises(loop, invoker):
    invoker.run_streaming = AsyncMock(side_effect=CCNetworkOfflineError("offline"))
    loop._recover_stale_resume = AsyncMock()
    inv = CCInvocation(prompt="x", resume_session_id="cc-live")
    with pytest.raises(CCNetworkOfflineError):
        await loop._try_invoke_streaming(
            inv, session={"id": "cc-live"}, was_resume=True, prompt_text="x",
            model=CCModel.SONNET, effort=EffortLevel.MEDIUM,
            user_id="u1", channel=ChannelType.TERMINAL, thread_id=None, on_event=None,
        )
    loop._recover_stale_resume.assert_not_awaited()
    assert invoker.run_streaming.await_count == 1


@pytest.mark.asyncio
async def test_run_failover_peer_offline_propagates_without_fresh_retry(loop, invoker):
    # Same class on the peer path: an offline error on a sticky peer resume must
    # propagate, NOT trigger the "retry fresh" recovery (which is for stale resumes).
    invoker.run = AsyncMock(side_effect=CCNetworkOfflineError("offline"))
    with pytest.raises(CCNetworkOfflineError):
        await loop._run_failover_peer(
            "glm-5.2", _PEER_INV,
            sticky={"roster_model": "glm-5.2", "cc_session_id": "glm-prev"},
            on_event=None,
        )
    assert invoker.run.call_count == 1  # no fresh retry


# ── peer-availability recording (advisory observation on the failover path) ──
# These live here, not in test_peer_availability.py, because this is where the
# behaviour can actually break: roster.py never references peer_availability, so
# a test asserting "selection is unchanged" is tautological by construction.


@pytest.mark.asyncio
async def test_blocked_peer_is_still_attempted(loop, invoker, db):
    """THE SAFETY PROPERTY: the record is advisory and must never suppress a peer.

    A stale or wrong "unavailable" record that removed a peer from the attempt
    would drop a WORKING backup at exactly the moment the home model is down,
    turning a recoverable outage into a degraded one. Pre-mark the only peer as
    blocked, then require that it is still invoked AND still serves the turn.
    """
    peer_availability.note_failure("glm-5.2", CCRateLimitError("earlier 429"))
    assert peer_availability.read_peer("glm-5.2").available is False

    invoker.run = AsyncMock(side_effect=[
        CCRateLimitError("limit"), _output(text="GLM reply", session_id="glm-1"),
    ])
    result = await loop.handle_message("hi", user_id="u1", channel=ChannelType.TERMINAL)

    assert "GLM reply" in result, "a peer recorded as blocked was skipped — gating regression"
    assert invoker.run.await_count == 2  # home, then the 'blocked' peer
    # ...and serving the turn clears the stale block.
    assert peer_availability.read_peer("glm-5.2").available is True


@pytest.mark.asyncio
async def test_peer_quota_refusal_is_recorded(loop, invoker):
    # Home AND peer rate-limited → the peer's refusal is real evidence about it.
    invoker.run = AsyncMock(side_effect=CCRateLimitError("limit"))
    await loop.handle_message("hi", user_id="u1", channel=ChannelType.TERMINAL)
    st = peer_availability.read_peer("glm-5.2")
    assert st is not None and st.available is False
    assert st.reason == peer_availability.QUOTA


@pytest.mark.asyncio
async def test_local_fault_does_not_mark_the_peer_down(loop, invoker):
    """A dead local network never reaches the provider — blaming the peer would
    mark the standby fleet down for a blip that had nothing to do with it."""
    invoker.run = AsyncMock(side_effect=[
        CCRateLimitError("limit"), CCNetworkOfflineError("no route"),
    ])
    await loop.handle_message("hi", user_id="u1", channel=ChannelType.TERMINAL)
    assert peer_availability.read_peer("glm-5.2") is None


@pytest.mark.asyncio
async def test_successful_peer_is_recorded_available(loop, invoker):
    invoker.run = AsyncMock(side_effect=[
        CCRateLimitError("limit"), _output(text="GLM reply", session_id="glm-1"),
    ])
    await loop.handle_message("hi", user_id="u1", channel=ChannelType.TERMINAL)
    st = peer_availability.read_peer("glm-5.2")
    assert st is not None and st.available is True


# Captured at import, BEFORE the `loop` fixture monkeypatches it, so one test
# below can exercise the REAL selection path instead of a stub.
_REAL_FAILOVER_INVOCATIONS = roster.failover_invocations


@pytest.mark.asyncio
async def test_blocked_peer_survives_the_real_selection_path(loop, invoker, monkeypatch):
    """The advisory property, without stubbing the place a gate would go.

    The shared fixture monkeypatches ``roster.failover_invocations`` — the most
    natural home for a suppression gate — so a test resting on it cannot observe
    a gate added there. This one restores the real function and drives it from a
    fake roster, so selection genuinely runs while the peer is recorded blocked.
    """
    fake_roster = {
        "default": "claude",
        "models": {
            "claude": {"native_subscription": True, "failover_order": 0},
            "glm-5.2": {
                "anthropic_base_url": "https://glm.invalid/anthropic",
                "auth_env": "FAKE_PEER_KEY",
                "model_id": "glm-5.2",
                "failover_order": 1,
            },
        },
    }
    monkeypatch.setenv("FAKE_PEER_KEY", "token-value-long-enough")
    monkeypatch.setattr(roster, "failover_invocations", _REAL_FAILOVER_INVOCATIONS)
    monkeypatch.setattr(roster, "load_roster", lambda *a, **k: fake_roster)

    peer_availability.note_failure("glm-5.2", CCRateLimitError("earlier 429"))
    assert peer_availability.read_peer("glm-5.2").available is False

    invoker.run = AsyncMock(side_effect=[
        CCRateLimitError("limit"), _output(text="GLM reply", session_id="glm-1"),
    ])
    result = await loop.handle_message("hi", user_id="u1", channel=ChannelType.TERMINAL)

    assert "GLM reply" in result, "blocked peer was suppressed by real selection — gating regression"
    assert peer_availability.read_peer("glm-5.2").available is True


@pytest.mark.asyncio
async def test_degenerate_empty_success_does_not_clear_a_block(loop, invoker):
    """A silent cap returns a non-error output with NO text. Treating that as
    'available' would erase a real prior block — deleting the one signal an
    operator would act on."""
    peer_availability.note_failure("glm-5.2", CCRateLimitError("earlier 429"))
    invoker.run = AsyncMock(side_effect=[
        CCRateLimitError("limit"), _output(text="", session_id="glm-1"),
    ])
    await loop.handle_message("hi", user_id="u1", channel=ChannelType.TERMINAL)
    assert peer_availability.read_peer("glm-5.2").available is False
