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
async def test_zero_valued_suffixed_sizes_are_still_reconciled(tmp_path):
    """A zero-valued byte-size ceiling parses structurally but represents
    zero bytes of swap -- Incus applies it literally and disables swap,
    exactly like the boolean FALSE spellings. Every zero-valued suffix form
    must still heal to 'true', not be mistaken for an already-on ceiling.
    (Codex finding on PR #3069.)"""
    for zero_form in ("0B", "0 bytes", "00GiB", "0GiB", "0kB", "0MB", "0TiB", "0PB", "0EiB"):
        cfg = _Cfg(tmp_path)
        d = _dispatcher()
        sp = _subproc({"get": (0, f"{zero_form}\n", ""), "set": (0, "", "")})
        with (
            patch.object(swap_watch, "_run_subprocess", sp),
            patch.object(swap_watch, "read_swap_max", AsyncMock(return_value="max")),
        ):
            await swap_watch.check_container_swap_and_alert(cfg, d)
        assert [c[2] for c in sp.calls] == ["get", "set"], (
            f"{zero_form!r} must be reconciled (it is zero bytes of swap), not left alone"
        )


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
