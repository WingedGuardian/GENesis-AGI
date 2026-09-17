"""Tests for Phase 3 conversation failover orchestration (ConversationLoop).

Covers the STICKY failover flow at the rate-limit call sites: failover to a peer
before contingency, the account-wide state + per-session sticky persistence, the
streaming double-send guard, recovery detection, and the sticky-resume +
stale-resume paths. Roster resolution is monkeypatched so these exercise the
ORCHESTRATION only (peer selection itself is unit-tested in test_roster.py).
"""
from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.cc import fallback_state, peer_availability, roster
from genesis.cc.conversation import (
    ConversationLoop,
    _session_control_block,
    _strip_session_control_block,
)
from genesis.cc.exceptions import (
    CCError,
    CCMCPError,
    CCNetworkOfflineError,
    CCProcessError,
    CCRateLimitError,
    CCStreamTruncatedError,
)
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


@pytest.mark.asyncio
async def test_empty_answer_after_streaming_does_not_double_output(loop, invoker, monkeypatch):
    """The limit on the fix above. If a peer ALREADY streamed text, continuing
    would hit the loop-top guard, break to contingency, and answer a second time
    on top of text the user can already see — the exact double-output the guard
    at the top of the loop exists to prevent. So a streamed-then-empty peer keeps
    the old behaviour and does NOT advance."""
    monkeypatch.setattr(
        roster, "failover_invocations",
        lambda home, base, *a, **k: [("peer-a", _PEER_INV), ("peer-b", _PEER_INV)],
    )
    # streamed starts EMPTY — the loop-top guard would otherwise break before
    # any peer is attempted, which tests nothing. peer-a streams text DURING its
    # attempt and then returns an empty output: the case the guard is for.
    streamed: dict = {}
    base_inv = CCInvocation(prompt="x", roster_eligible=True)
    session = {"id": "s1"}
    loop._merge_session_metadata = AsyncMock()
    loop._session_mgr = MagicMock(update_activity=AsyncMock())

    async def _stream_then_empty(*a, **k):
        streamed["text"] = "partial answer already shown to the user"
        return _output(text="", session_id="a-1")

    invoker.run = AsyncMock(side_effect=_stream_then_empty)

    result = await loop._try_roster_failover(
        session=session, base_inv=base_inv, channel=ChannelType.TERMINAL,
        model=CCModel.SONNET, effort=EffortLevel.LOW, prompt_text="x",
        streamed=streamed,
    )
    # Only peer-a was attempted — we did NOT advance to peer-b...
    assert invoker.run.await_count == 1
    # ...and the RETURN VALUE is what actually prevents the second answer: the
    # call sites test `is not None`, so "" suppresses contingency while None
    # would let it answer again over the streamed text. Counting calls alone
    # certified a property this test never exercised.
    assert result == ""


@pytest.mark.asyncio
async def test_streamed_then_raised_does_not_let_contingency_answer_again(loop, invoker, monkeypatch):
    """The double-output reasoning applied only to the EMPTY-output branch; the
    exception branches still `continue`. A peer that streams text and then
    raises hit the loop-top break, returned None, and contingency answered a
    second time on top of the partial text the user could already see."""
    monkeypatch.setattr(
        roster, "failover_invocations",
        lambda home, base, *a, **k: [("peer-a", _PEER_INV), ("peer-b", _PEER_INV)],
    )
    streamed: dict = {}

    async def _stream_then_raise(*a, **k):
        streamed["text"] = "partial answer already shown to the user"
        raise CCRateLimitError("limit")

    invoker.run = AsyncMock(side_effect=_stream_then_raise)
    loop._merge_session_metadata = AsyncMock()
    loop._session_mgr = MagicMock(update_activity=AsyncMock())

    result = await loop._try_roster_failover(
        session={"id": "s1"}, base_inv=CCInvocation(prompt="x", roster_eligible=True),
        channel=ChannelType.TERMINAL, model=CCModel.SONNET,
        effort=EffortLevel.LOW, prompt_text="x", streamed=streamed,
    )
    assert result == "", "None here lets contingency answer over the streamed text"
    assert invoker.run.await_count == 1  # peer-b never attempted


@pytest.mark.asyncio
async def test_streamed_then_empty_still_records_the_peer_as_available(loop, invoker, monkeypatch):
    """The surface was blind in exactly the degenerate case it was built for:
    streamed-then-empty skipped recording entirely while still entering fallback
    state and telling the user replies were running on that peer."""
    monkeypatch.setattr(
        roster, "failover_invocations",
        lambda home, base, *a, **k: [("peer-a", replace(base, resume_session_id=None))],
    )
    streamed: dict = {}

    async def _stream_then_empty(*a, **k):
        streamed["text"] = "partial answer already shown"
        return _output(text="", session_id="a-1")

    invoker.run = AsyncMock(side_effect=_stream_then_empty)
    loop._merge_session_metadata = AsyncMock()
    loop._session_mgr = MagicMock(update_activity=AsyncMock())

    await loop._try_roster_failover(
        session={"id": "s1"}, base_inv=CCInvocation(prompt="x", roster_eligible=True),
        channel=ChannelType.TERMINAL, model=CCModel.SONNET,
        effort=EffortLevel.LOW, prompt_text="x", streamed=streamed,
    )
    st = peer_availability.read_peer("peer-a")
    assert st is not None, "served the turn but recorded nothing"
    assert st.available is True


@pytest.mark.asyncio
async def test_streamed_then_local_error_clears_a_stale_block(loop, invoker, monkeypatch):
    """A peer that ANSWERS must not keep a stale "blocked" record.

    The peer streams text — the user can see it — and then a LOCAL fault ends the
    turn (an MCP crash, our own timeout). `note_failure` correctly declines that
    as evidence about the peer, but the earlier quota block then survives an
    attempt that demonstrably reached and served from this peer. Because records
    refresh only during a home-model outage, that false "blocked" can stand for
    days on the one surface built to show whether the standby is usable.
    """
    peer_availability.note_failure("peer-a", CCRateLimitError("429 quota"))
    assert peer_availability.read_peer("peer-a").available is False, "fixture precondition"

    monkeypatch.setattr(
        roster, "failover_invocations",
        lambda home, base, *a, **k: [("peer-a", replace(base, resume_session_id=None))],
    )
    streamed: dict = {}

    async def _stream_then_local_error(*a, **k):
        streamed["text"] = "answer already shown to the user"
        raise CCMCPError("genesis-health MCP server crashed")

    invoker.run = AsyncMock(side_effect=_stream_then_local_error)
    loop._merge_session_metadata = AsyncMock()
    loop._session_mgr = MagicMock(update_activity=AsyncMock())

    result = await loop._try_roster_failover(
        session={"id": "s1"}, base_inv=CCInvocation(prompt="x", roster_eligible=True),
        channel=ChannelType.TERMINAL, model=CCModel.SONNET,
        effort=EffortLevel.LOW, prompt_text="x", streamed=streamed,
    )

    assert result == "", "streamed text must still suppress contingency"
    st = peer_availability.read_peer("peer-a")
    assert st.available is True, "a peer that answered is still recorded blocked"
    assert st.reason == "", "stale quota reason survived a served turn"


@pytest.mark.asyncio
async def test_local_error_without_streaming_leaves_the_block_untouched(loop, invoker, monkeypatch):
    """The CONVERSE, so the fix above cannot over-reach.

    With no text streamed the peer did NOT demonstrably serve, so a local fault
    is no evidence of recovery — clearing the block there would invent an
    observation, which is the failure mode this whole module exists to avoid.
    """
    peer_availability.note_failure("peer-a", CCRateLimitError("429 quota"))
    monkeypatch.setattr(
        roster, "failover_invocations",
        lambda home, base, *a, **k: [("peer-a", replace(base, resume_session_id=None))],
    )
    invoker.run = AsyncMock(side_effect=CCMCPError("MCP crashed before any output"))
    loop._merge_session_metadata = AsyncMock()
    loop._session_mgr = MagicMock(update_activity=AsyncMock())

    await loop._try_roster_failover(
        session={"id": "s1"}, base_inv=CCInvocation(prompt="x", roster_eligible=True),
        channel=ChannelType.TERMINAL, model=CCModel.SONNET,
        effort=EffortLevel.LOW, prompt_text="x", streamed={},
    )
    st = peer_availability.read_peer("peer-a")
    assert st.available is False, "invented a recovery from a local fault"


@pytest.mark.asyncio
async def test_recorder_failure_never_abandons_the_failover(loop, invoker, monkeypatch):
    """Advisory bookkeeping must never decide whether the user gets an answer.

    Moving the recorder off the event loop put the module's never-raises
    guarantee at risk at the WRAPPER: `asyncio.to_thread` raises RuntimeError
    once the default executor is shut down, and the outer handler catches that
    by returning None — abandoning the entire failover, which is strictly worse
    than the lost row the move was protecting.
    """
    monkeypatch.setattr(
        roster, "failover_invocations",
        lambda home, base, *a, **k: [("peer-a", replace(base, resume_session_id=None))],
    )

    async def _explode(*a, **k):
        raise RuntimeError("cannot schedule new futures after shutdown")

    monkeypatch.setattr("asyncio.to_thread", _explode)
    invoker.run = AsyncMock(return_value=_output(text="the peer's answer"))
    loop._merge_session_metadata = AsyncMock()
    loop._session_mgr = MagicMock(update_activity=AsyncMock())

    result = await loop._try_roster_failover(
        session={"id": "s1"}, base_inv=CCInvocation(prompt="x", roster_eligible=True),
        channel=ChannelType.TERMINAL, model=CCModel.SONNET,
        effort=EffortLevel.LOW, prompt_text="x", streamed={},
    )
    assert result is not None, "a recorder fault threw away the peer's reply"
    assert "the peer's answer" in result


# NOTE: the generator-E effects matrix (what an attempt DID deciding whether it
# may be retried) lives on the feat/peer-effects-guard branch with the guard it
# tests. This file covers RECORDING semantics only.


@pytest.mark.asyncio
async def test_a_balance_refusal_after_streaming_is_not_cleared_as_available(
    loop, invoker, monkeypatch,
):
    """A drained peer that streamed text must stay BLOCKED.

    The stale-block clearing keys on whether `note_failure` DECLINED the
    exception — not on whether its write landed, which returns False for four
    different reasons (declined, lock contention, failed write, internal fault).

    This is the case that distinguishes them: a drained prepaid account surfaces
    as `CCProcessError("insufficient balance")`, which `_is_provider_refusal`
    accepts, and it arrives on the generic `CCError` branch alongside genuine
    local faults. Keyed on the write result, a transient write failure here would
    flip a real refusal into a recorded SUCCESS — and records only refresh during
    a home outage, so that false "available" stands for days.
    """
    peer_availability.note_failure("peer-a", CCRateLimitError("429 earlier"))
    monkeypatch.setattr(
        roster, "failover_invocations",
        lambda home, base, *a, **k: [("peer-a", replace(base, resume_session_id=None))],
    )
    streamed: dict = {"text": False, "tools": False}

    async def _stream_then_drained(*a, **k):
        streamed["text"] = True
        raise CCProcessError("API error: insufficient balance")

    invoker.run_streaming = AsyncMock(side_effect=_stream_then_drained)
    loop._merge_session_metadata = AsyncMock()
    loop._session_mgr = MagicMock(update_activity=AsyncMock())

    await loop._try_roster_failover(
        session={"id": "s1"}, base_inv=CCInvocation(prompt="x", roster_eligible=True),
        channel=ChannelType.TERMINAL, model=CCModel.SONNET,
        effort=EffortLevel.LOW, prompt_text="x",
        on_event=AsyncMock(), streamed=streamed,
    )
    st = peer_availability.read_peer("peer-a")
    assert st is not None and st.available is False, (
        "a drained peer was recorded AVAILABLE — a refusal was read as a decline"
    )


@pytest.mark.asyncio
async def test_a_balance_refusal_does_not_trigger_a_fresh_sticky_retry(
    loop, invoker, monkeypatch,
):
    """R5-P2. A DRAINED prepaid account reaches us as a generic ``CCProcessError``,
    because the invoker's global classifier deliberately does not know the
    balance phrases (teaching it would let a drained BACKUP report the primary as
    down). So the refusal lands on the generic stale-resume branch and buys a
    redundant full re-invocation on the same dead peer before the outer handler
    ever gets to classify it.

    A fresh session cannot refill an empty balance. Re-raise instead, and let the
    peer loop advance.
    """
    monkeypatch.setattr(
        roster, "failover_invocations",
        lambda home, base, *a, **k: [("peer-a", replace(base, resume_session_id=None))],
    )
    invoker.run_streaming = AsyncMock(
        side_effect=CCProcessError("API error: insufficient balance"),
    )
    loop._merge_session_metadata = AsyncMock()
    loop._session_mgr = MagicMock(update_activity=AsyncMock())
    loop._session_fallback_session = lambda s: {
        "roster_model": "peer-a", "cc_session_id": "sticky-1",
    }

    await loop._try_roster_failover(
        session={"id": "s1"}, base_inv=CCInvocation(prompt="x", roster_eligible=True),
        channel=ChannelType.TERMINAL, model=CCModel.SONNET,
        effort=EffortLevel.LOW, prompt_text="x",
        on_event=AsyncMock(), streamed={"text": False, "tools": False},
    )
    assert invoker.run_streaming.await_count == 1, (
        "retried a drained account with a fresh session"
    )


@pytest.mark.asyncio
async def test_an_ordinary_stale_resume_still_retries_fresh(loop, invoker, monkeypatch):
    """The other direction of the same gate, so the fix cannot be satisfied by
    simply never retrying. An ambiguous CCError on a sticky resume is exactly
    what the fresh retry exists for."""
    monkeypatch.setattr(
        roster, "failover_invocations",
        lambda home, base, *a, **k: [("peer-a", replace(base, resume_session_id=None))],
    )
    invoker.run_streaming = AsyncMock(side_effect=CCMCPError("stale resume"))
    loop._merge_session_metadata = AsyncMock()
    loop._session_mgr = MagicMock(update_activity=AsyncMock())
    loop._session_fallback_session = lambda s: {
        "roster_model": "peer-a", "cc_session_id": "sticky-1",
    }

    await loop._try_roster_failover(
        session={"id": "s1"}, base_inv=CCInvocation(prompt="x", roster_eligible=True),
        channel=ChannelType.TERMINAL, model=CCModel.SONNET,
        effort=EffortLevel.LOW, prompt_text="x",
        on_event=AsyncMock(), streamed={"text": False, "tools": False},
    )
    assert invoker.run_streaming.await_count == 2, "the fresh retry was lost"




@pytest.mark.asyncio
async def test_a_declined_limit_error_after_streaming_clears_a_stale_block(
    loop, invoker, monkeypatch,
):
    """The typed rate-limit branch is no longer refusals-only: since the MCP
    exclusion, a tool's own 429 arrives there typed CCRateLimitError and is
    correctly DECLINED as evidence about the peer. But the declined-plus-streamed
    stale-block clearing lived only on the generic CCError branch — so a
    previously blocked peer that had just SERVED text stayed falsely blocked,
    potentially for days, on exactly the branch that runs first.
    """
    peer_availability.note_failure("peer-a", CCRateLimitError("429 earlier"))
    monkeypatch.setattr(
        roster, "failover_invocations",
        lambda home, base, *a, **k: [("peer-a", replace(base, resume_session_id=None))],
    )
    streamed: dict = {"text": False}

    async def _stream_then_tool_429(*a, **k):
        streamed["text"] = True
        raise CCRateLimitError("MCP server 'web-search' returned error: 429 rate limit")

    invoker.run_streaming = AsyncMock(side_effect=_stream_then_tool_429)
    loop._merge_session_metadata = AsyncMock()
    loop._session_mgr = MagicMock(update_activity=AsyncMock())

    await loop._try_roster_failover(
        session={"id": "s1"}, base_inv=CCInvocation(prompt="x", roster_eligible=True),
        channel=ChannelType.TERMINAL, model=CCModel.SONNET,
        effort=EffortLevel.LOW, prompt_text="x",
        on_event=AsyncMock(), streamed=streamed,
    )
    st = peer_availability.read_peer("peer-a")
    assert st is not None and st.available is True, (
        "a peer that served text stayed blocked after a declined tool 429"
    )


@pytest.mark.asyncio
async def test_a_genuine_refusal_after_streaming_still_blocks(
    loop, invoker, monkeypatch,
):
    """The control: a REAL refusal after streaming must keep the peer blocked —
    the cleanup fires only for declined evidence, or the branch would launder
    genuine quota blocks into availability."""
    monkeypatch.setattr(
        roster, "failover_invocations",
        lambda home, base, *a, **k: [("peer-a", replace(base, resume_session_id=None))],
    )
    streamed: dict = {"text": False}

    async def _stream_then_refused(*a, **k):
        streamed["text"] = True
        raise CCRateLimitError("429 rate limit exceeded")

    invoker.run_streaming = AsyncMock(side_effect=_stream_then_refused)
    loop._merge_session_metadata = AsyncMock()
    loop._session_mgr = MagicMock(update_activity=AsyncMock())

    await loop._try_roster_failover(
        session={"id": "s1"}, base_inv=CCInvocation(prompt="x", roster_eligible=True),
        channel=ChannelType.TERMINAL, model=CCModel.SONNET,
        effort=EffortLevel.LOW, prompt_text="x",
        on_event=AsyncMock(), streamed=streamed,
    )
    st = peer_availability.read_peer("peer-a")
    assert st is not None and st.available is False, (
        "a genuine refusal after streaming was laundered into availability"
    )


@pytest.mark.asyncio
async def test_whitespace_only_streaming_is_not_evidence_of_service(
    loop, invoker, monkeypatch,
):
    """A whitespace-only text block is truthy but shows the user nothing. It set
    streamed["text"], and that flag is EVIDENCE — it records the peer as having
    served and clears stale blocks — so a silent-cap attempt emitting one blank
    could erase a genuine quota block."""
    peer_availability.note_failure("glm-5.2", CCRateLimitError("429 earlier"))

    async def _blank_stream(inv, on_event=None):
        calls = getattr(_blank_stream, "n", 0)
        _blank_stream.n = calls + 1
        if calls == 0:
            raise CCRateLimitError("limit")  # home
        if on_event:
            await on_event(StreamEvent(event_type="text", text="   \n  "))
        return _output(text="   ", session_id="glm-9")

    invoker.run_streaming = AsyncMock(side_effect=_blank_stream)
    await loop.handle_message_streaming(
        "hi", user_id="u1", channel=ChannelType.TERMINAL, on_event=AsyncMock(),
    )
    st = peer_availability.read_peer("glm-5.2")
    assert st is not None and st.available is False, (
        "a whitespace-only attempt cleared a genuine quota block"
    )


@pytest.mark.asyncio
async def test_a_dropped_result_never_advances_to_another_failover_peer(
    loop, invoker, monkeypatch,
):
    """The multi-peer loop, which the same-peer fix did not reach.

    Re-raising `CCStreamTruncatedError` out of `_run_failover_peer` only stopped
    the sticky retry on THAT peer. One level out, the loop's generic
    `except CCError` caught it and `continue`d — handing the identical prompt,
    with full tools, to the NEXT peer, after the first may already have made an
    MCP write or sent outreach. The `streamed["text"]` guard the loop otherwise
    leans on reads empty precisely because the answer is what the oversized line
    ate (Codex P1, PR #1625 round 2).

    None is the right return: the caller then runs contingency, which is a
    TOOL-LESS API call, so the turn degrades without repeating anything.
    """
    monkeypatch.setattr(
        roster, "failover_invocations",
        lambda home, base, *a, **k: [("peer-a", _PEER_INV), ("peer-b", _PEER_INV)],
    )
    invoker.run = AsyncMock(side_effect=CCStreamTruncatedError("result line dropped"))
    loop._merge_session_metadata = AsyncMock()
    loop._session_mgr = MagicMock(update_activity=AsyncMock())

    result = await loop._try_roster_failover(
        session={"id": "s1"}, base_inv=CCInvocation(prompt="x", roster_eligible=True),
        channel=ChannelType.TERMINAL, model=CCModel.SONNET,
        effort=EffortLevel.LOW, prompt_text="x", streamed={},
    )

    assert invoker.run.await_count == 1, (
        f"the prompt was replayed on another peer ({invoker.run.await_count}x)"
    )
    from genesis.cc.conversation import UNREPLAYABLE

    assert result is UNREPLAYABLE, (
        "the helper must report UNREPLAYABLE, not a bare None. Both are "
        "'let tool-less contingency answer', but None ALSO reads as ordinary "
        "exhausted failover, and the callers park that when contingency "
        "fails — durably scheduling a full-tools replay of writes the "
        "truncated peer already performed (Codex P1, PR #1625)"
    )
    assert result is not None, "contingency is tool-less and should still answer"


@pytest.mark.asyncio
async def test_a_dropped_result_after_streaming_does_not_double_output(
    loop, invoker, monkeypatch,
):
    """The other half of the peer-loop stop, and the one that needs "" not None.

    An oversized TOOL-RESULT line can be dropped after the peer already streamed
    answer text. Returning None there would let contingency answer a second time
    over text the user can already see — the same double-output the sibling
    branches return "" to prevent. The peer demonstrably served, so a stale
    block on it is cleared too.
    """
    monkeypatch.setattr(
        roster, "failover_invocations",
        lambda home, base, *a, **k: [("peer-a", _PEER_INV), ("peer-b", _PEER_INV)],
    )
    streamed: dict = {}

    async def _stream_then_drop(*a, **k):
        streamed["text"] = "partial answer already shown to the user"
        raise CCStreamTruncatedError("result line dropped")

    invoker.run = AsyncMock(side_effect=_stream_then_drop)
    loop._merge_session_metadata = AsyncMock()
    loop._session_mgr = MagicMock(update_activity=AsyncMock())

    # The OUTCOME assertions (result == "", one attempt, peer recorded
    # available) are all reproduced by the generic `except CCError` branch, so
    # asserting only those left this test surviving deletion of the very
    # handler it is named after — MEASURED by mutation during review. What
    # distinguishes the branches is that the truncation branch never OFFERS the
    # exception as evidence about the peer: the generic branch calls
    # note_failure first (which declines it, invisibly), this one does not call
    # it at all. Assert the call ORDER, not the end state.
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        peer_availability, "note_failure",
        lambda p, e: bool(calls.append(("failure", p))),
    )
    monkeypatch.setattr(
        peer_availability, "note_success",
        lambda p: bool(calls.append(("success", p))) or True,
    )

    result = await loop._try_roster_failover(
        session={"id": "s1"}, base_inv=CCInvocation(prompt="x", roster_eligible=True),
        channel=ChannelType.TERMINAL, model=CCModel.SONNET,
        effort=EffortLevel.LOW, prompt_text="x", streamed=streamed,
    )

    assert calls == [("success", "peer-a")], (
        f"the generic CCError branch ran instead of the truncation branch: {calls}"
    )
    assert invoker.run.await_count == 1  # peer-b never attempted
    # Not None (that lets contingency answer over the streamed text) and not ""
    # either: `streamed["text"]` means a text EVENT was observed, not that the
    # channel delivered it, so an empty return can show the user nothing at all
    # (Codex P1, PR #1625 round 4). A notice is safe in both cases.
    assert result is not None
    assert result.strip(), "a truncated failover returned an empty, non-error reply"
    assert "lost this answer" in result, f"the user was told nothing useful: {result!r}"

# ── Resumed-turn fragments must SURVIVE failover/contingency rebuilds ────────
# A resumed turn's system_prompt is not empty on Telegram: it carries the
# per-turn fragments assembled by the caller (topic context with the live
# proposal board, the session-control block, the research-routing nudge). Both
# degraded paths re-assemble the identity for a fresh/tool-less session — that
# part is right — but they must COMPOSE it with those fragments, not replace
# them: "approve this proposal" needs the board, and "the older ones" needs the
# thread context, on the peer exactly as much as on the home model.


@pytest.mark.asyncio
async def test_failover_preserves_resume_fragments_beside_identity(loop, invoker, monkeypatch):
    captured: dict = {}

    def _spy(home, base, *a, **k):
        captured["system_prompt"] = base.system_prompt
        return [("glm-5.2", _PEER_INV)]

    inv = CCInvocation(
        prompt="approve this proposal", resume_session_id="cc-1",
        system_prompt="## Pending proposals\n- P1: adopt-first demo",
    )
    monkeypatch.setattr(roster, "failover_invocations", _spy)
    invoker.run = AsyncMock(return_value=_output(text="peer reply", session_id="glm-1"))

    out = await loop._try_roster_failover(
        inv, session={"id": "cc-live"}, channel=ChannelType.TELEGRAM,
        model=CCModel.SONNET, effort=EffortLevel.MEDIUM,
        prompt_text="approve this proposal",
    )

    assert out and "peer reply" in out
    # Identity re-assembled for the fresh peer session…
    assert "You are Genesis." in captured["system_prompt"]
    # …AND the turn's own context preserved, not discarded by the rebuild.
    assert "P1: adopt-first demo" in captured["system_prompt"]


@pytest.mark.asyncio
async def test_contingency_preserves_resume_fragments_beside_identity(loop):
    # The tool-less contingency dispatcher gets referents for "this one"/"the
    # older ones" the same way: assembled identity + the turn's fragments.
    captured: dict = {}

    class _Dispatch:
        async def dispatch_conversation(self, messages, system_prompt):
            captured["system_prompt"] = system_prompt
            result = MagicMock()
            result.success, result.content, result.model = True, "ok", "router-x"
            return result

    loop._contingency = _Dispatch()

    out = await loop._try_contingency(
        "approve this proposal",
        "## Pending proposals\n- P1: adopt-first demo",
        ChannelType.TELEGRAM, session_id="cc-live", was_resume=True,
    )

    assert "ok" in out  # reply text (contingency preamble is prefixed by the caller)
    assert "You are Genesis." in captured["system_prompt"]
    assert "P1: adopt-first demo" in captured["system_prompt"]
@pytest.mark.asyncio
async def test_the_home_session_control_block_never_reaches_a_peer(loop, invoker, monkeypatch):
    """The peer runs a DIFFERENT model, so the home block states a falsehood.

    `_session_control_block` names the home model and effort and tells the
    session it can change them with `session_config`. Forwarded to a roster
    peer that is running something else, it is wrong on both counts — and
    `session_config` cannot change a peer dispatch that has already been
    created, so the instruction is not merely stale but unactionable.
    (CodeRabbit Major, PR #1627.)

    The peer-INDEPENDENT fragments must survive the removal; stripping the
    whole prompt would re-create the context loss two earlier findings were
    about.
    """
    captured: dict = {}

    def _spy(home, base, *a, **k):
        captured["system_prompt"] = base.system_prompt
        return [("glm-5.2", _PEER_INV)]

    control = _session_control_block(
        ChannelType.TELEGRAM, CCModel.OPUS, EffortLevel.HIGH, "cc-live"
    )
    assert control, "fixture precondition: the channel must emit a control block"

    inv = CCInvocation(
        prompt="approve this proposal",
        resume_session_id="cc-1",
        system_prompt=(
            "## Pending proposals\n- P1: adopt-first demo"
            + control
            + "\n\n## Long research\nroute it to the background lane"
        ),
    )
    monkeypatch.setattr(roster, "failover_invocations", _spy)
    invoker.run = AsyncMock(return_value=_output(text="peer reply", session_id="glm-1"))

    out = await loop._try_roster_failover(
        inv,
        session={"id": "cc-live"},
        channel=ChannelType.TELEGRAM,
        model=CCModel.OPUS,
        effort=EffortLevel.HIGH,
        prompt_text="approve this proposal",
    )

    assert out and "peer reply" in out
    sent = captured["system_prompt"]
    assert "Changing your own model / effort" not in sent, (
        "the home session-control block was forwarded to the peer"
    )
    assert "session_config" not in sent, (
        "the peer was told to call session_config on an already-created dispatch"
    )
    # The fragments on BOTH sides of the removed block survive.
    assert "P1: adopt-first demo" in sent, "topic context was removed with the block"
    assert "route it to the background lane" in sent, (
        "the fragment AFTER the block was removed with it"
    )


@pytest.mark.asyncio
async def test_a_sticky_peer_resume_is_not_handed_the_identity_again(loop, invoker, monkeypatch):
    """An outage past the first turn resumes the PEER's own session.

    That session already holds the assembled identity, so rebuilding it here
    re-sends the whole SOUL/user/cognitive-state prompt on every subsequent
    turn — burning context and putting a second, possibly conflicting,
    configuration snapshot next to the one the peer already had. The rebuild
    therefore has to happen AFTER the sticky decision, not before it.
    (Codex P2, PR #1627.)
    """
    captured: dict = {}

    def _spy(home, base, *a, **k):
        captured["system_prompt"] = base.system_prompt
        return [("glm-5.2", _PEER_INV)]

    monkeypatch.setattr(roster, "failover_invocations", _spy)
    invoker.run = AsyncMock(return_value=_output(text="peer reply", session_id="glm-1"))
    # A persisted peer session: this is what makes the turn a sticky resume.
    loop._session_fallback_session = lambda session: {
        "cc_session_id": "glm-1", "roster_model": "glm-5.2",
    }

    inv = CCInvocation(
        prompt="and the older ones?",
        resume_session_id="cc-1",
        system_prompt="## Pending proposals\n- P1: adopt-first demo",
    )
    out = await loop._try_roster_failover(
        inv,
        session={"id": "cc-live"},
        channel=ChannelType.TELEGRAM,
        model=CCModel.SONNET,
        effort=EffortLevel.MEDIUM,
        prompt_text="and the older ones?",
    )

    assert out and "peer reply" in out
    # Asserted on what the INVOKER received, not on what `failover_invocations`
    # was handed: the identity is built for every peer and dropped only on the
    # branch that actually resumes, so the upstream spy legitimately sees it.
    sent_inv = invoker.run.await_args.args[0]
    assert sent_inv.resume_session_id == "glm-1", (
        "the turn did not actually resume the sticky peer session, so this "
        "test would pass in the state it exists to catch"
    )
    sent = sent_inv.system_prompt or ""
    assert "You are Genesis." not in sent, (
        "the full identity was re-sent to a peer session that already has it"
    )
    # The per-turn fragment still goes: it is what the sticky session lacks.
    assert "P1: adopt-first demo" in sent, (
        "a sticky resume must still receive the turn's own context"
    )


def test_stripping_the_control_block_is_bounded_to_that_section():
    """Unit-level, because the strip is what protects the other fragments.

    A strip that ran to the end of the prompt would silently take the
    research-routing nudge with it, which is exactly the context-loss class
    this PR has already been reviewed for twice.
    """
    control = _session_control_block(ChannelType.TELEGRAM, CCModel.OPUS, EffortLevel.HIGH, "s1")
    prompt = "## Topic\nbefore" + control + "\n\n## After\nafter-text"
    out = _strip_session_control_block(prompt)

    assert "Changing your own model / effort" not in out
    assert "## Topic\nbefore" in out
    assert "## After\nafter-text" in out


def test_stripping_is_a_no_op_when_there_is_no_control_block():
    # CONTROL: a strip that always fired would silently reshape every prompt
    # on channels where the block is deliberately withheld (terminal, web).
    prompt = "## Topic\nonly this"
    assert _strip_session_control_block(prompt) == prompt
    assert _strip_session_control_block(None) is None
    assert _strip_session_control_block("") == ""
@pytest.mark.asyncio
async def test_a_peer_the_sticky_session_does_not_name_still_gets_the_identity(
    loop, invoker, monkeypatch
):
    """`sticky` is a PER-PEER fact, and using it as a per-turn flag drops the
    identity on a peer that never resumes.

    `_run_failover_peer` resumes only when `sticky["roster_model"] == peer_name`.
    So when the sticky session names peer-a and peer-a refuses this turn, peer-b
    runs FRESH — and a rebuild skipped upstream on "there is a sticky session"
    leaves peer-b answering with no Genesis persona at all. That is the exact
    failure the comment above the rebuild warns about, re-opened from the other
    side. (Found by adversarial audit, measured; PR #1627.)
    """
    seen: list = []

    def _spy(home, base, *a, **k):
        return [
            ("peer-a", replace(base, resume_session_id=None)),
            ("peer-b", replace(base, resume_session_id=None)),
        ]

    monkeypatch.setattr(roster, "failover_invocations", _spy)
    # sticky names peer-a ONLY.
    loop._session_fallback_session = lambda s: {
        "cc_session_id": "a-1",
        "roster_model": "peer-a",
    }

    async def _run(inv, *a, **k):
        seen.append((inv.system_prompt, inv.resume_session_id))
        if len(seen) == 1:
            raise CCRateLimitError("peer-a is out")  # force the walk to peer-b
        return _output(text="peer-b reply", session_id="b-1")

    invoker.run = AsyncMock(side_effect=_run)

    inv = CCInvocation(
        prompt="and the older ones?",
        resume_session_id="cc-1",
        system_prompt="## Pending proposals\n- P1: adopt-first demo",
    )
    out = await loop._try_roster_failover(
        inv,
        session={"id": "cc-live"},
        channel=ChannelType.TELEGRAM,
        model=CCModel.SONNET,
        effort=EffortLevel.MEDIUM,
        prompt_text="and the older ones?",
    )

    assert out and "peer-b reply" in out
    assert len(seen) == 2, f"the walk did not reach peer-b: {seen}"
    b_prompt, b_resume = seen[1]
    assert b_resume is None, "peer-b should be a FRESH session"
    assert "You are Genesis." in (b_prompt or ""), (
        "peer-b ran fresh with NO identity — it would answer as nobody"
    )


@pytest.mark.asyncio
async def test_a_stale_sticky_resume_retries_fresh_with_the_identity(loop, invoker, monkeypatch):
    """The fresh retry exists BECAUSE the sticky session may be gone.

    So "the peer's own session already holds the identity" is false exactly
    here, and the retry reuses the fresh invocation. If the identity was never
    built, that retry answers with no persona. The existing coverage of this
    path asserts only the call COUNT, which is why the regression was green.
    (Found by adversarial audit, measured; PR #1627.)
    """
    seen: list = []

    monkeypatch.setattr(
        roster,
        "failover_invocations",
        lambda home, base, *a, **k: [("peer-a", replace(base, resume_session_id=None))],
    )
    loop._session_fallback_session = lambda s: {
        "cc_session_id": "a-1",
        "roster_model": "peer-a",
    }

    async def _run(inv, *a, **k):
        seen.append((inv.system_prompt, inv.resume_session_id))
        if len(seen) == 1:
            raise CCMCPError("that session is gone")  # stale resume
        return _output(text="fresh reply", session_id="a-2")

    invoker.run = AsyncMock(side_effect=_run)

    inv = CCInvocation(
        prompt="and the older ones?",
        resume_session_id="cc-1",
        system_prompt="## Pending proposals\n- P1: adopt-first demo",
    )
    out = await loop._try_roster_failover(
        inv,
        session={"id": "cc-live"},
        channel=ChannelType.TELEGRAM,
        model=CCModel.SONNET,
        effort=EffortLevel.MEDIUM,
        prompt_text="and the older ones?",
    )

    assert out and "fresh reply" in out
    assert len(seen) == 2, f"the fresh retry did not happen: {seen}"
    # The RESUME attempt carries only the turn's fragments...
    assert "You are Genesis." not in (seen[0][0] or ""), (
        "the sticky resume was handed the identity it already has"
    )
    assert seen[0][1] == "a-1"
    # ...and the FRESH retry carries the identity.
    assert seen[1][1] is None, "the retry should be fresh"
    assert "You are Genesis." in (seen[1][0] or ""), "the fresh retry ran with NO identity"
def test_every_fragment_appended_after_the_control_block_opens_a_section():
    """The strip's boundary is a COUPLING to whatever composes the prompt.

    `_strip_session_control_block` ends the block at the next `\\n\\n## `. A
    fragment appended after it with a bare newline, or with no heading, would
    be swallowed along with the block — which is the context-loss class this
    PR has now been reviewed for three times. The coupling held when it was
    written (3 of 3 composition sites); this fails the moment it stops.

    Found by mutation: narrowing the delimiter to `\\n## ` left all tests green.
    """
    from genesis.cc.conversation import _BG_RESEARCH_ROUTING

    for fragment in (_BG_RESEARCH_ROUTING,):
        assert fragment.startswith("\n\n## "), (
            f"{fragment[:48]!r} is appended after the session-control block but "
            "does not open a new top-level section — the strip would consume it"
        )


@pytest.mark.asyncio
async def test_the_contingency_router_is_not_told_it_can_change_its_model(loop):
    """The contingency dispatcher is TOOL-LESS, so the block is worse here.

    It instructs the session to call `mcp__genesis-health__session_config` and
    explicitly not to refuse on the belief it cannot — to a model with no MCP
    tools — and names a model and effort that are not what the router runs.
    Same finding as the roster-peer path, sharper instance. (PR #1627.)
    """
    captured: dict = {}

    class _Dispatch:
        async def dispatch_conversation(self, messages, system_prompt):
            captured["system_prompt"] = system_prompt
            result = MagicMock()
            result.success, result.content, result.model = True, "ok", "router-x"
            return result

    loop._contingency = _Dispatch()
    control = _session_control_block(
        ChannelType.TELEGRAM, CCModel.OPUS, EffortLevel.HIGH, "cc-live"
    )
    assert control, "fixture precondition: the channel must emit a control block"

    out = await loop._try_contingency(
        "approve this proposal",
        "## Pending proposals\n- P1: adopt-first demo" + control,
        ChannelType.TELEGRAM,
        session_id="cc-live",
        was_resume=True,
    )

    assert "ok" in out
    sent = captured["system_prompt"]
    assert "session_config" not in sent, "a tool-less router was told to call session_config"
    assert "Changing your own model / effort" not in sent
    # The fragments it DOES need are untouched.
    assert "P1: adopt-first demo" in sent, "topic context was stripped too"
    assert "You are Genesis." in sent, "the identity rebuild was lost"
