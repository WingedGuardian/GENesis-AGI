"""Provisioning flow tests — gate refusal, approval gating, single execution,
post-approval re-check, ledger honesty, CONFLICT tolerance, propose damper."""

from __future__ import annotations

import pytest

import genesis.guardian.provisioning.flow as flow_mod
from genesis.guardian.alert.telegram import CONFLICT_SENTINEL, TelegramAlertChannel
from genesis.guardian.config import GuardianConfig
from genesis.guardian.provisioning.base import HostCapacity, ProvisionResult
from genesis.guardian.provisioning.flow import (
    ProvisionRequest,
    execute_provisioning_action,
    maybe_propose_pool_grow,
    run_provisioning_flow,
)
from genesis.guardian.provisioning.ledger import ProvisioningLedger

_GIB = 1024**3


class FakeChannel(TelegramAlertChannel):
    def __init__(self, replies):
        super().__init__(bot_token="x", chat_id="y")
        self.sent: list[str] = []
        self._replies = list(replies)

    async def send_text(self, text):
        self.sent.append(text)
        return 111

    async def poll_for_keyword(self, gate_msg_id, keywords, timeout_s=25):
        return self._replies.pop(0) if self._replies else None


class FakeDispatcher:
    def __init__(self, channel=None):
        self._channels = [channel] if channel else []
        self.alerts = []

    async def send(self, alert):
        self.alerts.append(alert)
        return True


class FakeAdapter:
    def __init__(self, cap, *, disk_result=None, mem_result=None, backup_age=1.0, cap2=None):
        self._cap, self._cap2 = cap, (cap2 or cap)
        self._disk_result, self._mem_result = disk_result, mem_result
        self._backup = backup_age
        self.grow_disk_calls = self.grow_mem_calls = 0
        self._cap_calls = 0

    async def get_capacity(self):
        self._cap_calls += 1
        return self._cap if self._cap_calls == 1 else self._cap2

    async def grow_vm_disk(self, disk, add_gib):
        self.grow_disk_calls += 1
        return self._disk_result

    async def grow_vm_memory(self, new_mib):
        self.grow_mem_calls += 1
        return self._mem_result

    async def test_connectivity(self):
        return True

    async def newest_backup_age_days(self):
        return self._backup


def _cfg(**prov):
    c = GuardianConfig()
    c.provisioning.enabled = True
    for k, v in prov.items():
        setattr(c.provisioning, k, v)
    return c


def _good_cap():
    return HostCapacity(
        detected=True, vm_memory_mib=21500, cores=5,
        disks={"scsi1": 32 * _GIB}, storage_free_bytes=574 * _GIB,
        node_mem_total_bytes=141 * _GIB, node_mem_available_bytes=65 * _GIB, detail="ok",
    )


def _disk_req(**kw):
    return ProvisionRequest(kind="disk", disk="scsi1", add_gib=32, **kw)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _instant(*_a, **_k):
        return None
    monkeypatch.setattr(flow_mod.asyncio, "sleep", _instant)


def _sev(alerts):
    return [a.severity.value for a in alerts]


# ── gate refusal / no channel ─────────────────────────────────────────────
async def test_gate_fail_never_proposes():
    cap = HostCapacity(detected=False, detail="unreachable")
    ch = FakeChannel(["APPROVE"])
    disp = FakeDispatcher(ch)
    adapter = FakeAdapter(cap)
    res = await run_provisioning_flow(_cfg(), _disk_req(), adapter, disp, ProvisioningLedger("/tmp/x_nonexist"))
    assert res["stage"] == "refused_gate"
    assert ch.sent == []  # never proposed
    assert adapter.grow_disk_calls == 0


async def test_no_channel_refuses(tmp_path):
    disp = FakeDispatcher(None)  # no telegram channel
    adapter = FakeAdapter(_good_cap())
    res = await run_provisioning_flow(_cfg(), _disk_req(), adapter, disp, ProvisioningLedger(tmp_path))
    assert res["stage"] == "no_channel"
    assert adapter.grow_disk_calls == 0


# ── deny / timeout ─────────────────────────────────────────────────────────
async def test_deny_no_mutation(tmp_path):
    ch = FakeChannel(["DENY"])
    disp = FakeDispatcher(ch)
    adapter = FakeAdapter(_good_cap())
    res = await run_provisioning_flow(_cfg(), _disk_req(), adapter, disp, ProvisioningLedger(tmp_path))
    assert res["stage"] == "denied"
    assert adapter.grow_disk_calls == 0
    assert ch.sent, "proposal was sent"


async def test_timeout_no_mutation(tmp_path):
    ch = FakeChannel([])  # no reply ever
    disp = FakeDispatcher(ch)
    adapter = FakeAdapter(_good_cap())
    res = await run_provisioning_flow(_cfg(approval_timeout_s=0), _disk_req(), adapter, disp, ProvisioningLedger(tmp_path))
    assert res["stage"] == "timeout"
    assert adapter.grow_disk_calls == 0


# ── approve happy path ─────────────────────────────────────────────────────
async def test_approve_executes_ledgers_and_expands(tmp_path, monkeypatch):
    async def _fake_expand(config):
        return {"ok": True, "vg_free_bytes": 32 * _GIB}
    monkeypatch.setattr(flow_mod, "expand_storage", _fake_expand)

    ch = FakeChannel(["APPROVE"])
    disp = FakeDispatcher(ch)
    result = ProvisionResult(ok=True, action="grow_vm_disk", requested="scsi1 +32G",
                             before="32.0G", after="64.0G", verified=True)
    adapter = FakeAdapter(_good_cap(), disk_result=result)
    led = ProvisioningLedger(tmp_path)
    res = await run_provisioning_flow(_cfg(), _disk_req(absorb_after=True), adapter, disp, led)

    assert res["stage"] == "executed" and res["ok"] is True
    assert adapter.grow_disk_calls == 1  # exactly once
    assert led.actions_in_window() == 1
    assert res["expand"]["ok"] is True
    assert "critical" not in _sev(disp.alerts)


async def test_approve_recheck_fail_aborts(tmp_path):
    ch = FakeChannel(["APPROVE"])
    disp = FakeDispatcher(ch)
    adapter = FakeAdapter(
        _good_cap(),
        cap2=HostCapacity(detected=False, detail="vanished after approval"),
        disk_result=ProvisionResult(ok=True, action="grow_vm_disk", verified=True),
    )
    res = await run_provisioning_flow(_cfg(), _disk_req(), adapter, disp, ProvisioningLedger(tmp_path))
    assert res["stage"] == "recheck_failed"
    assert adapter.grow_disk_calls == 0  # never executed after re-check failed


async def test_unverified_still_ledgers_and_criticals(tmp_path):
    ch = FakeChannel(["APPROVE"])
    disp = FakeDispatcher(ch)
    result = ProvisionResult(ok=False, action="grow_vm_disk", requested="scsi1 +32G",
                             verified=False, error="re-read did not confirm")
    adapter = FakeAdapter(_good_cap(), disk_result=result)
    led = ProvisioningLedger(tmp_path)
    res = await run_provisioning_flow(_cfg(), _disk_req(), adapter, disp, led)
    assert res["ok"] is False and res["stage"] == "executed"
    assert led.actions_in_window() == 1  # unverified mutation still counts
    assert "critical" in _sev(disp.alerts)


async def test_conflict_tolerated_then_approve(tmp_path):
    ch = FakeChannel([CONFLICT_SENTINEL, "APPROVE"])
    disp = FakeDispatcher(ch)
    result = ProvisionResult(ok=True, action="grow_vm_disk", verified=True)
    adapter = FakeAdapter(_good_cap(), disk_result=result)
    res = await run_provisioning_flow(_cfg(), _disk_req(), adapter, disp, ProvisioningLedger(tmp_path))
    assert res["stage"] == "executed" and res["ok"] is True


async def test_memory_grow_reboot_notice(tmp_path):
    ch = FakeChannel(["APPROVE"])
    disp = FakeDispatcher(ch)
    result = ProvisionResult(ok=True, action="grow_vm_memory", requested="24576MiB",
                             before="21500MiB", after="24576MiB", verified=True,
                             requires_reboot=True)
    adapter = FakeAdapter(_good_cap(), mem_result=result)
    req = ProvisionRequest(kind="memory", new_mib=24576)
    res = await run_provisioning_flow(_cfg(), req, adapter, disp, ProvisioningLedger(tmp_path))
    assert res["ok"] is True and res["requires_reboot"] is True
    assert adapter.grow_mem_calls == 1


# ── execute-only core (the container-approved / Genesis-UP path) ───────────
async def test_execute_core_needs_no_channel_and_executes(tmp_path):
    """execute_provisioning_action runs WITHOUT any Telegram gate — the
    container already approved via its own bot. No channel on the dispatcher."""
    disp = FakeDispatcher(None)  # deliberately no Telegram channel
    result = ProvisionResult(ok=True, action="grow_vm_disk", requested="scsi1 +32G",
                             before="32.0G", after="64.0G", verified=True)
    adapter = FakeAdapter(_good_cap(), disk_result=result)
    led = ProvisioningLedger(tmp_path)
    res = await execute_provisioning_action(_cfg(), _disk_req(), adapter, disp, led)
    assert res["stage"] == "executed" and res["ok"] is True
    assert adapter.grow_disk_calls == 1
    assert adapter._cap_calls == 1  # single fresh re-check, no separate propose read
    assert led.actions_in_window() == 1
    assert "critical" not in _sev(disp.alerts)


async def test_execute_core_recheck_fail_aborts(tmp_path):
    disp = FakeDispatcher(None)
    adapter = FakeAdapter(HostCapacity(detected=False, detail="vanished"))
    res = await execute_provisioning_action(_cfg(), _disk_req(), adapter, disp,
                                            ProvisioningLedger(tmp_path))
    assert res["stage"] == "recheck_failed"
    assert adapter.grow_disk_calls == 0


async def test_execute_core_enforces_rate_cap_independently(tmp_path):
    """Even with container approval, the HOST execute path re-enforces the rate
    cap — a fresh gate at execute time, all-or-nothing."""
    led = ProvisioningLedger(tmp_path)
    led.record_action("grow_vm_disk", "scsi1 +32G", True, True)
    led.record_action("grow_vm_disk", "scsi1 +32G", True, True)  # == max_actions_per_week
    disp = FakeDispatcher(None)
    adapter = FakeAdapter(_good_cap(),
                          disk_result=ProvisionResult(ok=True, action="grow_vm_disk", verified=True))
    res = await execute_provisioning_action(_cfg(max_actions_per_week=2), _disk_req(),
                                            adapter, disp, led)
    assert res["stage"] == "recheck_failed"
    assert adapter.grow_disk_calls == 0  # rate cap blocked before mutation


async def test_execute_core_unverified_criticals_and_ledgers(tmp_path):
    disp = FakeDispatcher(None)
    result = ProvisionResult(ok=False, action="grow_vm_disk", requested="scsi1 +32G",
                             verified=False, error="re-read did not confirm")
    adapter = FakeAdapter(_good_cap(), disk_result=result)
    led = ProvisioningLedger(tmp_path)
    res = await execute_provisioning_action(_cfg(), _disk_req(), adapter, disp, led)
    assert res["ok"] is False and res["stage"] == "executed"
    assert led.actions_in_window() == 1
    assert "critical" in _sev(disp.alerts)


# ── autonomous propose damper ──────────────────────────────────────────────
async def test_propose_damped_when_recent(tmp_path):
    led = ProvisioningLedger(tmp_path)
    led.mark_proposed("pool_grow")
    adapter = FakeAdapter(_good_cap())
    disp = FakeDispatcher(FakeChannel([]))
    res = await maybe_propose_pool_grow(_cfg(min_repropose_hours=24), adapter, disp, led)
    assert res is None  # damped
    assert adapter.grow_disk_calls == 0


async def test_propose_fires_when_stale(tmp_path):
    led = ProvisioningLedger(tmp_path)
    ch = FakeChannel(["DENY"])  # propose reaches approval, user denies
    disp = FakeDispatcher(ch)
    adapter = FakeAdapter(_good_cap())
    res = await maybe_propose_pool_grow(_cfg(), adapter, disp, led)
    assert res is not None and res["stage"] == "denied"
    assert ch.sent, "proposal was sent"
    assert led.hours_since_last_proposal("pool_grow") is not None  # marked
