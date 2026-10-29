"""Tests for rate_limit_park — park orchestration the CC call sites delegate to.

Covers dedup-key stability, caller_context lineage parsing, mode-gated foreground
parking + copy, fresh background parking, the re-limit-in-place lineage (the
architect-#1 fix), resumed-close, and backoff math. Uses the real in-memory db.
"""

from __future__ import annotations

import asyncio
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


# ── Park→resume round-trip preserves the FULL execution shape (2026-08-18) ──
# The canonical set of DirectSessionRequest fields that must survive a park:
# missing any one silently changes the resumed session's behavior (measured:
# a campaign resume ran WITHOUT its strategy doc because system_prompt was
# dropped at park time).
_ROUNDTRIP_FIELDS = {
    "prompt", "profile", "model", "effort", "timeout_s", "roster_model",
    "system_prompt", "source_tag", "skills", "tool_exceptions",
    "planning_instruction",
}


async def test_park_payload_preserves_full_request_shape(db):
    import json as _json

    req = DirectSessionRequest(
        prompt="campaign tick prompt",
        profile="campaign",
        system_prompt="STRATEGY DOC TEXT",
        source_tag="campaign",
        skills=["genesis-voice"],
        tool_exceptions=("WebFetch",),
        planning_instruction="think first",
        caller_context="campaign:c1",
        notify=False,
    )
    exc = CCRateLimitError("limit", raw_text="resets in 30m")
    pid = await rlp.park_direct_session(db, request=req, exc=exc, now=NOW, mode="live")
    assert pid is not None
    payload = _json.loads((await parks.get_by_id(db, pid))["payload_json"])
    missing = _ROUNDTRIP_FIELDS - set(payload)
    assert not missing, f"park payload dropped execution fields: {missing}"
    assert payload["system_prompt"] == "STRATEGY DOC TEXT"
    assert payload["source_tag"] == "campaign"
    assert payload["skills"] == ["genesis-voice"]
    assert list(payload["tool_exceptions"]) == ["WebFetch"]
    assert payload["planning_instruction"] == "think first"


async def test_resume_redispatch_and_consumer_preserve_execution_fields(db):
    import json as _json

    from genesis.cc import rate_limit_resume as rlr
    from genesis.runtime.init.direct_session import request_from_payload

    req = DirectSessionRequest(
        prompt="campaign tick prompt",
        profile="campaign",
        system_prompt="STRATEGY DOC TEXT",
        source_tag="campaign",
        skills=["genesis-voice"],
        tool_exceptions=("WebFetch",),
        planning_instruction="think first",
    )
    exc = CCRateLimitError("limit", raw_text="resets in 30m")
    pid = await rlp.park_direct_session(db, request=req, exc=exc, now=NOW, mode="live")
    park = await parks.get_by_id(db, pid)

    await rlr._redispatch(db, park)
    row = await db.execute_fetchall(
        "SELECT payload_json FROM direct_session_queue ORDER BY rowid DESC LIMIT 1",
    )
    qpayload = _json.loads(row[0][0])
    missing = (_ROUNDTRIP_FIELDS - {"roster_model"}) - {
        k for k, v in qpayload.items() if v is not None
    }
    assert not missing, f"queue payload dropped execution fields: {missing}"

    rebuilt = request_from_payload(qpayload)
    assert rebuilt.system_prompt == "STRATEGY DOC TEXT"
    assert rebuilt.source_tag == "campaign"
    assert rebuilt.skills == ["genesis-voice"]
    assert tuple(rebuilt.tool_exceptions) == ("WebFetch",)
    assert rebuilt.planning_instruction == "think first"
    assert rebuilt.profile == "campaign"



# ── capped turns that already issued tool calls (#3649) ─────────────────
_EARLY_SEND = [("outreach_send", {"message": "weekly update"})] + [
    ("web_search", {"q": f"q{i}"}) for i in range(25)
]


async def _park_ran(db, ran, prompt="send the update"):
    exc = CCQuotaExhaustedError("limit", raw_text="resets 5pm")
    return await rlp.park_conversation(
        db, prompt=prompt, origin_session_id="s1", exc=exc, now=NOW,
        mode="live", ran=ran,
    )


async def _payload(db, park_id):
    import json

    return json.loads((await parks.get_by_id(db, park_id))["payload_json"])


async def test_an_early_side_effect_is_named_however_many_calls_follow(db):
    outcome = await _park_ran(db, _EARLY_SEND)
    payload = await _payload(db, outcome.park_id)
    assert payload["prompt"] == "send the update", "the stored prompt must stay bare"
    names = [e["name"] for e in payload["ran"]]
    assert names[0] == "outreach_send" and len(names) == 26
    assert sum(1 for e in payload["ran"] if e["input"]) == 20, "previews: latest 20 only"
    note = rlp.with_already_ran_note(payload["prompt"], payload["ran"])
    assert "`outreach_send`, `web_search` x25" in note


async def test_a_secret_inside_a_string_input_never_reaches_the_park_row(db):
    secret = "sk-abcdefghijklmnopqrstuvwx1234"
    ran = [("Bash", {"command": f"curl -H 'Authorization: Bearer {secret}' https://example.test"}),
           ("browser_run_js", {"expression": f"fetch('/x?token={secret}')"})]
    outcome = await _park_ran(db, ran)
    row = await parks.get_by_id(db, outcome.park_id)
    assert secret not in row["payload_json"]
    assert "Bash" in row["payload_json"], "the call itself must still be named"


async def test_inputs_are_withheld_when_the_scrubber_cannot_vouch(db, monkeypatch):
    import genesis.transcript_analytics.scrub as scrub

    # Content handed back WITH the failure flag: the flag alone must withhold it.
    monkeypatch.setattr(scrub, "scrub_json", lambda *a, **k: ('{"command": "echo hi"}', True))
    outcome = await _park_ran(db, [("Bash", {"command": "echo hi"})])
    payload = await _payload(db, outcome.park_id)
    assert payload["ran"] == [{"name": "Bash", "input": "<input withheld>"}]


async def test_a_resend_lands_on_the_same_park_and_merges_its_calls(db):
    first = await _park_ran(db, [("outreach_send", {"message": "a"})])
    second = await _park_ran(db, [("memory_store", {"content": "b"})])
    assert first.park_id == second.park_id, "a resend opened a second park"
    names = [e["name"] for e in (await _payload(db, first.park_id))["ran"]]
    assert names == ["outreach_send", "memory_store"]
    rows = await db.execute_fetchall(
        "SELECT COUNT(*) FROM cc_rate_limit_parks WHERE status IN ('parked','resuming')",
    )
    assert rows[0][0] == 1


async def test_a_bare_resend_does_not_wipe_the_recorded_calls(db):
    first = await _park_ran(db, [("outreach_send", {"message": "a"})])
    await _park_ran(db, None)
    names = [e["name"] for e in (await _payload(db, first.park_id))["ran"]]
    assert names == ["outreach_send"]


def test_an_oversized_inventory_names_its_gap():
    inv = rlp.ran_inventory([("t", {})] * 600)
    note = rlp.with_already_ran_note("p", inv)
    assert "`t` x250, INVENTORY INCOMPLETE: 100 call(s) not recorded, `t` x250" in note


async def test_redispatch_renders_the_note_from_the_stored_inventory(db):
    import json as _json

    from genesis.cc import rate_limit_resume as rlr

    outcome = await _park_ran(db, [("outreach_send", {"message": "a"})])
    await rlr._redispatch(db, await parks.get_by_id(db, outcome.park_id))
    row = await db.execute_fetchall(
        "SELECT payload_json FROM direct_session_queue ORDER BY rowid DESC LIMIT 1",
    )
    prompt = _json.loads(row[0][0])["prompt"]
    assert prompt.startswith("send the update\n\n[Resume note")
    assert "`outreach_send`" in prompt


async def test_a_park_without_an_inventory_resumes_the_bare_prompt(db):
    import json as _json

    from genesis.cc import rate_limit_resume as rlr

    outcome = await _park_ran(db, None, prompt="plain request")
    await rlr._redispatch(db, await parks.get_by_id(db, outcome.park_id))
    row = await db.execute_fetchall(
        "SELECT payload_json FROM direct_session_queue ORDER BY rowid DESC LIMIT 1",
    )
    assert _json.loads(row[0][0])["prompt"] == "plain request"


# ── A RESUMED run that caps again adds its own calls to the record (#3654) ──
# The resumed run is a fresh direct session. If it sends and then hits the
# limit, the next resume must be told about that send, or it sends again.


async def _redispatched_prompt(db, park_id):
    import json as _json

    from genesis.cc import rate_limit_resume as rlr

    await rlr._redispatch(db, await parks.get_by_id(db, park_id))
    row = await db.execute_fetchall(
        "SELECT payload_json FROM direct_session_queue ORDER BY rowid DESC LIMIT 1",
    )
    return _json.loads(row[0][0])["prompt"]


async def _resume_caps(db, park_id, ran):
    """The resumed session for ``park_id`` hits the limit after ``ran``."""
    await parks.claim(db, park_id)
    resumed = _bg_request(caller_context=f"rate_limit_resume:{park_id}")
    exc = CCRateLimitError("limit", raw_text="resets in 1h")
    return await rlp.park_direct_session(
        db, request=resumed, exc=exc, now=NOW, mode="live", ran=ran,
    )


async def test_a_resumed_run_that_sent_then_capped_is_not_told_to_send_blind(db):
    secret = "sk-abcdefghijklmnopqrstuvwx1234"
    first = await _park_ran(db, [("web_search", {"q": "status"})])
    returned = await _resume_caps(db, first.park_id, [
        ("outreach_send", {"message": "weekly update"}),
        ("Bash", {"command": f"curl -H 'Authorization: Bearer {secret}' https://example.test"}),
    ])
    assert returned == first.park_id, "the re-limit left its own park"
    row = await parks.get_by_id(db, first.park_id)
    assert (row["attempts"], row["status"]) == (1, "parked"), "lineage changed"
    assert secret not in row["payload_json"], "the resumed run's inputs were not scrubbed"
    payload = await _payload(db, first.park_id)
    assert payload["prompt"] == "send the update", "the stored prompt must stay bare"
    assert [e["name"] for e in payload["ran"]] == ["web_search", "outreach_send", "Bash"]
    prompt = await _redispatched_prompt(db, first.park_id)
    assert "`outreach_send`" in prompt, "the next resume is not told about the send"


async def test_a_bare_relimit_keeps_the_recorded_calls_unchanged(db):
    first = await _park_ran(db, [("outreach_send", {"message": "a"})])
    before = (await parks.get_by_id(db, first.park_id))["payload_json"]
    await _resume_caps(db, first.park_id, None)
    await _resume_caps(db, first.park_id, [])
    row = await parks.get_by_id(db, first.park_id)
    assert row["payload_json"] == before
    assert row["attempts"] == 2


async def test_the_call_is_recorded_even_when_the_relimit_escalates(db):
    first = await _park_ran(db, None)
    await db.execute(
        "UPDATE cc_rate_limit_parks SET attempts = 39 WHERE id = ?", (first.park_id,),
    )
    await db.commit()
    await _resume_caps(db, first.park_id, [("outreach_send", {"message": "a"})])
    row = await parks.get_by_id(db, first.park_id)
    assert row["status"] == "needs_user"
    names = [e["name"] for e in (await _payload(db, first.park_id))["ran"]]
    assert names == ["outreach_send"], "an escalated park lost the send"


async def test_a_background_session_capped_after_a_send_parks_with_it(db):
    exc = CCRateLimitError("limit", raw_text="resets in 30m")
    pid = await rlp.park_direct_session(
        db, request=_bg_request(), exc=exc, now=NOW, mode="live",
        ran=[("outreach_send", {"message": "a"})],
    )
    payload = await _payload(db, pid)
    assert payload["prompt"] == "bg work"
    assert [e["name"] for e in payload["ran"]] == ["outreach_send"]
    assert "`outreach_send`" in await _redispatched_prompt(db, pid)


async def test_a_resume_whose_park_is_gone_reparks_with_its_calls(db):
    resumed = _bg_request(caller_context="rate_limit_resume:rlp-doesnotexist")
    exc = CCRateLimitError("limit", raw_text="resets in 30m")
    pid = await rlp.park_direct_session(
        db, request=resumed, exc=exc, now=NOW, mode="live",
        ran=[("outreach_send", {"message": "a"})],
    )
    assert pid != "rlp-doesnotexist"
    assert [e["name"] for e in (await _payload(db, pid))["ran"]] == ["outreach_send"]


async def test_the_runner_hands_the_resumed_runs_calls_to_the_park(db, monkeypatch):
    """Wiring: the calls DirectSessionRunner saw reach park_direct_session."""
    from genesis.cc.types import StreamEvent

    monkeypatch.setattr("genesis.cc.rate_limit_resume_config.effective_mode", lambda: "live")
    first = await _park_ran(db, [("web_search", {"q": "status"})])
    await parks.claim(db, first.park_id)

    async def _send_then_cap(invocation, *, on_event=None):
        await on_event(StreamEvent.from_raw({
            "type": "assistant",
            "message": {"content": [
                {"type": "text", "text": "sending"},
                {"type": "tool_use", "name": "outreach_send", "input": {"message": "a"}},
            ]},
        }))
        raise CCRateLimitError("limit", raw_text="resets in 30m")

    runner = _park_runner(db)
    runner._invoker.run_streaming = _send_then_cap
    req = DirectSessionRequest(
        prompt="send the update", delivery_mode=DeliveryMode.RESULT, origin_session_id="s1",
        caller_context=f"rate_limit_resume:{first.park_id}",
    )
    result = await runner._run_session(req, "sess-resumed")
    assert "parked for resume" in (result.error or "")
    names = [e["name"] for e in (await _payload(db, first.park_id))["ran"]]
    assert names == ["web_search", "outreach_send"]


@pytest.mark.parametrize("ending", [RuntimeError("boom"), asyncio.CancelledError()])
async def test_a_resumed_run_that_sent_then_died_records_the_send(db, ending):
    """Failed or cancelled, the run's park stays `resuming` and the stale-claim
    reclaim retries it later: that retry must be told about the send too."""
    from genesis.cc.types import StreamEvent

    first = await _park_ran(db, [("web_search", {"q": "status"})])
    await parks.claim(db, first.park_id)

    async def _send_then_die(invocation, *, on_event=None):
        await on_event(StreamEvent.from_raw({
            "type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "name": "outreach_send", "input": {"message": "a"}},
            ]},
        }))
        raise ending

    runner = _park_runner(db)
    runner._invoker.run_streaming = _send_then_die
    req = DirectSessionRequest(
        prompt="send the update", delivery_mode=DeliveryMode.RESULT, origin_session_id="s1",
        caller_context=f"rate_limit_resume:{first.park_id}",
    )
    with pytest.raises(type(ending)):
        await runner._run_session(req, "sess-resumed")
    row = await parks.get_by_id(db, first.park_id)
    assert row["status"] == "resuming"
    names = [e["name"] for e in (await _payload(db, first.park_id))["ran"]]
    assert names == ["web_search", "outreach_send"]
    assert "`outreach_send`" in await _redispatched_prompt(db, first.park_id)


async def test_a_resume_capped_while_parking_is_off_still_records_its_send(db):
    first = await _park_ran(db, None)
    await parks.claim(db, first.park_id)
    resumed = _bg_request(caller_context=f"rate_limit_resume:{first.park_id}")
    exc = CCRateLimitError("limit", raw_text="resets in 1h")
    pid = await rlp.park_direct_session(
        db, request=resumed, exc=exc, now=NOW, mode="off",
        ran=[("outreach_send", {"message": "a"})],
    )
    assert pid is None
    names = [e["name"] for e in (await _payload(db, first.park_id))["ran"]]
    assert names == ["outreach_send"]


async def test_a_failed_record_never_costs_the_park_or_the_relimit(db, monkeypatch):
    async def _broken(*a, **k):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(rlp, "_merge_into_open_park", _broken)
    exc = CCRateLimitError("limit", raw_text="resets in 30m")
    send = [("outreach_send", {"message": "a"})]
    pid = await rlp.park_direct_session(
        db, request=_bg_request(), exc=exc, now=NOW, mode="live", ran=send,
    )
    assert pid is not None, "a committed park was reported as not parked"
    returned = await _resume_caps(db, pid, send)
    assert returned == pid
    assert (await parks.get_by_id(db, pid))["attempts"] == 1, "the relimit was skipped"


@pytest.mark.parametrize("ending", ["cap", "error", "cancel", "off", "success"])
async def test_runner_releases_raw_inputs_before_session_exit(db, monkeypatch, ending):
    import weakref

    from genesis.cc.types import CCOutput, StreamEvent

    class ToolInput(dict):
        pass

    monkeypatch.setattr(rlp.cfg_mod, "effective_mode", lambda: "off" if ending == "off" else "live")
    first = await _park_ran(db, None)
    await parks.claim(db, first.park_id)

    async def emit(on_event):
        raw = ToolInput(content="synthetic payload " * 10000, password="synthetic secret")
        ref = weakref.ref(raw)
        await on_event(StreamEvent.from_raw({
            "type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Write", "input": raw},
            ]},
        }))
        return ref

    async def invoke(invocation, *, on_event=None):
        ref = await emit(on_event)
        assert ref() is None, "runner retains raw input after event callback returns"
        if ending in {"cap", "off"}:
            raise CCRateLimitError("limit", raw_text="resets in 30m")
        if ending == "error":
            raise RuntimeError("synthetic failure")
        if ending == "cancel":
            raise asyncio.CancelledError()
        return CCOutput(
            session_id="synthetic-run", text="done", model_used="synthetic",
            cost_usd=0, input_tokens=0, output_tokens=0, duration_ms=1, exit_code=0,
        )

    runner = _park_runner(db)
    runner._invoker.run_streaming = invoke
    req = DirectSessionRequest(prompt="synthetic task", caller_context=f"rate_limit_resume:{first.park_id}")
    error = {"error": RuntimeError, "cancel": asyncio.CancelledError, "off": CCRateLimitError}.get(ending)
    if error:
        with pytest.raises(error):
            await runner._run_session(req, "synthetic-run")
    else:
        await runner._run_session(req, "synthetic-run")
    inventory = (await _payload(db, first.park_id)).get("ran", [])
    if ending == "success":
        assert inventory == []  # successful completion still does not append
    else:
        assert [e["name"] for e in inventory] == ["Write"]
        assert "synthetic secret" not in inventory[0]["input"]


@pytest.mark.parametrize("count", [0, 1, 20, 21, 500, 501, 1003])
async def test_streamed_inventory_matches_existing_batch_contract(db, monkeypatch, count):
    from genesis.cc.types import StreamEvent

    monkeypatch.setattr(rlp.cfg_mod, "effective_mode", lambda: "live")
    calls = [(f"tool_{i % 3}", {"ordinal": i, "password": "synthetic secret"}) for i in range(count)]
    expected = rlp.ran_inventory(calls)
    first = await _park_ran(db, None)
    await parks.claim(db, first.park_id)

    async def invoke(invocation, *, on_event=None):
        for start in range(0, count, 3):
            await on_event(StreamEvent.from_raw({
                "type": "assistant", "message": {"content": [
                    {"type": "text", "text": "synthetic event"},
                    *[{"type": "tool_use", "name": name, "input": inp}
                      for name, inp in calls[start:start + 3]],
                ]},
            }))
        raise CCRateLimitError("limit", raw_text="resets in 30m")

    runner = _park_runner(db)
    runner._invoker.run_streaming = invoke
    req = DirectSessionRequest(prompt="synthetic task", caller_context=f"rate_limit_resume:{first.park_id}")
    await runner._run_session(req, "synthetic-run")
    actual = (await _payload(db, first.park_id)).get("ran", [])
    assert actual == expected
    assert sum(e.get("omitted", 1) for e in actual) == count
    assert len(actual) <= 501
    assert sum(bool(e.get("input")) for e in actual) <= 20


@pytest.mark.parametrize("path", ["fresh", "resume", "off", "failure"])
async def test_precomputed_inventory_is_not_scrubbed_again(db, monkeypatch, path):
    inventory = rlp.ran_inventory([("Write", {"content": "synthetic content"})])
    first = await _park_ran(db, None)
    await parks.claim(db, first.park_id)
    context = f"rate_limit_resume:{first.park_id}"

    def no_second_scrub(_):
        raise AssertionError("precomputed previews must not be re-encoded")

    monkeypatch.setattr(rlp, "ran_inventory", no_second_scrub)
    if path == "failure":
        await rlp.record_resumed_run_calls(db, context, inventory=inventory)
        pid = first.park_id
    else:
        result = await rlp.park_direct_session(
            db, request=_bg_request(caller_context=None if path == "fresh" else context),
            exc=CCRateLimitError("limit"), mode="off" if path == "off" else "live",
            inventory=inventory,
        )
        pid = first.park_id if path == "off" else result
    assert (await _payload(db, pid))["ran"] == inventory


@pytest.mark.parametrize("path", ["live", "off", "failure"])
@pytest.mark.parametrize("empty", [True, False])
async def test_raw_and_precomputed_inventory_are_mutually_exclusive(db, path, empty):
    raw = [] if empty else [("Write", {})]
    inventory = [] if empty else rlp.ran_inventory(raw)
    with pytest.raises(ValueError, match="not both"):
        if path == "failure":
            await rlp.record_resumed_run_calls(db, None, raw, inventory=inventory)
        else:
            await rlp.park_direct_session(
                db, request=_bg_request(), exc=CCRateLimitError("limit"),
                mode=path, ran=raw, inventory=inventory,
            )


@pytest.mark.parametrize("failure", ["exception", "flag"])
async def test_runner_keeps_call_name_when_preview_scrubbing_fails(db, monkeypatch, failure):
    from genesis.cc.types import StreamEvent

    def broken_scrub(_):
        if failure == "exception":
            raise RuntimeError("synthetic scrub failure")
        return "unsafe synthetic preview", True

    monkeypatch.setattr("genesis.transcript_analytics.scrub.scrub_json", broken_scrub)
    monkeypatch.setattr(rlp.cfg_mod, "effective_mode", lambda: "live")
    first = await _park_ran(db, None)
    await parks.claim(db, first.park_id)

    async def invoke(invocation, *, on_event=None):
        await on_event(StreamEvent.from_raw({
            "type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Write", "input": {"content": "synthetic"}},
            ]},
        }))
        raise CCRateLimitError("limit")

    runner = _park_runner(db)
    runner._invoker.run_streaming = invoke
    req = DirectSessionRequest(prompt="synthetic task", caller_context=f"rate_limit_resume:{first.park_id}")
    await runner._run_session(req, "synthetic-run")
    assert (await _payload(db, first.park_id))["ran"] == [{"name": "Write", "input": "<input withheld>"}]
