"""Tests for the guardian-side container-swap reconciler (swap_watch).

The guardian re-asserts the swap invariant on observed state each tick:
persistent ``incus config`` knob + live cgroup ``memory.swap.max``. Healthy
path must be read-only and silent; heals emit one INFO alert; failures emit a
throttled WARNING; an unreadable signal is NO signal. Subprocess and cgroup
primitives are mocked at the swap_watch/cgroup_ops seams.
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

    def __init__(self, tmp_path, enabled=True):
        self.container_name = "genesis"
        self.swap_reconcile_enabled = enabled
        self._sp = tmp_path

    @property
    def state_path(self):
        return self._sp


def _subproc(responses):
    """Build a _run_subprocess mock keyed on the subcommand ('get'/'set').

    ``responses`` maps 'get'/'set' → (rc, stdout, stderr). Records calls on
    the returned mock's ``calls`` list.
    """

    async def fake(*cmd, timeout=None):
        assert cmd[0] == "incus" and cmd[1] == "config"
        if cmd[2] == "get":
            # The get must read the EXPANDED config (profile-inherited true
            # must not be treated as unset).
            assert cmd[3] == "--expanded"
        fake.calls.append(cmd)
        return responses[cmd[2]]

    fake.calls = []
    return fake


def _dispatcher():
    d = AsyncMock()
    d.send = AsyncMock()
    return d


def _sent_severities(dispatcher):
    return [call.args[0].severity for call in dispatcher.send.call_args_list]


@pytest.mark.asyncio
async def test_healthy_path_is_readonly_and_silent(tmp_path):
    """Knob true + cgroup already max → no writes, no alerts."""
    cfg = _Cfg(tmp_path)
    d = _dispatcher()
    sp = _subproc({"get": (0, "true\n", "")})
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="max")),
        patch.object(swap_watch, "activate_swap_max", AsyncMock()) as act,
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert [c[2] for c in sp.calls] == ["get"]  # read-only: no config set
    act.assert_not_awaited()
    d.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_unset_knob_is_set_and_info_alerted(tmp_path):
    """`incus config get` on an unset key: rc=0, empty output → config set."""
    cfg = _Cfg(tmp_path)
    d = _dispatcher()
    sp = _subproc({"get": (0, "", ""), "set": (0, "", "")})
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="max")),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert [c[2] for c in sp.calls] == ["get", "set"]
    set_cmd = sp.calls[1]
    assert set_cmd[3:] == ("genesis", "limits.memory.swap", "true")
    assert _sent_severities(d) == [AlertSeverity.INFO]
    assert "limits.memory.swap" in d.send.call_args.args[0].body


@pytest.mark.asyncio
async def test_explicit_false_knob_is_reconciled(tmp_path):
    """Deliberate-override semantics: false → true (invariant wins; kill
    switch is the opt-out, documented in the alert body)."""
    cfg = _Cfg(tmp_path)
    d = _dispatcher()
    sp = _subproc({"get": (0, "false\n", ""), "set": (0, "", "")})
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="max")),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert [c[2] for c in sp.calls] == ["get", "set"]
    assert "swap_reconcile_enabled" in d.send.call_args.args[0].body


@pytest.mark.asyncio
async def test_live_zero_activates_and_info_alerts(tmp_path):
    """The sibling incident state: knob true but live cgroup still 0."""
    cfg = _Cfg(tmp_path)
    d = _dispatcher()
    sp = _subproc({"get": (0, "true", "")})
    act = AsyncMock(return_value=True)
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="0")),
        patch.object(swap_watch, "activate_swap_max", act),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    act.assert_awaited_once_with("genesis")
    assert _sent_severities(d) == [AlertSeverity.INFO]
    assert "live" in d.send.call_args.args[0].body


@pytest.mark.asyncio
async def test_live_heal_with_unverified_config_warns_not_info(tmp_path):
    """config read fails but the cgroup is still writable and at 0: activating
    live must NOT claim a clean reconcile — the persistent knob is unverified
    and a restart can revert swap to off, so this pages a WARNING, not INFO."""
    cfg = _Cfg(tmp_path)
    d = _dispatcher()
    sp = _subproc({"get": (1, "", "socket error")})  # config read fails
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="0")),
        patch.object(swap_watch, "activate_swap_max", AsyncMock(return_value=True)),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert [c[2] for c in sp.calls] == ["get"]  # no set attempted (read failed)
    assert _sent_severities(d) == [AlertSeverity.WARNING]
    body = d.send.call_args.args[0].body
    assert "persistent" in body and "revert" in body


@pytest.mark.asyncio
async def test_live_write_failure_warns_once_then_throttles(tmp_path):
    """A failed heal pages WARNING, but not again inside the throttle window."""
    cfg = _Cfg(tmp_path)
    d = _dispatcher()
    sp = _subproc({"get": (0, "true", "")})
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="0")),
        patch.object(swap_watch, "activate_swap_max", AsyncMock(return_value=False)),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert _sent_severities(d) == [AlertSeverity.WARNING]  # one, not two
    # After the window elapses, it re-pages.
    stale = datetime.now(UTC) - timedelta(hours=swap_watch._REALERT_HOURS + 1)
    (tmp_path / "swap_watch_state.json").write_text(
        json.dumps({"last_failure_alert_at": stale.isoformat()}),
    )
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="0")),
        patch.object(swap_watch, "activate_swap_max", AsyncMock(return_value=False)),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert _sent_severities(d) == [AlertSeverity.WARNING, AlertSeverity.WARNING]


@pytest.mark.asyncio
async def test_config_set_failure_warns(tmp_path):
    cfg = _Cfg(tmp_path)
    d = _dispatcher()
    sp = _subproc({"get": (0, "", ""), "set": (1, "", "boom")})
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="max")),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert _sent_severities(d) == [AlertSeverity.WARNING]
    assert "boom" in d.send.call_args.args[0].body


@pytest.mark.asyncio
async def test_incus_unreachable_is_no_signal(tmp_path):
    """config get rc!=0 → no set attempt, no alert (state machine's job)."""
    cfg = _Cfg(tmp_path)
    d = _dispatcher()
    sp = _subproc({"get": (1, "", "connection refused")})
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value=None)),
        patch.object(swap_watch, "activate_swap_max", AsyncMock()) as act,
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert [c[2] for c in sp.calls] == ["get"]
    act.assert_not_awaited()
    d.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_unreadable_cgroup_skips_live_half(tmp_path):
    """read_swap_max None (stopped container / cgroup v1) → no live write."""
    cfg = _Cfg(tmp_path)
    d = _dispatcher()
    sp = _subproc({"get": (0, "true", "")})
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value=None)),
        patch.object(swap_watch, "activate_swap_max", AsyncMock()) as act,
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    act.assert_not_awaited()
    d.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_kill_switch_disables_everything(tmp_path):
    cfg = _Cfg(tmp_path, enabled=False)
    d = _dispatcher()
    with (
        patch.object(swap_watch, "_run_subprocess", AsyncMock()) as sp,
        patch.object(swap_watch, "read_swap_max", AsyncMock()) as rd,
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    sp.assert_not_awaited()
    rd.assert_not_awaited()
    d.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_failure_never_raises(tmp_path):
    """A dead dispatcher must not break the reconcile (heal still happened)."""
    cfg = _Cfg(tmp_path)
    d = _dispatcher()
    d.send.side_effect = RuntimeError("transport down")
    sp = _subproc({"get": (0, "", ""), "set": (0, "", "")})
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="max")),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)  # no raise
    assert [c[2] for c in sp.calls] == ["get", "set"]


# ── cgroup primitives ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_swap_max_reads_via_sudo():
    async def fake(*cmd, timeout=None):
        assert cmd[:2] == ("sudo", "cat")
        assert cmd[2].endswith("lxc.payload.genesis/memory.swap.max")
        return (0, "0\n", "")

    with patch.object(cgroup_ops, "_run_subprocess", fake):
        assert await cgroup_ops.read_swap_max("genesis") == "0"


@pytest.mark.asyncio
async def test_read_swap_max_failure_is_none():
    with patch.object(
        cgroup_ops,
        "_run_subprocess",
        AsyncMock(return_value=(1, "", "denied")),
    ):
        assert await cgroup_ops.read_swap_max("genesis") is None


@pytest.mark.asyncio
async def test_activate_swap_max_writes_max_via_sudo():
    async def fake(*cmd, timeout=None):
        assert cmd[:3] == ("sudo", "sh", "-c")
        assert "echo max >" in cmd[3]
        assert "lxc.payload.genesis/memory.swap.max" in cmd[3]
        return (0, "", "")

    with patch.object(cgroup_ops, "_run_subprocess", fake):
        assert await cgroup_ops.activate_swap_max("genesis") is True


@pytest.mark.asyncio
async def test_activate_swap_max_failure_is_false():
    with patch.object(
        cgroup_ops,
        "_run_subprocess",
        AsyncMock(return_value=(1, "", "denied")),
    ):
        assert await cgroup_ops.activate_swap_max("genesis") is False


# ── wiring ─────────────────────────────────────────────────────────────────


def test_run_check_wires_the_watch():
    """The reconciler is dead unless run_check actually calls it."""
    from pathlib import Path

    import genesis.guardian.check as check_mod

    text = Path(check_mod.__file__).read_text()
    assert "await _check_container_swap_and_alert(config, dispatcher)" in text
    assert "from genesis.guardian.swap_watch import check_container_swap_and_alert" in text


@pytest.mark.asyncio
async def test_byte_valued_knob_is_not_reset_to_true(tmp_path):
    """A parseable Incus byte size (the native swap-ceiling form) is swap-on
    and must NOT be reset to ``true`` — doing so is a live Incus update that
    rewrites the cgroup to 0 (Incus 6.0 driver_lxc.go, confirmed from source:
    any ``limits.memory.swap`` value other than a parseable size or explicit
    false writes ``SetMemorySwapLimit(0)`` at apply time). Resetting it here
    would flip swap off every tick on an operator- or guardian-set ceiling."""
    cfg = _Cfg(tmp_path)
    d = _dispatcher()
    sp = _subproc({"get": (0, "10737418240\n", "")})
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="10737418240")),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert [c[2] for c in sp.calls] == ["get"], "a byte value must not trigger a 'set'"
    assert not d.send.called


@pytest.mark.asyncio
async def test_byte_valued_knob_with_suffix_is_not_reset(tmp_path):
    """Same, for a human-written size with a unit suffix ('8GiB')."""
    cfg = _Cfg(tmp_path)
    d = _dispatcher()
    sp = _subproc({"get": (0, "8GiB\n", "")})
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="max")),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert [c[2] for c in sp.calls] == ["get"]
    assert not d.send.called


@pytest.mark.asyncio
async def test_garbage_knob_value_is_still_healed(tmp_path):
    """A value that is neither a bool nor a parseable size is NOT swap-on and
    still gets reconciled to true (distinguishes 'not recognized as a size'
    from 'recognized as swap-on')."""
    cfg = _Cfg(tmp_path)
    d = _dispatcher()
    sp = _subproc({"get": (0, "garbage\n", ""), "set": (0, "", "")})
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="max")),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert [c[2] for c in sp.calls] == ["get", "set"]


@pytest.mark.asyncio
async def test_bare_zero_knob_is_still_reconciled(tmp_path):
    """'0' is NOT a 0-byte ceiling here — Incus's own IsFalse("0") claims it
    as a boolean before any byte-size parsing is attempted (shared/util/
    boolean.go), so it must be reconciled to 'true' exactly like 'false'.
    Misreading it as a byte size would silently defeat the guardian's
    deliberate-override policy for this one spelling of false."""
    cfg = _Cfg(tmp_path)
    d = _dispatcher()
    sp = _subproc({"get": (0, "0\n", ""), "set": (0, "", "")})
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="max")),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert [c[2] for c in sp.calls] == ["get", "set"]


@pytest.mark.asyncio
async def test_bare_one_knob_is_not_reset(tmp_path):
    """'1' is Incus's IsTrue("1") — treated exactly like 'true' (same
    SetMemorySwapLimit(0) branch), so there is nothing to reconcile."""
    cfg = _Cfg(tmp_path)
    d = _dispatcher()
    sp = _subproc({"get": (0, "1\n", "")})
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="max")),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert [c[2] for c in sp.calls] == ["get"]


@pytest.mark.asyncio
async def test_write_swap_max_rejects_non_integer_without_subprocess():
    """A value that is neither 'max' nor a plain decimal must never reach a
    subprocess — closed-set validation before use, not after."""
    called = AsyncMock()
    with patch.object(cgroup_ops, "_run_subprocess", called):
        for bad in ("１０", "1; rm -rf /", "-1", "10.5", "10GiB", "", " 10", "10 "):
            assert await cgroup_ops.write_swap_max("genesis", bad) is False
    called.assert_not_called()


@pytest.mark.asyncio
async def test_write_swap_max_writes_plain_integer():
    async def fake(*cmd, timeout=None):
        assert cmd[:3] == ("sudo", "sh", "-c")
        assert "echo 10737418240 >" in cmd[3]
        return (0, "", "")

    with patch.object(cgroup_ops, "_run_subprocess", fake):
        assert await cgroup_ops.write_swap_max("genesis", "10737418240") is True


# ── swap_ceiling_pct: pure helpers ──────────────────────────────────────────


class _CeilingCfg(_Cfg):
    def __init__(self, tmp_path, pct=None, enabled=True):
        super().__init__(tmp_path, enabled=enabled)
        self.swap_ceiling_pct = pct


def test_compute_target_none_when_unconfigured(tmp_path):
    cfg = _CeilingCfg(tmp_path, pct=None)
    assert swap_watch._compute_swap_ceiling_target(cfg) is None


def test_compute_target_none_when_host_swap_unreadable(tmp_path):
    cfg = _CeilingCfg(tmp_path, pct=50)
    with patch.object(swap_watch, "_host_swap_total_bytes", return_value=None):
        assert swap_watch._compute_swap_ceiling_target(cfg) is None


def test_compute_target_page_aligned_standard_page(tmp_path):
    cfg = _CeilingCfg(tmp_path, pct=50)
    swap_total = 20 * 1024**3  # 20 GiB
    with (
        patch.object(swap_watch, "_host_swap_total_bytes", return_value=swap_total),
        patch("os.sysconf", return_value=4096),
    ):
        target = swap_watch._compute_swap_ceiling_target(cfg)
    assert target == 10 * 1024**3
    assert target % 4096 == 0


def test_compute_target_floors_to_nonstandard_page_size(tmp_path):
    """A 64 KiB page host must not get a target the kernel would re-floor on
    readback (which would rewrite + re-alert every tick, S2)."""
    cfg = _CeilingCfg(tmp_path, pct=33)
    swap_total = 1_000_000_000  # not evenly divisible by 33% or by 64 KiB
    page = 65536
    with (
        patch.object(swap_watch, "_host_swap_total_bytes", return_value=swap_total),
        patch("os.sysconf", return_value=page),
    ):
        target = swap_watch._compute_swap_ceiling_target(cfg)
    assert target is not None
    assert target % page == 0
    assert target == (int(swap_total * 33 / 100) // page) * page


def test_compute_target_none_when_smaller_than_one_page(tmp_path, caplog):
    import logging

    cfg = _CeilingCfg(tmp_path, pct=0.0000001)
    with (
        patch.object(swap_watch, "_host_swap_total_bytes", return_value=1024),
        patch("os.sysconf", return_value=4096),
        caplog.at_level(logging.WARNING, logger="genesis.guardian.swap_watch"),
    ):
        assert swap_watch._compute_swap_ceiling_target(cfg) is None
    assert "swap_ceiling_pct" in caplog.text or "ceiling" in caplog.text


def test_cgroup_matches_target_none_current():
    assert swap_watch._cgroup_matches_target(None, 100) is False


def test_cgroup_matches_target_max_current():
    assert swap_watch._cgroup_matches_target("max", 100) is False


def test_cgroup_matches_target_equal():
    assert swap_watch._cgroup_matches_target("100", 100) is True


def test_cgroup_matches_target_unequal():
    assert swap_watch._cgroup_matches_target("50", 100) is False


def test_cgroup_matches_target_garbage_current():
    assert swap_watch._cgroup_matches_target("not-a-number", 100) is False


@pytest.mark.asyncio
async def test_limits_memory_is_set_true():
    async def fake(*cmd, timeout=None):
        assert cmd[-1] == "limits.memory"
        return (0, "36GiB\n", "")

    with patch.object(swap_watch, "_run_subprocess", fake):
        assert await swap_watch._limits_memory_is_set("genesis") is True


@pytest.mark.asyncio
async def test_limits_memory_is_set_false_when_empty():
    with patch.object(swap_watch, "_run_subprocess", AsyncMock(return_value=(0, "\n", ""))):
        assert await swap_watch._limits_memory_is_set("genesis") is False


@pytest.mark.asyncio
async def test_limits_memory_is_set_false_when_unreadable():
    with patch.object(swap_watch, "_run_subprocess", AsyncMock(return_value=(1, "", "err"))):
        assert await swap_watch._limits_memory_is_set("genesis") is False


@pytest.mark.asyncio
async def test_limits_memory_is_set_false_on_exception():
    with patch.object(swap_watch, "_run_subprocess", AsyncMock(side_effect=OSError("x"))):
        assert await swap_watch._limits_memory_is_set("genesis") is False


def test_parse_incus_bytes_numeric_values():
    assert swap_watch._parse_incus_bytes("100") == 100
    assert swap_watch._parse_incus_bytes("8GiB") == 8 * 1024**3
    assert swap_watch._parse_incus_bytes("2MB") == 2 * 1000**2
    assert swap_watch._parse_incus_bytes("") is None
    assert swap_watch._parse_incus_bytes("abc") is None
    assert swap_watch._parse_incus_bytes("5XB") is None  # unknown suffix


# ── swap_ceiling_pct: integration (ceiling helper mocked to a fixed value) ──

_CEILING = 10_000_000_000  # arbitrary fixed bytes; alignment is _compute's job


def _subproc_ceiling(swap_get, memory_get=(0, "36GiB\n", ""), swap_set=(0, "", "")):
    """Route by the queried config KEY, not just the subcommand — a ceiling
    tick issues a 'get' for both limits.memory.swap and limits.memory."""

    async def fake(*cmd, timeout=None):
        assert cmd[0] == "incus" and cmd[1] == "config"
        fake.calls.append(cmd)
        if cmd[2] == "get":
            assert cmd[3] == "--expanded"
            key = cmd[5]
            if key == "limits.memory.swap":
                return swap_get
            if key == "limits.memory":
                return memory_get
            raise AssertionError(f"unexpected get key {key!r}")
        if cmd[2] == "set":
            assert cmd[4] == "limits.memory.swap"
            return swap_set
        raise AssertionError(f"unexpected subcommand {cmd[2]!r}")

    fake.calls = []
    return fake


@pytest.mark.asyncio
async def test_ceiling_native_path_sets_byte_value(tmp_path):
    cfg = _CeilingCfg(tmp_path, pct=50)
    d = _dispatcher()
    sp = _subproc_ceiling(swap_get=(0, "true\n", ""))
    with (
        patch.object(swap_watch, "_compute_swap_ceiling_target", return_value=_CEILING),
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="max")),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    set_calls = [c for c in sp.calls if c[2] == "set"]
    assert len(set_calls) == 1
    assert set_calls[0][5] == str(_CEILING)
    assert _sent_severities(d) == [AlertSeverity.INFO]
    assert "native ceiling" in d.send.call_args.args[0].body


@pytest.mark.asyncio
async def test_ceiling_native_path_idempotent_when_already_set(tmp_path):
    cfg = _CeilingCfg(tmp_path, pct=50)
    d = _dispatcher()
    sp = _subproc_ceiling(swap_get=(0, f"{_CEILING}\n", ""))
    with (
        patch.object(swap_watch, "_compute_swap_ceiling_target", return_value=_CEILING),
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value=str(_CEILING))),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert [c[2] for c in sp.calls] == ["get", "get"]  # swap.swap + limits.memory, no 'set'
    assert not d.send.called


@pytest.mark.asyncio
async def test_ceiling_fallback_path_writes_cgroup_directly(tmp_path):
    """No limits.memory cap -> Incus's native path never runs; the
    reconciler must enforce the ceiling via a direct cgroup write."""
    cfg = _CeilingCfg(tmp_path, pct=50)
    d = _dispatcher()
    sp = _subproc_ceiling(swap_get=(0, "true\n", ""), memory_get=(0, "\n", ""))
    write_calls = []

    async def fake_write(container, value):
        write_calls.append((container, value))
        return True

    with (
        patch.object(swap_watch, "_compute_swap_ceiling_target", return_value=_CEILING),
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="max")),
        patch.object(swap_watch, "write_swap_max", fake_write),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert [c[2] for c in sp.calls] == ["get", "get"]  # no 'set' — native path not in use
    assert write_calls == [("genesis", str(_CEILING))]
    assert "cgroup fallback" in d.send.call_args.args[0].body


@pytest.mark.asyncio
async def test_ceiling_fallback_path_noop_when_already_matching(tmp_path):
    cfg = _CeilingCfg(tmp_path, pct=50)
    d = _dispatcher()
    sp = _subproc_ceiling(swap_get=(0, "true\n", ""), memory_get=(0, "\n", ""))
    with (
        patch.object(swap_watch, "_compute_swap_ceiling_target", return_value=_CEILING),
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value=str(_CEILING))),
        patch.object(swap_watch, "write_swap_max", AsyncMock()) as wsm,
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    wsm.assert_not_called()
    assert not d.send.called


@pytest.mark.asyncio
async def test_ceiling_zero_cgroup_writes_target_not_max(tmp_path):
    cfg = _CeilingCfg(tmp_path, pct=50)
    d = _dispatcher()
    sp = _subproc_ceiling(swap_get=(0, f"{_CEILING}\n", ""))
    with (
        patch.object(swap_watch, "_compute_swap_ceiling_target", return_value=_CEILING),
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="0")),
        patch.object(swap_watch, "write_swap_max", AsyncMock(return_value=True)) as wsm,
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    wsm.assert_awaited_once_with("genesis", str(_CEILING))
    assert f"0 → {_CEILING} bytes" in d.send.call_args.args[0].body


@pytest.mark.asyncio
async def test_ceiling_zero_cgroup_falls_back_to_max_on_failure(tmp_path):
    """A failed ceiling write at '0' must still bring swap on (uncapped)
    rather than leaving it off — swap-off is strictly worse (S4)."""
    cfg = _CeilingCfg(tmp_path, pct=50)
    d = _dispatcher()
    sp = _subproc_ceiling(swap_get=(0, f"{_CEILING}\n", ""))
    with (
        patch.object(swap_watch, "_compute_swap_ceiling_target", return_value=_CEILING),
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="0")),
        patch.object(swap_watch, "write_swap_max", AsyncMock(return_value=False)),
        patch.object(swap_watch, "activate_swap_max", AsyncMock(return_value=True)) as act,
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    act.assert_awaited_once_with("genesis")
    severities = _sent_severities(d)
    assert AlertSeverity.INFO in severities  # swap IS on (uncapped)
    assert AlertSeverity.WARNING in severities  # but the ceiling itself failed


@pytest.mark.asyncio
async def test_ceiling_and_swapoff_failures_throttle_independently(tmp_path):
    """The money test for the per-class throttle (S4): a ceiling-class
    failure must never mute a swap_off-class WARNING in the same 24h
    window, and vice versa."""
    cfg = _CeilingCfg(tmp_path, pct=50)
    d1 = _dispatcher()
    sp1 = _subproc_ceiling(swap_get=(0, f"{_CEILING}\n", ""))
    with (
        patch.object(swap_watch, "_compute_swap_ceiling_target", return_value=_CEILING),
        patch.object(swap_watch, "_run_subprocess", sp1),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="0")),
        patch.object(swap_watch, "write_swap_max", AsyncMock(return_value=False)),
        patch.object(swap_watch, "activate_swap_max", AsyncMock(return_value=False)),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d1)
    # Both the ceiling write AND the max fallback failed -> genuine swap_off.
    assert _sent_severities(d1) == [AlertSeverity.WARNING]

    # Immediately after, a DIFFERENT problem of the OTHER class must still
    # alert even though the swap_off throttle was just recorded.
    d2 = _dispatcher()
    sp2 = _subproc_ceiling(swap_get=(0, "true\n", ""), swap_set=(1, "", "denied"))
    with (
        patch.object(swap_watch, "_compute_swap_ceiling_target", return_value=_CEILING),
        patch.object(swap_watch, "_run_subprocess", sp2),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="max")),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d2)
    assert _sent_severities(d2) == [AlertSeverity.WARNING]
    assert "ceiling" in d2.send.call_args.args[0].title.lower()


@pytest.mark.asyncio
async def test_legacy_flat_state_file_throttles_swap_off_only(tmp_path):
    """A pre-upgrade state file (flat string, no per-class structure) must
    throttle the swap_off class and must NOT be misread as throttling a
    ceiling failure it never recorded."""
    state_file = tmp_path / "swap_watch_state.json"
    state_file.write_text(json.dumps({"last_failure_alert_at": datetime.now(UTC).isoformat()}))

    cfg = _CeilingCfg(tmp_path, pct=50)
    d = _dispatcher()
    sp = _subproc_ceiling(swap_get=(0, "true\n", ""), swap_set=(1, "", "denied"))
    with (
        patch.object(swap_watch, "_compute_swap_ceiling_target", return_value=_CEILING),
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="max")),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    # A ceiling failure right after a legacy swap_off throttle record must
    # still alert -- the legacy entry says nothing about the ceiling class.
    assert _sent_severities(d) == [AlertSeverity.WARNING]


@pytest.mark.asyncio
async def test_kill_switch_disables_ceiling_too(tmp_path):
    cfg = _CeilingCfg(tmp_path, pct=50, enabled=False)
    d = _dispatcher()
    with (
        patch.object(swap_watch, "_compute_swap_ceiling_target") as compute,
        patch.object(swap_watch, "_run_subprocess", AsyncMock()) as sp,
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    compute.assert_not_called()
    sp.assert_not_called()
    assert not d.send.called


# ── genesis-architect review fixes ──────────────────────────────────────────


def test_parse_incus_bytes_zero_valued_suffixed_forms():
    """A zero-valued suffixed size parses to 0, and _is_parseable_incus_size
    must reject it (zero bytes of swap IS swap-off)."""
    for zero_form in ("0B", "0 bytes", "00GiB", "0GiB", "0kB", "0MB", "0TiB", "0PB", "0EiB"):
        assert swap_watch._parse_incus_bytes(zero_form) == 0, zero_form
        assert swap_watch._is_parseable_incus_size(zero_form) is False, zero_form


def test_parse_incus_bytes_long_zero_padded_does_not_raise():
    """4,301 leading zeros + '1GiB' must parse to the correct value (1 GiB)
    without raising -- Incus tolerates arbitrary leading zeros; Python's
    int() does not past ~4,300 digits unless they're stripped first."""
    huge = ("0" * 4301) + "1GiB"
    assert swap_watch._parse_incus_bytes(huge) == 1024**3
    assert swap_watch._is_parseable_incus_size(huge) is True


def test_parse_incus_bytes_long_all_zero_does_not_raise():
    huge_zero = ("0" * 4301) + "GiB"
    assert swap_watch._parse_incus_bytes(huge_zero) == 0
    assert swap_watch._is_parseable_incus_size(huge_zero) is False


@pytest.mark.asyncio
async def test_zero_valued_ceiling_knob_is_still_reconciled(tmp_path):
    """Integration-level check that the fix actually reaches
    check_container_swap_and_alert: a zero-valued suffixed knob must still
    be healed to 'true', not mistaken for an already-set ceiling."""
    cfg = _Cfg(tmp_path)
    d = _dispatcher()
    sp = _subproc({"get": (0, "0GiB\n", ""), "set": (0, "", "")})
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="max")),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert [c[2] for c in sp.calls] == ["get", "set"]


@pytest.mark.asyncio
async def test_nonzero_value_with_leading_zero_digits_is_not_reset(tmp_path):
    """A leading-zero-padded but genuinely NONZERO value ('010GiB' = 10 GiB)
    must still be recognized as swap-on -- the fix targets an all-zero
    digit run, not any digit string containing a zero."""
    cfg = _Cfg(tmp_path)
    d = _dispatcher()
    sp = _subproc({"get": (0, "010GiB\n", "")})
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="max")),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert [c[2] for c in sp.calls] == ["get"]


@pytest.mark.asyncio
async def test_long_zero_padded_nonzero_size_does_not_raise(tmp_path):
    """4,301 leading zeros + '1GiB' is a legitimate (if perverse) 1 GiB
    ceiling Incus's own parser accepts with no overflow -- Python's
    int(raw) would raise ValueError past ~4,300 digits and must never be
    used for the nonzero check. Must be recognized as swap-on (not reset),
    and must not raise. (Second Codex finding on PR #3069.)"""
    cfg = _Cfg(tmp_path)
    d = _dispatcher()
    huge = ("0" * 4301) + "1GiB"
    sp = _subproc({"get": (0, f"{huge}\n", "")})
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="max")),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert [c[2] for c in sp.calls] == ["get"]


@pytest.mark.asyncio
async def test_long_zero_padded_all_zero_size_is_reconciled(tmp_path):
    """The all-zero counterpart of the above: a huge digit run that is
    genuinely all zeros must still be recognized as swap-off and healed,
    and must not raise."""
    cfg = _Cfg(tmp_path)
    d = _dispatcher()
    huge_zero = ("0" * 4301) + "GiB"
    sp = _subproc({"get": (0, f"{huge_zero}\n", ""), "set": (0, "", "")})
    with (
        patch.object(swap_watch, "_run_subprocess", sp),
        patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="max")),
    ):
        await swap_watch.check_container_swap_and_alert(cfg, d)
    assert [c[2] for c in sp.calls] == ["get", "set"]


def test_legacy_flat_timestamp_migrates_to_swap_off_class(tmp_path):
    """A legacy flat-format record is migrated into the dict under the
    swap_off class, not dropped, when a DIFFERENT class is recorded next."""
    state_file = tmp_path / "swap_watch_state.json"
    legacy_ts = datetime.now(UTC).isoformat()
    state_file.write_text(json.dumps({"last_failure_alert_at": legacy_ts}))

    swap_watch._record_failure_alert(state_file, datetime.now(UTC), swap_watch._PROBLEM_CLASS_CEILING)

    data = json.loads(state_file.read_text())
    by_class = data["last_failure_alert_at"]
    assert by_class[swap_watch._PROBLEM_CLASS_SWAP_OFF] == legacy_ts
    assert swap_watch._PROBLEM_CLASS_CEILING in by_class


def test_write_swap_max_rejects_octal_ambiguous_leading_zero():
    """'010' must be refused -- a base-0 numeric parser downstream could
    read it as octal 8 rather than decimal 10, silently applying the
    wrong byte value."""
    for bad in ("010", "007", "00"):
        assert cgroup_ops._SWAP_MAX_VALUE_RE.fullmatch(bad) is None, bad
    assert cgroup_ops._SWAP_MAX_VALUE_RE.fullmatch("0") is not None
    assert cgroup_ops._SWAP_MAX_VALUE_RE.fullmatch("10") is not None


# ── direct unit coverage of the classification functions ───────────────────
# (fresh-context class audit, 2026-10-08: coverage lived only behind the
# subprocess-mocked integration surface; these probe the pure functions
# directly, cheaper insurance against a future round finding a case the
# integration tests happen not to exercise.)

@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", False),
        ("   ", False),
        ("true", True),
        ("True", True),
        ("TRUE", True),
        ("tRuE", True),
        ("1", True),
        ("yes", True),
        ("YES", True),
        ("on", True),
        ("ON", True),
        ("false", False),
        ("False", False),
        ("0", False),
        ("no", False),
        ("NO", False),
        ("off", False),
        ("OFF", False),
        ("00", False),  # not a word-list member -> falls through to size parsing, all zeros
        ("01", True),   # not a word-list member -> size parsing, nonzero
        ("2", True),
        ("42", True),
        ("5GiB", True),
        ("0GiB", False),
        ("00GiB", False),
        ("5Gib", False),  # wrong case suffix -> unparseable -> not swap-on
        ("5XB", False),   # unknown suffix
        ("1.5GiB", False),  # decimal point -> not a digit run + suffix
        ("+5GiB", False),   # sign
        ("-5GiB", False),
        ("5 GiB", False),   # internal whitespace the grammar doesn't allow
        ("garbage", False),
        ("५GiB", False),    # Devanagari digit 5 -- not an ASCII digit
    ],
)
def test_is_swap_on_matrix(value, expected):
    assert swap_watch._is_swap_on(value) is expected, value
