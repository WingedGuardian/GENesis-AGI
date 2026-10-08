"""Tests for the guardian-side container-swap reconciler (swap_watch).

The guardian re-asserts the swap invariant on observed state each tick:
persistent ``incus config`` knob + live cgroup ``memory.swap.max``, plus an
opt-in ``swap_ceiling_pct`` enforced via Incus's native key (with a cgroup
fallback) and removed only by an explicit ``swap_ceiling_pct: off``. Healthy
path must be
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
    """Key already true, cgroup already max -> zero writes, zero alerts."""
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
    """Disabled: zero subprocess calls at all."""
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


@pytest.mark.asyncio
async def test_true_word_one_with_live_zero_is_healed_not_reported(tmp_path):
    """"1" is an Incus TRUE word (a boolean), not a one-byte ceiling: under a
    hard limit Incus writes memory.swap.max=0 for it, so a live 0 is the
    ordinary swap-off defect and gets healed to max, never reported as a
    ceiling left alone."""
    cfg = _Cfg(tmp_path)
    d = _dispatcher()
    sp = _subproc(get_responses={"limits.memory.swap": (0, "1", "")})
    act = AsyncMock(return_value=True)
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="0")),
        patch.object(swap_watch, "activate_swap_max", act),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    act.assert_awaited_once_with("genesis")
    assert sp.sets == []
    assert _sent_severities(d) == [AlertSeverity.INFO]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2147483648", True),
        ("2GiB", True),
        (" 2GiB ", True),
        ("01", True),  # not a boolean word: a one-byte size
        ("1", False),  # Incus TRUE
        ("true", False),
        ("0", False),  # Incus FALSE
        ("off", False),
        ("0GiB", False),  # a zero size is swap-off, not a ceiling
        ("", False),
        ("garbage", False),
    ],
)
def test_is_byte_ceiling_matrix(raw, expected):
    assert swap_watch._is_byte_ceiling(raw) is expected


# ---------------------------------------------------------------------------
# Removing a ceiling: unset leaves it alone, `off` removes it.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unset_setting_leaves_a_native_ceiling_alone(tmp_path):
    """No swap_ceiling_pct at all: the reconciler does not manage a ceiling,
    so an existing byte value stays exactly as it is, key and cgroup."""
    cfg = _Cfg(tmp_path)
    d = _dispatcher()
    sp = _subproc(get_responses={"limits.memory.swap": (0, "2147483648", "")})
    wm = AsyncMock(return_value=True)
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="2147483648")),
        patch.object(swap_watch, "write_swap_max", wm),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert sp.sets == []
    wm.assert_not_awaited()
    assert not d.send.called


@pytest.mark.asyncio
async def test_unset_setting_leaves_a_fallback_cgroup_cap_alone(tmp_path):
    cfg = _Cfg(tmp_path)
    d = _dispatcher()
    sp = _subproc(get_responses={"limits.memory.swap": (0, "true", "")})
    wm = AsyncMock(return_value=True)
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="2147483648")),
        patch.object(swap_watch, "write_swap_max", wm),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert sp.sets == []
    wm.assert_not_awaited()
    assert not d.send.called


@pytest.mark.asyncio
async def test_off_removes_a_native_ceiling_and_heals_the_live_zero(tmp_path):
    """`off` with a byte-ceiling key: the key goes back to true (Incus then
    writes 0 to the live cgroup under a hard limit) and the same tick opens
    the live 0 to max. One INFO, no WARNING."""
    cfg = _Cfg(tmp_path, swap_ceiling_pct=swap_watch.SWAP_CEILING_OFF)
    d = _dispatcher()
    sp = _subproc(get_responses={"limits.memory.swap": (0, "2147483648", "")})
    act = AsyncMock(return_value=True)
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="0")),
        patch.object(swap_watch, "activate_swap_max", act),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert sp.sets == [{"limits.memory.swap": "true"}]
    act.assert_awaited_once_with("genesis")
    assert _sent_severities(d) == [AlertSeverity.INFO]
    assert "swap_ceiling_pct: off" in d.send.call_args.args[0].body


@pytest.mark.asyncio
async def test_off_lifts_a_fallback_cgroup_cap(tmp_path):
    """`off` with the key already true and a finite live cap (the fallback
    path's): the cap is lifted to max, the key is not touched."""
    cfg = _Cfg(tmp_path, swap_ceiling_pct=swap_watch.SWAP_CEILING_OFF)
    d = _dispatcher()
    sp = _subproc(get_responses={"limits.memory.swap": (0, "true", "")})
    wm = AsyncMock(return_value=True)
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="2147483648")),
        patch.object(swap_watch, "write_swap_max", wm),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    wm.assert_awaited_once_with("genesis", "max")
    assert sp.sets == []
    assert _sent_severities(d) == [AlertSeverity.INFO]


@pytest.mark.asyncio
async def test_off_is_idempotent_once_converged(tmp_path):
    cfg = _Cfg(tmp_path, swap_ceiling_pct=swap_watch.SWAP_CEILING_OFF)
    d = _dispatcher()
    sp = _subproc(get_responses={"limits.memory.swap": (0, "true", "")})
    wm = AsyncMock(return_value=True)
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="max")),
        patch.object(swap_watch, "write_swap_max", wm),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert sp.sets == []
    wm.assert_not_awaited()
    assert not d.send.called


@pytest.mark.asyncio
async def test_off_with_a_failed_key_write_leaves_the_cgroup_and_warns(tmp_path):
    """The key cannot be set back to true: the live ceiling still matches the
    key, so the cgroup is left alone (the two stay consistent) and one
    ceiling WARNING names the value. The next tick retries."""
    cfg = _Cfg(tmp_path, swap_ceiling_pct=swap_watch.SWAP_CEILING_OFF)
    d = _dispatcher()
    sp = _subproc(
        get_responses={"limits.memory.swap": (0, "2147483648", "")},
        set_responses={frozenset({"limits.memory.swap": "true"}.items()): (1, "", "denied")},
    )
    wm = AsyncMock(return_value=True)
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="2147483648")),
        patch.object(swap_watch, "write_swap_max", wm),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    wm.assert_not_awaited()
    assert _sent_severities(d) == [AlertSeverity.WARNING]
    assert "2147483648" in d.send.call_args.args[0].body


@pytest.mark.asyncio
async def test_off_with_an_unreadable_key_warns_and_leaves_the_cgroup(tmp_path):
    """`off` but limits.memory.swap cannot be read: a byte ceiling may still be
    stored there and would come back at the next restart, so the live cap is
    not lifted (no false "removed") and a ceiling WARNING says why."""
    cfg = _Cfg(tmp_path, swap_ceiling_pct=swap_watch.SWAP_CEILING_OFF)
    d = _dispatcher()
    sp = _subproc(get_responses={"limits.memory.swap": (1, "", "timeout")})
    wm = AsyncMock(return_value=True)
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="2147483648")),
        patch.object(swap_watch, "write_swap_max", wm),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    wm.assert_not_awaited()
    assert sp.sets == []
    assert _sent_severities(d) == [AlertSeverity.WARNING]
    assert "not removed" in d.send.call_args.args[0].body


@pytest.mark.asyncio
async def test_off_still_heals_a_false_key(tmp_path):
    cfg = _Cfg(tmp_path, swap_ceiling_pct=swap_watch.SWAP_CEILING_OFF)
    d = _dispatcher()
    sp = _subproc(get_responses={"limits.memory.swap": (0, "false", "")})
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="max")),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert sp.sets == [{"limits.memory.swap": "true"}]


@pytest.mark.asyncio
async def test_off_never_computes_a_target(tmp_path, monkeypatch):
    """`off` is not a number: no SwapTotal read, no limits.memory probe."""
    monkeypatch.setattr(swap_watch, "_host_swap_total_bytes", lambda: pytest.fail("read SwapTotal"))
    cfg = _Cfg(tmp_path, swap_ceiling_pct=swap_watch.SWAP_CEILING_OFF)
    d = _dispatcher()
    sp = _subproc(get_responses={"limits.memory.swap": (0, "true", "")})
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="max")),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert [c[5] for c in sp.calls] == ["limits.memory.swap"]


# ---------------------------------------------------------------------------
# Ceiling configured (mode="ceiling") — native and fallback paths.
# ---------------------------------------------------------------------------

_TARGET = 10 * 1024 * 1024 * 1024  # 10 GiB, already page-aligned
_TARGET_S = str(_TARGET)


def _patch_target(monkeypatch, target=_TARGET):
    monkeypatch.setattr(swap_watch, "_compute_swap_ceiling_target", lambda config: target)


@pytest.mark.asyncio
async def test_native_path_asserts_the_key(tmp_path, monkeypatch):
    _patch_target(monkeypatch)
    cfg = _Cfg(tmp_path, swap_ceiling_pct=50)
    d = _dispatcher()
    sp = _subproc(get_responses={
        "limits.memory.swap": (0, "true", ""),
        "limits.memory": (0, "8GiB", ""),
    })
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value=_TARGET_S)),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert sp.sets == [{"limits.memory.swap": _TARGET_S}]
    assert _sent_severities(d) == [AlertSeverity.INFO]


@pytest.mark.asyncio
async def test_native_path_idempotent_when_matching(tmp_path, monkeypatch):
    _patch_target(monkeypatch)
    cfg = _Cfg(tmp_path, swap_ceiling_pct=50)
    d = _dispatcher()
    sp = _subproc(get_responses={
        "limits.memory.swap": (0, _TARGET_S, ""),
        "limits.memory": (0, "8GiB", ""),
    })
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value=_TARGET_S)),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert sp.sets == []
    assert not d.send.called


@pytest.mark.asyncio
async def test_native_path_repairs_live_drift(tmp_path, monkeypatch):
    """Key already correct, but the live cgroup was changed to 'max' from
    outside -> a direct cgroup write repairs it."""
    _patch_target(monkeypatch)
    cfg = _Cfg(tmp_path, swap_ceiling_pct=50)
    d = _dispatcher()
    sp = _subproc(get_responses={
        "limits.memory.swap": (0, _TARGET_S, ""),
        "limits.memory": (0, "8GiB", ""),
    })
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="max")),
        patch.object(swap_watch, "write_swap_max", AsyncMock(return_value=True)) as wm,
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    wm.assert_awaited_once_with("genesis", _TARGET_S)
    assert sp.sets == []
    assert _sent_severities(d) == [AlertSeverity.INFO]


@pytest.mark.asyncio
async def test_soft_memory_limit_takes_the_fallback_path(tmp_path, monkeypatch):
    """limits.memory.enforce=soft: Incus applies limits.memory.swap only in
    its hard-limit branch (driver_lxc.go v6.0.0), so the key is not written as
    a ceiling; the ceiling goes to the live cgroup instead."""
    _patch_target(monkeypatch)
    cfg = _Cfg(tmp_path, swap_ceiling_pct=50)
    d = _dispatcher()
    sp = _subproc(get_responses={
        "limits.memory.swap": (0, "true", ""),
        "limits.memory": (0, "8GiB", ""),
        "limits.memory.enforce": (0, "soft", ""),
    })
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="max")),
        patch.object(swap_watch, "write_swap_max", AsyncMock(return_value=True)) as wm,
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert sp.sets == []
    wm.assert_awaited_once_with("genesis", _TARGET_S)
    assert _sent_severities(d) == [AlertSeverity.INFO]


@pytest.mark.asyncio
async def test_unreadable_enforce_probe_is_degraded(tmp_path, monkeypatch):
    _patch_target(monkeypatch)
    cfg = _Cfg(tmp_path, swap_ceiling_pct=50)
    d = _dispatcher()
    sp = _subproc(get_responses={
        "limits.memory.swap": (0, "false", ""),
        "limits.memory": (0, "8GiB", ""),
        "limits.memory.enforce": (1, "", "timeout"),
    })
    wm = AsyncMock(return_value=True)
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="max")),
        patch.object(swap_watch, "write_swap_max", wm),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert sp.sets == []
    wm.assert_not_awaited()
    assert _sent_severities(d) == [AlertSeverity.WARNING]


@pytest.mark.asyncio
async def test_fallback_path_writes_the_cgroup(tmp_path, monkeypatch):
    """No limits.memory cap -> the native key is inert; the ceiling is
    enforced on the live cgroup and the key is left as the swap-on value."""
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
    assert sp.sets == []
    assert _sent_severities(d) == [AlertSeverity.INFO]


@pytest.mark.asyncio
async def test_fallback_path_idempotent_when_matching(tmp_path, monkeypatch):
    _patch_target(monkeypatch)
    cfg = _Cfg(tmp_path, swap_ceiling_pct=50)
    d = _dispatcher()
    sp = _subproc(get_responses={
        "limits.memory.swap": (0, "true", ""),
        "limits.memory": (0, "", ""),
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
async def test_unreadable_key_with_a_ceiling_warns_not_just_info(tmp_path, monkeypatch):
    """The limits.memory.swap read fails on a ceiling tick: the live ceiling
    is still applied, but the persistent half is unverified, so a ceiling
    WARNING accompanies the INFO heal."""
    _patch_target(monkeypatch)
    cfg = _Cfg(tmp_path, swap_ceiling_pct=50)
    d = _dispatcher()
    sp = _subproc(get_responses={
        "limits.memory.swap": (1, "", "timeout"),
        "limits.memory": (0, "8GiB", ""),
    })
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="max")),
        patch.object(swap_watch, "write_swap_max", AsyncMock(return_value=True)) as wm,
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    wm.assert_awaited_once_with("genesis", _TARGET_S)
    assert sp.sets == []
    assert _sent_severities(d) == [AlertSeverity.INFO, AlertSeverity.WARNING]
    assert "unverified" in d.send.call_args.args[0].body


@pytest.mark.asyncio
async def test_ceiling_live_zero_writes_target_not_max(tmp_path, monkeypatch):
    """Ceiling configured, live cgroup reads 0, key holds the byte ceiling ->
    the ceiling write wins over #3069's 'byte key + live 0 -> warn' rule:
    these are disjoint code paths, this asserts which one runs."""
    _patch_target(monkeypatch)
    cfg = _Cfg(tmp_path, swap_ceiling_pct=50)
    d = _dispatcher()
    sp = _subproc(get_responses={
        "limits.memory.swap": (0, _TARGET_S, ""),
        "limits.memory": (0, "8GiB", ""),
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


@pytest.mark.asyncio
async def test_ceiling_cgroup_write_failure_is_a_ceiling_problem(tmp_path, monkeypatch):
    _patch_target(monkeypatch)
    cfg = _Cfg(tmp_path, swap_ceiling_pct=50)
    d = _dispatcher()
    sp = _subproc(get_responses={
        "limits.memory.swap": (0, _TARGET_S, ""),
        "limits.memory": (0, "8GiB", ""),
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
async def test_failed_ceiling_write_over_a_live_zero_is_also_swap_off(tmp_path, monkeypatch):
    """The live cgroup reads 0 and the ceiling write fails: swap is OFF, so the
    swap_off WARNING fires as well as the ceiling one."""
    _patch_target(monkeypatch)
    cfg = _Cfg(tmp_path, swap_ceiling_pct=50)
    d = _dispatcher()
    sp = _subproc(get_responses={
        "limits.memory.swap": (0, _TARGET_S, ""),
        "limits.memory": (0, "8GiB", ""),
    })
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="0")),
        patch.object(swap_watch, "write_swap_max", AsyncMock(return_value=False)),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    titles = [call.args[0].title for call in d.send.call_args_list]
    assert "Container swap reconcile FAILED" in titles
    assert "Container swap ceiling FAILED" in titles


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
    assert "uncapped" in _sent_bodies(d)[-1]


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
