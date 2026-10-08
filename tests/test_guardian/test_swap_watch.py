"""Tests for the guardian-side container-swap reconciler (swap_watch).

The guardian re-asserts the swap invariant on observed state each tick:
persistent ``incus config`` knob + live cgroup ``memory.swap.max``, plus an
opt-in ``swap_ceiling_pct`` enforced via Incus's native key (with a cgroup
fallback) and tracked via an Incus instance key, ``user.genesis.swap_ceiling``
(never a local file — see the module docstring). Healthy path must be
read-only and silent; heals emit one INFO alert; failures emit a throttled
WARNING per problem class; an unreadable signal is NO signal, and a degraded
ceiling tick HOLDS rather than guessing. Subprocess and cgroup primitives are
mocked at the swap_watch/cgroup_ops seams.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from genesis.guardian import cgroup_ops, swap_watch
from genesis.guardian.alert.base import AlertSeverity

_MARKER_KEY = swap_watch._MARKER_KEY


class _Cfg:
    """Minimal config stub exposing what swap_watch reads."""

    def __init__(self, tmp_path, enabled=True, swap_ceiling_pct=None):
        self.container_name = "genesis"
        self.swap_reconcile_enabled = enabled
        self.swap_ceiling_pct = swap_ceiling_pct
        self._sp = tmp_path

    @property
    def state_path(self):
        return self._sp


def _parse_set_args(cmd) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for arg in cmd[4:]:
        k, _, v = arg.partition("=")
        pairs[k] = v
    return pairs


def _subproc(get_responses=None, set_responses=None):
    """Build a ``_run_subprocess`` fake for ``incus config get|set``.

    ``get_responses`` maps a config KEY (e.g. ``"limits.memory.swap"``) to
    ``(rc, stdout, stderr)``; a key absent from the map answers ``(0, "", "")``
    (Incus's own "unset" shape). ``set_responses`` maps a frozenset of the
    set call's ``{key: value}`` pairs to ``(rc, stdout, stderr)``; absent
    pairs default to success. Every call is recorded on ``fake.calls`` and
    every successful set's pairs dict on ``fake.sets``.
    """
    get_responses = get_responses or {}
    set_responses = set_responses or {}

    async def fake(*cmd, timeout=None):
        assert cmd[0] == "incus" and cmd[1] == "config"
        fake.calls.append(cmd)
        if cmd[2] == "get":
            assert cmd[3] == "--expanded"
            key = cmd[5]
            return get_responses.get(key, (0, "", ""))
        if cmd[2] == "set":
            pairs = _parse_set_args(cmd)
            fake.sets.append(pairs)
            return set_responses.get(frozenset(pairs.items()), (0, "", ""))
        raise AssertionError(f"unexpected incus config subcommand: {cmd[2]}")

    fake.calls = []
    fake.sets = []
    return fake


def _dispatcher(send_result=True):
    d = AsyncMock()
    d.send = AsyncMock(return_value=send_result)
    return d


def _sent_severities(dispatcher):
    return [call.args[0].severity for call in dispatcher.send.call_args_list]


def _sent_bodies(dispatcher):
    return [call.args[0].body for call in dispatcher.send.call_args_list]


def _set_calls_for_key(fake, key: str) -> list[dict[str, str]]:
    return [pairs for pairs in fake.sets if key in pairs]


# ---------------------------------------------------------------------------
# Baseline (no ceiling configured, "mode=none") — unchanged #3069 behavior.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_healthy_path_is_readonly_and_silent(tmp_path):
    """Key already true, marker absent, cgroup already max -> zero writes,
    zero alerts, even with the marker read added."""
    cfg = _Cfg(tmp_path)
    d = _dispatcher()
    sp = _subproc(get_responses={"limits.memory.swap": (0, "true", "")})
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="max")),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert sp.sets == []
    assert not d.send.called


@pytest.mark.asyncio
async def test_unset_knob_is_set_and_info_alerted(tmp_path):
    cfg = _Cfg(tmp_path)
    d = _dispatcher()
    sp = _subproc()
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="max")),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert _set_calls_for_key(sp, "limits.memory.swap") == [{"limits.memory.swap": "true"}]
    assert _sent_severities(d) == [AlertSeverity.INFO]


@pytest.mark.asyncio
async def test_explicit_false_knob_is_reconciled(tmp_path):
    cfg = _Cfg(tmp_path)
    d = _dispatcher()
    sp = _subproc(get_responses={"limits.memory.swap": (0, "false", "")})
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="max")),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert _set_calls_for_key(sp, "limits.memory.swap") == [{"limits.memory.swap": "true"}]


@pytest.mark.asyncio
async def test_live_zero_activates_and_info_alerts(tmp_path):
    cfg = _Cfg(tmp_path)
    d = _dispatcher()
    sp = _subproc(get_responses={"limits.memory.swap": (0, "true", "")})
    act = AsyncMock(return_value=True)
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="0")),
        patch.object(swap_watch, "activate_swap_max", act),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    act.assert_awaited_once_with("genesis")
    assert "live" in d.send.call_args.args[0].body


@pytest.mark.asyncio
async def test_byte_valued_key_with_live_zero_is_reported_not_overwritten(tmp_path):
    """A ceiling held in the key (set by hand, or by a past ceiling tick)
    survives a live 0 even with no ceiling configured THIS tick — #3069's
    own protection, carried forward unchanged into mode=none."""
    cfg = _Cfg(tmp_path)
    d = _dispatcher()
    sp = _subproc(get_responses={"limits.memory.swap": (0, "2147483648", "")})
    act = AsyncMock(return_value=True)
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="0")),
        patch.object(swap_watch, "activate_swap_max", act),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    act.assert_not_awaited()
    assert _sent_severities(d) == [AlertSeverity.WARNING]
    assert "2147483648" in d.send.call_args.args[0].body


@pytest.mark.asyncio
async def test_live_heal_with_unverified_config_warns_not_info(tmp_path):
    cfg = _Cfg(tmp_path)
    d = _dispatcher()
    sp = _subproc(get_responses={"limits.memory.swap": (1, "", "boom")})
    act = AsyncMock(return_value=True)
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="0")),
        patch.object(swap_watch, "activate_swap_max", act),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    act.assert_awaited_once()
    assert _sent_severities(d) == [AlertSeverity.WARNING]


@pytest.mark.asyncio
async def test_live_write_failure_warns(tmp_path):
    cfg = _Cfg(tmp_path)
    d = _dispatcher()
    sp = _subproc(get_responses={"limits.memory.swap": (0, "true", "")})
    act = AsyncMock(return_value=False)
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="0")),
        patch.object(swap_watch, "activate_swap_max", act),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert _sent_severities(d) == [AlertSeverity.WARNING]


@pytest.mark.asyncio
async def test_config_set_failure_warns(tmp_path):
    cfg = _Cfg(tmp_path)
    d = _dispatcher()
    sp = _subproc(set_responses={frozenset({"limits.memory.swap": "true"}.items()): (1, "", "nope")})
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="max")),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert _sent_severities(d) == [AlertSeverity.WARNING]


@pytest.mark.asyncio
async def test_incus_unreachable_is_no_signal(tmp_path):
    cfg = _Cfg(tmp_path)
    d = _dispatcher()

    async def raising(*cmd, timeout=None):
        raise OSError("incus not found")

    with (
        patch.object(swap_watch, "_run_subprocess", raising),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="max")),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert not d.send.called


@pytest.mark.asyncio
async def test_unreadable_cgroup_skips_live_half(tmp_path):
    cfg = _Cfg(tmp_path)
    d = _dispatcher()
    sp = _subproc(get_responses={"limits.memory.swap": (0, "true", "")})
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value=None)),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert not d.send.called


@pytest.mark.asyncio
async def test_kill_switch_disables_everything(tmp_path):
    """Disabled: zero subprocess calls at all, including the marker read."""
    cfg = _Cfg(tmp_path, enabled=False, swap_ceiling_pct=50)
    d = _dispatcher()
    sp = _subproc()
    with patch.object(swap_watch, "_run_subprocess", sp):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert sp.calls == []
    assert not d.send.called


@pytest.mark.asyncio
async def test_dispatch_failure_never_raises(tmp_path):
    cfg = _Cfg(tmp_path)
    d = AsyncMock()
    d.send = AsyncMock(side_effect=RuntimeError("boom"))
    sp = _subproc()
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="max")),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)  # must not raise


def test_run_check_wires_the_watch():
    from genesis.guardian import check

    assert hasattr(check, "_check_container_swap_and_alert")


@pytest.mark.asyncio
async def test_bare_zero_knob_is_still_reconciled(tmp_path):
    cfg = _Cfg(tmp_path)
    d = _dispatcher()
    sp = _subproc(get_responses={"limits.memory.swap": (0, "0", "")})
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="max")),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert _set_calls_for_key(sp, "limits.memory.swap") == [{"limits.memory.swap": "true"}]


@pytest.mark.asyncio
async def test_bare_one_knob_is_not_reset(tmp_path):
    cfg = _Cfg(tmp_path)
    d = _dispatcher()
    sp = _subproc(get_responses={"limits.memory.swap": (0, "1", "")})
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="max")),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert _set_calls_for_key(sp, "limits.memory.swap") == []


# ---------------------------------------------------------------------------
# Marker revert (mode=none, swap_ceiling_pct removed/invalid) — a prior
# ceiling, tracked only via the Incus marker key, is unwound.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revert_native_marker_match_heals_atomically(tmp_path):
    """Key still holds the old ceiling, marker matches it -> ONE atomic set
    call carries BOTH the true-revert and the marker clear; live cgroup
    heals to max in the SAME tick, zero WARNINGs (the step-2 ordering fix)."""
    cfg = _Cfg(tmp_path)  # swap_ceiling_pct=None: removed
    d = _dispatcher()
    sp = _subproc(get_responses={
        "limits.memory.swap": (0, "2147483648", ""),
        _MARKER_KEY: (0, "2147483648", ""),
    })
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="0")),
        patch.object(swap_watch, "activate_swap_max", AsyncMock(return_value=True)) as act,
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    key_sets = _set_calls_for_key(sp, "limits.memory.swap")
    assert key_sets == [{"limits.memory.swap": "true", _MARKER_KEY: ""}]
    act.assert_awaited_once_with("genesis")  # current=="0" + key_now=="true" (not a ceiling) -> baseline heal
    assert _sent_severities(d) == [AlertSeverity.INFO]


@pytest.mark.asyncio
async def test_revert_native_write_failure_does_not_strand_the_marker(tmp_path):
    """genesis-architect finding (pre-commit review, 2026-10-08): if the
    atomic native-revert set call FAILS, key_now/marker_now/current all stay
    equal to the stale ceiling. Without the native_revert_attempted gate,
    step 3 would misread this as 'the fallback path was in effect', write
    the live cgroup to max anyway, and clear the marker -- stranding the
    persisted key at the old ceiling with NOTHING left to signal it needs
    fixing. Correct behavior: no cgroup write, marker left intact, one
    ceiling problem recorded."""
    cfg = _Cfg(tmp_path)
    d = _dispatcher()
    sp = _subproc(
        get_responses={
            "limits.memory.swap": (0, "2147483648", ""),
            _MARKER_KEY: (0, "2147483648", ""),
        },
        set_responses={
            frozenset({"limits.memory.swap": "true", _MARKER_KEY: ""}.items()): (1, "", "denied"),
        },
    )
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        # The config set failed, so Incus's own state is unchanged: the live
        # cgroup is still enforcing the old ceiling, exactly like the config.
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="2147483648")),
        patch.object(swap_watch, "write_swap_max", AsyncMock(return_value=True)) as wm,
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    wm.assert_not_awaited()
    assert _set_calls_for_key(sp, _MARKER_KEY) == [{"limits.memory.swap": "true", _MARKER_KEY: ""}]
    assert _sent_severities(d) == [AlertSeverity.WARNING]
    assert "2147483648" in d.send.call_args.args[0].body


@pytest.mark.asyncio
async def test_revert_cgroup_marker_match_path_switch(tmp_path):
    """Marker doesn't match the (already-true) key, but DOES match the live
    cgroup -> the cgroup-fallback path was in effect; revert writes max and
    clears the marker in a separate call."""
    cfg = _Cfg(tmp_path)
    d = _dispatcher()
    sp = _subproc(get_responses={
        "limits.memory.swap": (0, "true", ""),
        _MARKER_KEY: (0, "2147483648", ""),
    })
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="2147483648")),
        patch.object(swap_watch, "write_swap_max", AsyncMock(return_value=True)) as wm,
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    wm.assert_awaited_once_with("genesis", "max")
    assert _set_calls_for_key(sp, _MARKER_KEY) == [{_MARKER_KEY: ""}]
    assert _sent_severities(d) == [AlertSeverity.INFO]


@pytest.mark.asyncio
async def test_revert_operator_changed_value_clears_marker_alone(tmp_path):
    """Marker matches neither the key nor the live cgroup -> an operator set
    something else directly; clear the stale marker, touch nothing else."""
    cfg = _Cfg(tmp_path)
    d = _dispatcher()
    sp = _subproc(get_responses={
        "limits.memory.swap": (0, "true", ""),
        _MARKER_KEY: (0, "2147483648", ""),
    })
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="max")),
        patch.object(swap_watch, "write_swap_max", AsyncMock(return_value=True)) as wm,
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    wm.assert_not_awaited()
    assert sp.sets == [{_MARKER_KEY: ""}]
    assert not d.send.called


@pytest.mark.asyncio
async def test_revert_absent_marker_leaves_operator_ceiling_alone(tmp_path):
    cfg = _Cfg(tmp_path)
    d = _dispatcher()
    sp = _subproc(get_responses={"limits.memory.swap": (0, "2147483648", "")})
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="2147483648")),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert sp.sets == []
    assert not d.send.called


@pytest.mark.asyncio
async def test_revert_unreadable_marker_holds(tmp_path):
    """The marker get itself fails -> hold: no reclaim, no revert, no clear,
    anywhere this tick — never read 'unreadable' as 'changed'."""
    cfg = _Cfg(tmp_path)
    d = _dispatcher()
    sp = _subproc(get_responses={
        "limits.memory.swap": (0, "true", ""),
        _MARKER_KEY: (1, "", "timeout"),
    })
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="max")),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert sp.sets == []
    assert not d.send.called


# ---------------------------------------------------------------------------
# Ceiling configured (mode="ceiling") — native and fallback paths.
# ---------------------------------------------------------------------------

_TARGET = 10 * 1024 * 1024 * 1024  # 10 GiB, already page-aligned
_TARGET_S = str(_TARGET)


def _patch_target(monkeypatch, target=_TARGET):
    monkeypatch.setattr(swap_watch, "_compute_swap_ceiling_target", lambda config: target)


@pytest.mark.asyncio
async def test_native_path_asserts_bundled_atomically(tmp_path, monkeypatch):
    _patch_target(monkeypatch)
    cfg = _Cfg(tmp_path, swap_ceiling_pct=50)
    d = _dispatcher()
    sp = _subproc(get_responses={
        "limits.memory.swap": (0, "true", ""),
        "limits.memory": (0, "36GiB", ""),
    })
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value=_TARGET_S)),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert sp.sets == [{"limits.memory.swap": _TARGET_S, _MARKER_KEY: _TARGET_S}]
    assert _sent_severities(d) == [AlertSeverity.INFO]


@pytest.mark.asyncio
async def test_native_path_idempotent_when_matching(tmp_path, monkeypatch):
    _patch_target(monkeypatch)
    cfg = _Cfg(tmp_path, swap_ceiling_pct=50)
    d = _dispatcher()
    sp = _subproc(get_responses={
        "limits.memory.swap": (0, _TARGET_S, ""),
        "limits.memory": (0, "36GiB", ""),
        _MARKER_KEY: (0, _TARGET_S, ""),
    })
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value=_TARGET_S)),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert sp.sets == []
    assert not d.send.called


@pytest.mark.asyncio
async def test_native_path_reclaims_drifted_marker(tmp_path, monkeypatch):
    """Key already correct, but the marker never caught up (SHOULD-FIX #1) ->
    reclaim the marker ALONE; the real key is not touched again."""
    _patch_target(monkeypatch)
    cfg = _Cfg(tmp_path, swap_ceiling_pct=50)
    d = _dispatcher()
    sp = _subproc(get_responses={
        "limits.memory.swap": (0, _TARGET_S, ""),
        "limits.memory": (0, "36GiB", ""),
        _MARKER_KEY: (0, "", ""),
    })
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value=_TARGET_S)),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert sp.sets == [{_MARKER_KEY: _TARGET_S}]


@pytest.mark.asyncio
async def test_native_path_repairs_live_drift(tmp_path, monkeypatch):
    """Key and marker already correct, but the live cgroup was changed to
    'max' from outside -> direct cgroup write repairs it (Codex P2, the
    review's SHOULD-FIX #1 'reconcile live drift on the native path')."""
    _patch_target(monkeypatch)
    cfg = _Cfg(tmp_path, swap_ceiling_pct=50)
    d = _dispatcher()
    sp = _subproc(get_responses={
        "limits.memory.swap": (0, _TARGET_S, ""),
        "limits.memory": (0, "36GiB", ""),
        _MARKER_KEY: (0, _TARGET_S, ""),
    })
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="max")),
        patch.object(swap_watch, "write_swap_max", AsyncMock(return_value=True)) as wm,
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    wm.assert_awaited_once_with("genesis", _TARGET_S)
    assert _sent_severities(d) == [AlertSeverity.INFO]


@pytest.mark.asyncio
async def test_fallback_path_writes_cgroup_and_marker(tmp_path, monkeypatch):
    """No limits.memory cap -> the native key is inert; baseline-heals the
    key to true, enforces the ceiling on the live cgroup, and sets the
    marker in a SEPARATE call right after."""
    _patch_target(monkeypatch)
    cfg = _Cfg(tmp_path, swap_ceiling_pct=50)
    d = _dispatcher()
    sp = _subproc(get_responses={
        "limits.memory.swap": (0, "true", ""),
        "limits.memory": (0, "", ""),  # unset
    })
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="max")),
        patch.object(swap_watch, "write_swap_max", AsyncMock(return_value=True)) as wm,
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    wm.assert_awaited_once_with("genesis", _TARGET_S)
    assert sp.sets == [{_MARKER_KEY: _TARGET_S}]
    assert _sent_severities(d) == [AlertSeverity.INFO]


@pytest.mark.asyncio
async def test_fallback_path_idempotent_when_matching(tmp_path, monkeypatch):
    _patch_target(monkeypatch)
    cfg = _Cfg(tmp_path, swap_ceiling_pct=50)
    d = _dispatcher()
    sp = _subproc(get_responses={
        "limits.memory.swap": (0, "true", ""),
        "limits.memory": (0, "", ""),
        _MARKER_KEY: (0, _TARGET_S, ""),
    })
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value=_TARGET_S)),
        patch.object(swap_watch, "write_swap_max", AsyncMock(return_value=True)) as wm,
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    wm.assert_not_awaited()
    assert sp.sets == []
    assert not d.send.called


@pytest.mark.asyncio
async def test_fallback_path_reclaims_drifted_marker(tmp_path, monkeypatch):
    """Cgroup already correct (a prior tick's write succeeded) but the
    marker-set call failed or the guardian crashed between them -> reclaim
    the marker alone on the next tick, no second cgroup write."""
    _patch_target(monkeypatch)
    cfg = _Cfg(tmp_path, swap_ceiling_pct=50)
    d = _dispatcher()
    sp = _subproc(get_responses={
        "limits.memory.swap": (0, "true", ""),
        "limits.memory": (0, "", ""),
        _MARKER_KEY: (0, "", ""),
    })
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value=_TARGET_S)),
        patch.object(swap_watch, "write_swap_max", AsyncMock(return_value=True)) as wm,
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    wm.assert_not_awaited()
    assert sp.sets == [{_MARKER_KEY: _TARGET_S}]


@pytest.mark.asyncio
async def test_ceiling_live_zero_writes_target_not_max(tmp_path, monkeypatch):
    """Ceiling configured, live cgroup reads 0, key holds the byte ceiling ->
    the ceiling write wins over #3069's 'byte key + live 0 -> warn' rule
    (NOTE N4): these are disjoint code paths, this asserts which one runs."""
    _patch_target(monkeypatch)
    cfg = _Cfg(tmp_path, swap_ceiling_pct=50)
    d = _dispatcher()
    sp = _subproc(get_responses={
        "limits.memory.swap": (0, _TARGET_S, ""),
        "limits.memory": (0, "36GiB", ""),
        _MARKER_KEY: (0, _TARGET_S, ""),
    })
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="0")),
        patch.object(swap_watch, "write_swap_max", AsyncMock(return_value=True)) as wm,
        patch.object(swap_watch, "activate_swap_max", AsyncMock(return_value=True)) as act,
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    wm.assert_awaited_once_with("genesis", _TARGET_S)
    act.assert_not_awaited()
    assert _sent_severities(d) == [AlertSeverity.INFO]
    assert "WARNING" not in "".join(_sent_bodies(d))


@pytest.mark.asyncio
async def test_ceiling_cgroup_write_failure_is_a_ceiling_problem(tmp_path, monkeypatch):
    _patch_target(monkeypatch)
    cfg = _Cfg(tmp_path, swap_ceiling_pct=50)
    d = _dispatcher()
    sp = _subproc(get_responses={
        "limits.memory.swap": (0, _TARGET_S, ""),
        "limits.memory": (0, "36GiB", ""),
        _MARKER_KEY: (0, _TARGET_S, ""),
    })
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="max")),
        patch.object(swap_watch, "write_swap_max", AsyncMock(return_value=False)),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert _sent_severities(d) == [AlertSeverity.WARNING]
    assert "swap ceiling" in d.send.call_args.args[0].title.lower()


@pytest.mark.asyncio
async def test_string_equality_idempotence_whole_tick(tmp_path, monkeypatch):
    """Every observed value already equals the target in whatever form the
    reconciler itself writes -> zero writes anywhere, zero alerts."""
    _patch_target(monkeypatch)
    cfg = _Cfg(tmp_path, swap_ceiling_pct=50)
    d = _dispatcher()
    sp = _subproc(get_responses={
        "limits.memory.swap": (0, _TARGET_S, ""),
        "limits.memory": (0, "36GiB", ""),
        _MARKER_KEY: (0, _TARGET_S, ""),
    })
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value=_TARGET_S)),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert sp.sets == []
    assert not d.send.called


@pytest.mark.asyncio
async def test_kill_switch_disables_ceiling_too(tmp_path, monkeypatch):
    _patch_target(monkeypatch)
    cfg = _Cfg(tmp_path, enabled=False, swap_ceiling_pct=50)
    d = _dispatcher()
    sp = _subproc()
    with patch.object(swap_watch, "_run_subprocess", sp):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert sp.calls == []
    assert not d.send.called


# ---------------------------------------------------------------------------
# Degraded ticks (swap_ceiling_pct set, but the target or the limits.memory
# probe is not computable this tick) — hold, never guess.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_degraded_swaptotal_unreadable_holds_the_key(tmp_path, monkeypatch):
    """SwapTotal unreadable -> ceiling problem, key left UNTOUCHED even
    though it reads 'false' (spec item 2's explicit 'never reset to true').
    """
    _patch_target(monkeypatch, target=None)
    cfg = _Cfg(tmp_path, swap_ceiling_pct=50)
    d = _dispatcher()
    sp = _subproc(get_responses={"limits.memory.swap": (0, "false", "")})
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="max")),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert _set_calls_for_key(sp, "limits.memory.swap") == []
    assert _sent_severities(d) == [AlertSeverity.WARNING]
    assert "swap ceiling" in d.send.call_args.args[0].title.lower()


@pytest.mark.asyncio
async def test_degraded_swaptotal_unreadable_still_protects_live_zero(tmp_path, monkeypatch):
    """Same degraded tick, but live cgroup reads 0 and the key is NOT a
    ceiling -> the ORIGINAL ceiling-unaware protection still runs: swap is
    activated live (key untouched). Two independent WARNINGs this tick: one
    per problem class (ceiling held, nothing else failed -> only the
    ceiling WARNING fires; the live heal here succeeds)."""
    _patch_target(monkeypatch, target=None)
    cfg = _Cfg(tmp_path, swap_ceiling_pct=50)
    d = _dispatcher()
    sp = _subproc(get_responses={"limits.memory.swap": (0, "", "")})  # unset
    act = AsyncMock(return_value=True)
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="0")),
        patch.object(swap_watch, "activate_swap_max", act),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    act.assert_awaited_once_with("genesis")
    assert _set_calls_for_key(sp, "limits.memory.swap") == []
    severities = _sent_severities(d)
    assert AlertSeverity.INFO in severities  # the live heal
    assert AlertSeverity.WARNING in severities  # the held ceiling


@pytest.mark.asyncio
async def test_degraded_limits_memory_probe_failure_holds(tmp_path):
    """Target computes fine, but the SEPARATE limits.memory probe itself
    fails -> degraded (Codex P2 'warn when persistence is unknown'), not
    silently treated as either native or fallback."""
    cfg = _Cfg(tmp_path, swap_ceiling_pct=50)
    d = _dispatcher()
    sp = _subproc(get_responses={
        "limits.memory.swap": (0, "false", ""),
        "limits.memory": (1, "", "timeout"),
    })
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "_host_swap_total_bytes", lambda: 20 * 1024**3),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="max")),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert _set_calls_for_key(sp, "limits.memory.swap") == []
    assert _sent_severities(d) == [AlertSeverity.WARNING]


@pytest.mark.asyncio
async def test_swaptotal_unreadable_computes_to_none(monkeypatch):
    """Unit test of _compute_swap_ceiling_target's own degraded return, so
    the 'degraded' mode above is exercised against the REAL function
    somewhere, not only through the monkeypatched shortcut."""
    cfg = _Cfg(tmp_path=None, swap_ceiling_pct=50)
    monkeypatch.setattr(swap_watch, "_host_swap_total_bytes", lambda: None)
    assert swap_watch._compute_swap_ceiling_target(cfg) is None


def test_compute_target_floors_to_page(monkeypatch):
    monkeypatch.setattr(swap_watch, "_host_swap_total_bytes", lambda: 20 * 1024**3)
    cfg = _Cfg(tmp_path=None, swap_ceiling_pct=50)
    target = swap_watch._compute_swap_ceiling_target(cfg)
    page = __import__("os").sysconf("SC_PAGE_SIZE")
    assert target == 10 * 1024**3
    assert target % page == 0


def test_compute_target_none_when_below_one_page(monkeypatch):
    monkeypatch.setattr(swap_watch, "_host_swap_total_bytes", lambda: 1024)
    cfg = _Cfg(tmp_path=None, swap_ceiling_pct=0.0001)
    assert swap_watch._compute_swap_ceiling_target(cfg) is None


# ---------------------------------------------------------------------------
# Throttle: per-class independence, delivery-aware retry, legacy migration.
# ---------------------------------------------------------------------------


def test_ceiling_and_swapoff_failures_throttle_independently(tmp_path):
    now = datetime.now(UTC)
    state_file = tmp_path / "swap_watch_state.json"
    swap_watch._record_failure_alert(state_file, now, swap_watch._PROBLEM_CLASS_SWAP_OFF, True)
    assert swap_watch._failure_alert_due(state_file, now, swap_watch._PROBLEM_CLASS_CEILING)
    assert not swap_watch._failure_alert_due(state_file, now, swap_watch._PROBLEM_CLASS_SWAP_OFF)


def test_delivered_failure_throttles_24h(tmp_path):
    now = datetime.now(UTC)
    state_file = tmp_path / "state.json"
    swap_watch._record_failure_alert(state_file, now, "ceiling", True)
    soon = now + timedelta(hours=1)
    later = now + timedelta(hours=25)
    assert not swap_watch._failure_alert_due(state_file, soon, "ceiling")
    assert swap_watch._failure_alert_due(state_file, later, "ceiling")


def test_undelivered_failure_retries_in_five_minutes(tmp_path):
    """SHOULD-FIX #4: a failed DELIVERY must not adopt the 24h window."""
    now = datetime.now(UTC)
    state_file = tmp_path / "state.json"
    swap_watch._record_failure_alert(state_file, now, "ceiling", False)
    soon = now + timedelta(minutes=2)
    later = now + timedelta(minutes=6)
    assert not swap_watch._failure_alert_due(state_file, soon, "ceiling")
    assert swap_watch._failure_alert_due(state_file, later, "ceiling")


@pytest.mark.asyncio
async def test_down_dispatcher_does_not_loop_every_tick(tmp_path):
    """A dispatcher with no channels (send() returns False) does not record
    a 24h-delivered throttle, and the NEXT tick inside the 5 min retry
    window does not re-alert."""
    cfg = _Cfg(tmp_path)
    d = _dispatcher(send_result=False)
    set_fail = {frozenset({"limits.memory.swap": "true"}.items()): (1, "", "nope")}

    with (
        patch.object(swap_watch, "_run_subprocess", _subproc(set_responses=set_fail)),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="max")),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert d.send.await_count == 1
    d.send.reset_mock()

    with (
        patch.object(swap_watch, "_run_subprocess", _subproc(set_responses=set_fail)),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="max")),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert d.send.await_count == 0  # inside the 5 min retry window


def test_legacy_flat_timestamp_migrates_to_swap_off_delivered(tmp_path):
    state_file = tmp_path / "state.json"
    now = datetime.now(UTC)
    state_file.write_text(json.dumps({"last_failure_alert_at": now.isoformat()}))
    assert not swap_watch._failure_alert_due(state_file, now + timedelta(hours=1), "swap_off")
    assert swap_watch._failure_alert_due(state_file, now + timedelta(hours=1), "ceiling")


def test_legacy_per_class_bare_string_treated_as_delivered(tmp_path):
    state_file = tmp_path / "state.json"
    now = datetime.now(UTC)
    state_file.write_text(json.dumps({"last_failure_alert_at": {"ceiling": now.isoformat()}}))
    assert not swap_watch._failure_alert_due(state_file, now + timedelta(hours=1), "ceiling")
    assert swap_watch._failure_alert_due(state_file, now + timedelta(hours=25), "ceiling")


def test_corrupt_state_file_is_due(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text("{not json")
    assert swap_watch._failure_alert_due(state_file, datetime.now(UTC), "ceiling")


# ---------------------------------------------------------------------------
# cgroup_ops: read/write primitives.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_swap_max_reads_via_sudo():
    sp = AsyncMock(return_value=(0, "max\n", ""))
    with patch.object(cgroup_ops, "_run_subprocess", sp):
        assert await cgroup_ops.read_swap_max("genesis") == "max"
    sp.assert_awaited_once()
    assert sp.await_args.args[0] == "sudo"


@pytest.mark.asyncio
async def test_read_swap_max_failure_is_none():
    sp = AsyncMock(return_value=(1, "", "denied"))
    with patch.object(cgroup_ops, "_run_subprocess", sp):
        assert await cgroup_ops.read_swap_max("genesis") is None


@pytest.mark.asyncio
async def test_activate_swap_max_delegates_to_write_swap_max():
    wm = AsyncMock(return_value=True)
    with patch.object(cgroup_ops, "write_swap_max", wm):
        assert await cgroup_ops.activate_swap_max("genesis") is True
    wm.assert_awaited_once_with("genesis", "max")


@pytest.mark.asyncio
async def test_write_swap_max_writes_plain_integer():
    sp = AsyncMock(return_value=(0, "", ""))
    with patch.object(cgroup_ops, "_run_subprocess", sp):
        assert await cgroup_ops.write_swap_max("genesis", "10737418240") is True
    sp.assert_awaited_once()


@pytest.mark.asyncio
async def test_write_swap_max_rejects_non_integer_without_subprocess():
    sp = AsyncMock()
    with patch.object(cgroup_ops, "_run_subprocess", sp):
        assert await cgroup_ops.write_swap_max("genesis", "10GiB") is False
    sp.assert_not_called()


@pytest.mark.asyncio
async def test_write_swap_max_rejects_octal_ambiguous_leading_zero():
    sp = AsyncMock()
    with patch.object(cgroup_ops, "_run_subprocess", sp):
        assert await cgroup_ops.write_swap_max("genesis", "010") is False
    sp.assert_not_called()


@pytest.mark.asyncio
async def test_write_swap_max_rejects_negative():
    sp = AsyncMock()
    with patch.object(cgroup_ops, "_run_subprocess", sp):
        assert await cgroup_ops.write_swap_max("genesis", "-5") is False
    sp.assert_not_called()


@pytest.mark.asyncio
async def test_write_swap_max_accepts_bare_zero():
    sp = AsyncMock(return_value=(0, "", ""))
    with patch.object(cgroup_ops, "_run_subprocess", sp):
        assert await cgroup_ops.write_swap_max("genesis", "0") is True


# ---------------------------------------------------------------------------
# _is_swap_on / _is_parseable_incus_size matrix — unchanged from #3069.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", False),
        ("true", True),
        ("True", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("false", False),
        ("False", False),
        ("0", False),
        ("no", False),
        ("off", False),
        ("  true  ", True),
        ("  false  ", False),
        ("garbage", False),
        ("2147483648", True),
        ("10GiB", True),
        ("5Gib", True),  # suffix-agnostic since #3069's round-4 fix
        ("5XB", True),
        ("1.5GiB", True),
        ("5 GiB", True),
        ("0B", False),
        ("0 bytes", False),
        ("00GiB", False),
        ("0GiB", False),
        ("010GiB", True),  # leading zero but genuinely nonzero
        ("0" * 5000 + "1GiB", True),  # long zero-padded, nonzero — no int() overflow
        ("0" * 5000, False),  # long zero-padded, all zero
    ],
)
def test_is_swap_on_matrix(raw, expected):
    assert swap_watch._is_swap_on(raw) is expected
