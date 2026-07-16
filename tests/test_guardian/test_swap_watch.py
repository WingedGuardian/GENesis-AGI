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
