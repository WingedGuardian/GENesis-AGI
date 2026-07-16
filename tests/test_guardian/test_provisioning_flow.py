"""Provisioning flow tests — gate refusal, approval gating, single execution,
post-approval re-check, ledger honesty, CONFLICT tolerance, propose damper."""

from __future__ import annotations

import pytest

import genesis.guardian.provisioning.flow as flow_mod
from genesis.guardian.alert.telegram import CONFLICT_SENTINEL, TelegramAlertChannel
from genesis.guardian.config import GuardianConfig
from genesis.guardian.provisioning.base import (
    BackupStartResult,
    BackupStatus,
    HostCapacity,
    ProvisionResult,
)
from genesis.guardian.provisioning.flow import (
    ProvisionRequest,
    execute_provisioning_action,
    execute_vzdump_start,
    maybe_propose_pool_grow,
    run_provisioning_flow,
    verify_vzdump_step,
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

    # ── backup verbs (vzdump slice) — knob-driven ──
    start_result = None      # BackupStartResult to return
    status_results = None    # list of BackupStatus, popped per probe
    prune_result = (True, "pruned to keep-last=2")

    async def vzdump_start(self):
        self.vzdump_start_calls = getattr(self, "vzdump_start_calls", 0) + 1
        return self.start_result

    async def vzdump_status(self, upid):
        self.status_upids = getattr(self, "status_upids", [])
        self.status_upids.append(upid)
        return self.status_results.pop(0)

    async def prune_backups(self):
        self.prune_calls = getattr(self, "prune_calls", 0) + 1
        return self.prune_result


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
    captured = {}

    async def _fake_expand(config, add_gib=None):
        captured["add_gib"] = add_gib
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
    assert captured["add_gib"] == 32  # absorb bounded to the approved grow amount
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


# ── anti-stack guard: unverified-but-landed disk grow must not double-grow ──
async def test_execute_core_skips_when_prior_unverified_grow_landed(tmp_path):
    """A prior grow recorded unverified whose target the live size now meets
    must NOT stack a second +NG — it is detected as landed and refused."""
    led = ProvisioningLedger(tmp_path)
    led.record_action("grow_vm_disk", "scsi1 +32G", ok=False, verified=False,
                      target_bytes=64 * _GIB)
    # Fresh capacity shows scsi1 already at 64G (the prior grow DID land).
    landed_cap = HostCapacity(
        detected=True, vm_memory_mib=21500, cores=5,
        disks={"scsi1": 64 * _GIB}, storage_free_bytes=574 * _GIB,
        node_mem_total_bytes=141 * _GIB, node_mem_available_bytes=65 * _GIB, detail="ok",
    )
    adapter = FakeAdapter(landed_cap,
                          disk_result=ProvisionResult(ok=True, action="grow_vm_disk", verified=True))
    res = await execute_provisioning_action(_cfg(), _disk_req(), adapter,
                                            FakeDispatcher(None), led)
    assert res["stage"] == "already_landed" and res["ok"] is True
    assert adapter.grow_disk_calls == 0            # no second grow issued
    assert led.latest_unverified_disk("scsi1") is None  # latch cleared


async def test_execute_core_proceeds_when_prior_grow_did_not_land(tmp_path):
    """If the prior unverified grow did NOT land (size still old), retrying to
    the same target is safe and proceeds."""
    led = ProvisioningLedger(tmp_path)
    led.record_action("grow_vm_disk", "scsi1 +32G", ok=False, verified=False,
                      target_bytes=64 * _GIB)
    # scsi1 still at 32G — the prior grow truly did not land.
    adapter = FakeAdapter(_good_cap(),
                          disk_result=ProvisionResult(ok=True, action="grow_vm_disk",
                                                      verified=True, target_bytes=64 * _GIB))
    res = await execute_provisioning_action(_cfg(), _disk_req(), adapter,
                                            FakeDispatcher(None), led)
    assert res["stage"] == "executed"
    assert adapter.grow_disk_calls == 1            # retry proceeds


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


# ── two-phase vzdump flow (start ledgers immediately; verify flips) ───────

_UPID = "UPID:pve:000A1B2C:001122DD:68765432:vzdump:100:user@pve!backup:"


def _backup_cap(**kw):
    defaults = dict(
        detected=True, vm_memory_mib=21500, cores=5,
        disks={"scsi0": 32 * _GIB, "scsi1": 64 * _GIB},
        storage_free_bytes=574 * _GIB,
        node_mem_total_bytes=141 * _GIB, node_mem_available_bytes=65 * _GIB,
        backup_storage_free_bytes=150 * _GIB,
        backup_storage_total_bytes=500 * _GIB,
        vm_agent_enabled=False, detail="ok",
    )
    defaults.update(kw)
    return HostCapacity(**defaults)


async def test_vzdump_start_ledgers_at_start_even_before_any_verify(tmp_path):
    """B1: the start row is written the moment the POST is accepted — the
    rate-cap entry, in-flight latch, and restart-resume handle in one row."""
    adapter = FakeAdapter(_backup_cap())
    adapter.start_result = BackupStartResult(
        ok=True, upid=_UPID, requested="vzdump vmid 100 -> backup", attempted=True,
    )
    disp = FakeDispatcher()
    ledger = ProvisioningLedger(tmp_path)
    res = await execute_vzdump_start(_cfg(), adapter, disp, ledger)
    assert res["ok"] and res["stage"] == "started" and res["upid"] == _UPID
    entry = ledger.latest_backup()
    assert entry is not None and entry["upid"] == _UPID
    assert entry["verified"] is False
    assert ledger.actions_in_window(action_prefix="vzdump") == 1
    assert disp.alerts == [], "a clean start is not an alert"


async def test_vzdump_start_gate_refusal_writes_nothing(tmp_path):
    adapter = FakeAdapter(_backup_cap(backup_storage_free_bytes=None))
    disp = FakeDispatcher()
    ledger = ProvisioningLedger(tmp_path)
    res = await execute_vzdump_start(_cfg(), adapter, disp, ledger)
    assert not res["ok"] and res["stage"] == "recheck_failed"
    assert "backup storage headroom" in res["failed_checks"]
    assert ledger.latest_backup() is None, "refusals are never ledgered"
    assert getattr(adapter, "vzdump_start_calls", 0) == 0


async def test_vzdump_start_failed_post_is_ledgered_but_never_latches(tmp_path):
    """A sent-but-failed POST counts against the cap (conservative) but a
    upid-less row must not latch — there is nothing to resume."""
    adapter = FakeAdapter(_backup_cap())
    adapter.start_result = BackupStartResult(
        ok=False, requested="vzdump vmid 100 -> backup", attempted=True,
        error="HTTP 403: denied",
    )
    disp = FakeDispatcher()
    ledger = ProvisioningLedger(tmp_path)
    res = await execute_vzdump_start(_cfg(), adapter, disp, ledger)
    assert not res["ok"] and res["stage"] == "start_failed"
    assert ledger.actions_in_window(action_prefix="vzdump") == 1
    assert _sev(disp.alerts) == ["critical"]
    # and the latch is NOT set: a second start passes the in-flight check
    adapter2 = FakeAdapter(_backup_cap())
    adapter2.start_result = BackupStartResult(
        ok=True, upid=_UPID, requested="x", attempted=True,
    )
    res2 = await execute_vzdump_start(_cfg(max_backups_per_week=5), adapter2, disp, ledger)
    assert res2["ok"], f"upid-less failure must not latch: {res2}"


async def test_vzdump_second_start_refused_while_latched(tmp_path):
    adapter = FakeAdapter(_backup_cap())
    adapter.start_result = BackupStartResult(
        ok=True, upid=_UPID, requested="x", attempted=True,
    )
    disp = FakeDispatcher()
    ledger = ProvisioningLedger(tmp_path)
    assert (await execute_vzdump_start(_cfg(max_backups_per_week=5), adapter, disp, ledger))["ok"]
    res2 = await execute_vzdump_start(_cfg(max_backups_per_week=5), adapter, disp, ledger)
    assert not res2["ok"] and "no backup in flight" in res2["failed_checks"]
    assert getattr(adapter, "vzdump_start_calls", 0) == 1


async def test_verify_no_arg_resumes_latest_inflight_and_flips_prunes(tmp_path):
    """S1: restart-safe resume — a fresh process verifies the ledger's UPID."""
    adapter = FakeAdapter(_backup_cap())
    adapter.status_results = [BackupStatus(
        state="verified", volid="backup:backup/vzdump-qemu-100-x.vma.zst",
        age_days=0.01, detail="task OK; new backup visible",
    )]
    disp = FakeDispatcher()
    ledger = ProvisioningLedger(tmp_path)
    ledger.record_action("vzdump", "vmid 100", ok=True, verified=False, upid=_UPID)
    res = await verify_vzdump_step(_cfg(), adapter, disp, ledger)  # NO upid arg
    assert res["ok"] and res["state"] == "verified"
    assert adapter.status_upids == [_UPID], "must probe the ledger's own UPID"
    assert ledger.latest_backup()["verified"] is True
    assert getattr(adapter, "prune_calls", 0) == 1, "rotation runs on verify"
    assert _sev(disp.alerts) == ["info"]


async def test_verify_terminal_failure_flips_and_alerts_critical(tmp_path):
    adapter = FakeAdapter(_backup_cap())
    adapter.status_results = [BackupStatus(state="failed", detail="task exitstatus: job errors")]
    disp = FakeDispatcher()
    ledger = ProvisioningLedger(tmp_path)
    ledger.record_action("vzdump", "vmid 100", ok=True, verified=False, upid=_UPID)
    res = await verify_vzdump_step(_cfg(), adapter, disp, ledger, upid=_UPID)
    assert not res["ok"] and res["state"] == "failed"
    entry = ledger.latest_backup()
    assert entry["ok"] is False, "terminal failure is recorded"
    assert getattr(adapter, "prune_calls", 0) == 0, "never rotate on failure"
    assert _sev(disp.alerts) == ["critical"]


async def test_verify_transient_states_change_nothing(tmp_path):
    """S7: running/unknown are TRANSIENT — no ledger flip, no alert, latch intact."""
    adapter = FakeAdapter(_backup_cap())
    adapter.status_results = [
        BackupStatus(state="running", detail="task still running"),
        BackupStatus(state="unknown", detail="status read 500"),
    ]
    disp = FakeDispatcher()
    ledger = ProvisioningLedger(tmp_path)
    ledger.record_action("vzdump", "vmid 100", ok=True, verified=False, upid=_UPID)
    for expected in ("running", "unknown"):
        res = await verify_vzdump_step(_cfg(), adapter, disp, ledger, upid=_UPID)
        assert res["ok"] and res["state"] == expected
    assert ledger.latest_backup()["verified"] is False
    assert disp.alerts == []


async def test_verify_with_nothing_in_flight_errors(tmp_path):
    adapter = FakeAdapter(_backup_cap())
    disp = FakeDispatcher()
    res = await verify_vzdump_step(_cfg(), adapter, disp, ProvisioningLedger(tmp_path))
    assert not res["ok"] and res["stage"] == "no_backup_in_flight"


async def test_grow_rate_cap_ignores_backups(tmp_path):
    """N3/the collision the due-diligence round found: a backup must not
    consume the grow budget."""
    ledger = ProvisioningLedger(tmp_path)
    ledger.record_action("vzdump", "vmid 100", ok=True, verified=True, upid=_UPID)
    ledger.record_action("vzdump", "vmid 100 again", ok=True, verified=True,
                         upid=_UPID.replace("68765432", "68765433"))
    adapter = FakeAdapter(
        _good_cap(),
        disk_result=ProvisionResult(
            ok=True, action="grow_vm_disk", requested="scsi1 +32G",
            before="32.0G", after="64.0G", verified=True,
        ),
    )
    disp = FakeDispatcher()
    res = await execute_provisioning_action(
        _cfg(max_actions_per_week=2), _disk_req(), adapter, disp, ledger,
    )
    assert res["ok"], f"two prior backups must not trip the 2-grow cap: {res}"


# ── P2 fixes: failed backup releases the latch; terminal state is usable ───

async def test_failed_backup_releases_in_flight_latch(tmp_path):
    """Codex P2: after a terminally-failed backup the task is OVER — the row
    stays verified:false but carries resolved_ts, and must NOT keep latching
    (which would block a retry/grow chain for up to vzdump_timeout_s)."""
    from genesis.guardian.provisioning.flow import _vzdump_in_flight_upid
    ledger = ProvisioningLedger(tmp_path)
    ledger.record_action("vzdump", "vmid 100", ok=True, verified=False, upid=_UPID)
    assert _vzdump_in_flight_upid(_cfg(), ledger) == _UPID  # in flight while running
    ledger.mark_latest_backup_verified(_UPID, ok=False)      # terminal failure
    assert _vzdump_in_flight_upid(_cfg(), ledger) == "", (
        "a resolved (failed) backup must not keep latching"
    )


async def test_new_backup_allowed_after_failure(tmp_path):
    """End-to-end of the latch fix: a fresh start passes the in-flight check
    once the prior backup has terminally failed."""
    ledger = ProvisioningLedger(tmp_path)
    ledger.record_action("vzdump", "vmid 100", ok=True, verified=False, upid=_UPID)
    ledger.mark_latest_backup_verified(_UPID, ok=False)
    adapter = FakeAdapter(_backup_cap())
    adapter.start_result = BackupStartResult(
        ok=True, upid=_UPID.replace("68765432", "68765499"), requested="x", attempted=True,
    )
    res = await execute_vzdump_start(
        _cfg(max_backups_per_week=5), adapter, FakeDispatcher(), ledger,
    )
    assert res["ok"], f"a fresh backup must be allowed after a failed one: {res}"
