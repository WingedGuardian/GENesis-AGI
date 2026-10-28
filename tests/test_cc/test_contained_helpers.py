"""Contained diagnostics preserve auth ambiguity, deadlines and process ownership."""

import os
import sys
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from genesis.cc import login_health, peer_helpers
from genesis.cc.invoker import CCInvoker
from genesis.cc.peer_segment import PeerSegment
from genesis.cc.types import CCInvocation
from genesis.util.proc_kill import ProcessCleanupUncertain


@pytest.fixture
def contained(tmp_path):
    return CCInvocation(prompt="Public diagnostic", system_prompt="Public identity",
        working_dir=str(tmp_path), origin="external_untrusted", disable_tools=True,
        peer_segment=PeerSegment("c" * 32, time.time()+60, (), mode="conversation"))


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("confirmed", [False, True])
async def test_injected_auth_probe_bypasses_shared_cache_and_lock(monkeypatch, cached, confirmed):
    old = (time.monotonic(), cached)
    monkeypatch.setattr(login_health, "_probe_cache", old)
    monkeypatch.setattr(login_health, "refresh_token_expiry", lambda: datetime.now(UTC)-timedelta(days=1))
    lock = Mock(side_effect=AssertionError("Shared probe lock reached"))
    monkeypatch.setattr(login_health, "_get_probe_lock", lock)
    probe = AsyncMock(return_value=confirmed)
    result = await login_health.fallback_env_if_login_dead(
        probe=probe, use_cache=False, token_reader=lambda: "synthetic-token",
        token_is_stale=lambda: False,
    )
    assert (result is not None) is confirmed
    probe.assert_awaited_once()
    lock.assert_not_called()
    assert login_health._probe_cache is old


async def test_login_cleanup_uncertainty_never_falls_through_to_actual_work(contained, monkeypatch, tmp_path):
    monkeypatch.setattr(login_health, "refresh_token_expiry", lambda: datetime.now(UTC)-timedelta(days=1))
    monkeypatch.setattr(login_health, "_TOKEN_FILE", tmp_path / "absent.env")
    error = ProcessCleanupUncertain(process=object())
    probe = AsyncMock(side_effect=error)
    monkeypatch.setattr(peer_helpers, "run_helper", probe)
    invoker = CCInvoker(claude_path=sys.executable)
    with pytest.raises(ProcessCleanupUncertain) as caught:
        await invoker._apply_login_fallback({}, contained)
    assert caught.value is error
    probe.assert_awaited_once()


@pytest.mark.parametrize("outcome", ["success", "failure", "timeout", "uncertain", "cached"])
async def test_version_diagnostic_does_not_poison_owner_caches(contained, monkeypatch, outcome):
    invoker = CCInvoker(claude_path=sys.executable)
    real = os.path.realpath(sys.executable)
    st = os.stat(real)
    key = (real, st.st_ino, st.st_mtime_ns)
    if outcome == "cached":
        invoker._cc_versions[key] = (2, 1, 299)
    invoker._cc_version_failed_at[key] = invoker._clock()
    versions, failures = dict(invoker._cc_versions), dict(invoker._cc_version_failed_at)
    error = ProcessCleanupUncertain(process=object())
    helper = AsyncMock(return_value=(0, "2.1.299 (Claude Code)"))
    if outcome == "failure":
        helper.return_value = (1, "Synthetic version refusal")
    elif outcome == "timeout":
        helper.side_effect = TimeoutError
    elif outcome == "uncertain":
        helper.side_effect = error
    monkeypatch.setattr(peer_helpers, "run_helper", helper)
    if outcome == "uncertain":
        with pytest.raises(ProcessCleanupUncertain) as caught:
            await invoker._cc_version(contained)
        assert caught.value is error
    else:
        result = await invoker._cc_version(contained)
        assert result == ((2, 1, 299) if outcome in {"success", "cached"} else None)
    assert helper.await_count == (0 if outcome == "cached" else 1)
    assert invoker._cc_versions == versions and invoker._cc_version_failed_at == failures
    assert not invoker._cc_version_warned


async def test_interrupted_helper_retains_process_handle(contained, monkeypatch):
    from genesis.cc import invoker as invoker_module

    invoker = CCInvoker(claude_path=sys.executable)
    process = SimpleNamespace(pid=424215, returncode=None)
    error = ProcessCleanupUncertain(process=process)
    monkeypatch.setattr(peer_helpers, "spawn_owned", AsyncMock(side_effect=error))
    monkeypatch.setattr(PeerSegment, "launch_args", lambda self, args, *rest: args)
    monkeypatch.setattr(PeerSegment, "stop_and_drain", AsyncMock())
    monkeypatch.setattr(invoker, "_build_env", lambda inv: {k: "" for k in invoker_module._GH_CREDENTIAL_ENV})
    with pytest.raises(ProcessCleanupUncertain) as caught:
        await peer_helpers.run_helper(invoker, contained, ("auth", "status", "--json"))
    assert caught.value is error
    assert list(invoker._active_procs.values()) == [process]
    assert len(invoker._cleanup_owned_procs) == 1
