"""Contained work isolates the whole native operation; ordinary Muse is unchanged."""

import asyncio
import hashlib
import json
from dataclasses import asdict, replace

import pytest

from genesis.cc import provider_readiness as isolation
from genesis.cc.direct_session import DirectSessionRequest
from genesis.cc.exceptions import CCReplayUnsafeError
from genesis.cc.invoker import CCInvoker
from genesis.cc.managed_session import fingerprint
from genesis.cc.peer_segment import PeerSegment
from genesis.cc.types import CCInvocation


def request(**changes):
    return DirectSessionRequest(
        prompt="Public conversation", profile="observe", system_prompt="Public identity",
        source_tag="agent_api", caller_context="agent_api:fixture", managed=True,
        notify=False, **changes,
    )


def test_explicit_tools_free_variant_preserves_facade_contract(tmp_path):
    import time

    policy = PeerSegment("a" * 32, time.time() + 60, (), mode="conversation")
    inv = CCInvocation(prompt="public", system_prompt="Public identity",
                       working_dir=str(tmp_path), origin="external_untrusted",
                       disable_tools=True, peer_segment=policy)
    for change in ({"disable_tools": False}, {"mcp_config": str(tmp_path / "owner.json")},
                   {"allowed_tools": ["Bash"]}, {"safe_mode": True}, {"origin": None},
                   {"timeout_s": True}, {"timeout_s": 7201}, {"working_dir": "."}):
        with pytest.raises(ValueError):
            replace(inv, **change)
    with pytest.raises(ValueError):
        PeerSegment("a" * 32, time.time() + 60, ())
    with pytest.raises(ValueError):
        replace(policy, tools=("mcp__genesis_peer__perform",))


@pytest.mark.parametrize("drain", ["success", "error", "cancel"])
@pytest.mark.parametrize("cancel_count", [0, 1, 3])
async def test_claim_retirement_requires_confirmed_drain(monkeypatch, drain, cancel_count):
    import time

    from genesis.cc import peer_segment
    from genesis.util.proc_kill import ProcessCleanupUncertain

    policy = PeerSegment("b" * 32, time.time() + 60, (), mode="conversation")
    entered, release = asyncio.Event(), asyncio.Event()
    async def stop(self):
        entered.set()
        await release.wait()
        if drain == "error":
            raise LookupError("synthetic drain failure")
        if drain == "cancel":
            raise asyncio.CancelledError
    monkeypatch.setattr(PeerSegment, "stop_and_drain", stop)
    async def run():
        async with policy.invocation():
            pass
    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        for _ in range(cancel_count):
            task.cancel()
            await asyncio.sleep(0)
        release.set()
        if drain != "success":
            with pytest.raises(ProcessCleanupUncertain):
                await task
        elif cancel_count:
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
        assert (policy.unit_name in peer_segment._INVOCATION_UNITS) is (drain != "success")
    finally:
        release.set()
        peer_segment._INVOCATION_UNITS.discard(policy.unit_name)


@pytest.mark.parametrize("nested_probe", [False, True])
async def test_contained_phase_is_sticky_across_scopes_tasks_threads(nested_probe):
    assert callable(getattr(isolation, "contained_provider", None)), "missing contained scope"
    assert not isolation.contained_provider()
    with isolation.isolated_provider(contained=True):
        with isolation.isolated_provider(probe=nested_probe):
            assert isolation.contained_provider() and isolation.provider_status_isolated()
            assert await asyncio.to_thread(isolation.contained_provider)
            async def child():
                return isolation.contained_provider()
            assert await asyncio.create_task(child())
        assert isolation.contained_provider()
    assert not isolation.contained_provider() and not isolation.provider_status_isolated()


@pytest.mark.parametrize("probe", [False, True])
def test_ordinary_provider_scope_semantics_unchanged(probe):
    with isolation.isolated_provider(probe=probe):
        assert isolation.provider_status_isolated()
        assert isolation.probing_provider() is probe
    assert not isolation.provider_status_isolated()


def test_default_false_marker_preserves_prechange_fingerprint():
    ordinary = request()
    legacy = asdict(ordinary)
    legacy.pop("contained_peer", None)
    expected = hashlib.sha256(json.dumps(legacy, sort_keys=True).encode()).hexdigest()
    assert fingerprint(ordinary) == expected
    assert hasattr(ordinary, "contained_peer"), "missing server-owned marker"
    contained = replace(ordinary, contained_peer=True)
    assert fingerprint(contained) != expected
    assert fingerprint(contained) == fingerprint(replace(contained))


@pytest.mark.parametrize("change", [
    {"contained_peer": 1}, {"contained_peer": None}, {"managed": False},
    {"profile": "interact"}, {"system_prompt": None}, {"system_prompt": " "},
    {"skills": ["owner-skill"]}, {"planning_instruction": "private instructions"},
    {"tool_exceptions": ("Bash",)},
])
def test_contained_request_rejects_trusted_enrichment(change):
    ordinary = request()
    assert hasattr(ordinary, "contained_peer"), "missing server-owned marker"
    with pytest.raises(ValueError):
        replace(ordinary, **({"contained_peer": True} | change))


@pytest.mark.parametrize("probe", [False, True])
def test_containment_preserves_known_work_no_replay_classification(probe):
    payload = {"api_error_status": 529, "num_turns": 2, "result": "Synthetic overload"}
    ordinary = CCInvoker._classify_error("", result=payload)
    assert isinstance(ordinary, CCReplayUnsafeError)
    with isolation.isolated_provider(contained=True, probe=probe):
        contained = CCInvoker._classify_error("", result=payload)
    assert type(contained) is type(ordinary)
    assert type(contained.__cause__) is type(ordinary.__cause__)
    assert contained.raw_event == ordinary.raw_event
