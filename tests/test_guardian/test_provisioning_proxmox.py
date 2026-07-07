"""ProxmoxAdapter tests — built on response shapes captured live from PVE 9.1.4.

Transport is injected by replacing the adapter's ``_request_sync`` with a fake
that records (method, path, params, token) so we can assert token discipline
(audit on reads, provision on writes) and exact request shapes without a network.
"""

from __future__ import annotations

import re
import urllib.parse

import pytest

from genesis.guardian.config import ProvisioningConfig
from genesis.guardian.provisioning import proxmox as proxmox_mod
from genesis.guardian.provisioning.proxmox import (
    ProxmoxAdapter,
    _parse_size_to_bytes,
)

_GIB = 1024**3

# PVE 9.x API response shapes (field names/nesting taken from a live probe;
# all identifiers below are synthetic).
_LIVE_STATUS = {
    "memory": {
        "free": 1658970112,
        "used": 82246012928,
        "total": 152036208640,
        "available": 69790195712,
    },
    "cpuinfo": {"cpus": 40},
}
_LIVE_STORAGE = [
    {"storage": "local", "total": 100861726720, "avail": 60265934848, "used": 1},
    {
        "storage": "local-lvm",
        "total": 1636273487872,
        "avail": 616384222882,
        "used": 1019889264990,
    },
]
_LIVE_CONFIG = {
    "name": "genesis-vm",
    "cores": 5,
    "memory": "21500",
    "scsi0": "local-lvm:vm-100-disk-0,cache=writeback,iothread=1,size=32G",
    "scsi1": "local-lvm:vm-100-disk-2,cache=writeback,iothread=1,size=32G",
    "ide2": "local:iso/installer.iso,media=cdrom,size=1000M",
}


class FakePVE:
    """Stand-in for ProxmoxAdapter._request_sync (called without adapter self)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None, str]] = []
        self.status = dict(_LIVE_STATUS)
        self.storage = [dict(s) for s in _LIVE_STORAGE]
        self.config = dict(_LIVE_CONFIG)
        self.pending: list = []
        self.put_resize_status = 200
        self.put_config_status = 200
        self.apply_writes = True  # False → PUT returns 200 but config unchanged
        self.transport_dead = False  # True → every call is a -1 transport error
        # Async-task (UPID) knobs — default None keeps the old synchronous shape.
        self.resize_upid = None  # str → resize PUT returns this UPID in `data`
        self.task_exitstatus = "OK"  # exitstatus once the task reports 'stopped'
        self.task_running_polls = 0  # 'running' responses before 'stopped'
        self.task_never_stops = False  # True → always 'running' (poll timeout)
        self.task_status_code = 200  # non-200 → task-status read failure
        self._task_polls = 0

    def __call__(self, method, path, params=None, token=""):
        self.calls.append((method, path, params, token))
        if self.transport_dead:
            return -1, None, "connection refused"
        if method == "GET" and "/tasks/" in path and path.endswith("/status"):
            if self.task_status_code != 200:
                return self.task_status_code, None, f"HTTP {self.task_status_code}: nope"
            self._task_polls += 1
            if self.task_never_stops or self._task_polls <= self.task_running_polls:
                return 200, {"status": "running"}, ""
            return 200, {"status": "stopped", "exitstatus": self.task_exitstatus}, ""
        if method == "GET" and path.endswith("/status"):
            return 200, self.status, ""
        if method == "GET" and path.endswith("/storage"):
            return 200, self.storage, ""
        if method == "GET" and path.endswith("/config"):
            return 200, dict(self.config), ""
        if method == "GET" and path.endswith("/pending"):
            return 200, self.pending, ""
        if method == "PUT" and path.endswith("/resize"):
            if self.put_resize_status != 200:
                return self.put_resize_status, None, f"HTTP {self.put_resize_status}: denied"
            if self.apply_writes and params:
                disk = params["disk"]
                add = int(params["size"].lstrip("+").rstrip("G"))
                cur = self.config[disk]
                m = re.search(r"size=(\d+)G", cur)
                new = int(m.group(1)) + add
                self.config[disk] = re.sub(r"size=\d+G", f"size={new}G", cur)
            return 200, self.resize_upid, ""
        if method == "PUT" and path.endswith("/config"):
            if self.put_config_status != 200:
                return self.put_config_status, None, f"HTTP {self.put_config_status}: denied"
            if self.apply_writes and params and "memory" in params:
                self.config["memory"] = str(params["memory"])
            return 200, None, ""
        return 404, None, "not found"


def _adapter(fake: FakePVE) -> ProxmoxAdapter:
    cfg = ProvisioningConfig(
        enabled=True, api_host="10.0.0.9", api_port=8006, verify_tls=False,
        node="pve", vmid=100, target_disk="scsi1", storage="local-lvm",
    )
    a = ProxmoxAdapter(cfg, audit_token="AUDIT", provision_token="PROVISION")
    a._request_sync = fake  # type: ignore[assignment]
    return a


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _instant(*_a, **_k):
        return None
    monkeypatch.setattr(proxmox_mod.asyncio, "sleep", _instant)


# ── size parsing ─────────────────────────────────────────────────────────
def test_parse_size_units():
    assert _parse_size_to_bytes("32G") == 32 * _GIB
    assert _parse_size_to_bytes("1000M") == 1000 * 1024**2
    assert _parse_size_to_bytes("512K") == 512 * 1024
    assert _parse_size_to_bytes("bogus") is None


# ── capacity ─────────────────────────────────────────────────────────────
async def test_get_capacity_parses_live_shapes():
    fake = FakePVE()
    cap = await _adapter(fake).get_capacity()
    assert cap.detected is True
    assert cap.vm_memory_mib == 21500
    assert cap.cores == 5
    assert cap.disks == {"scsi0": 32 * _GIB, "scsi1": 32 * _GIB}  # cdrom excluded
    assert cap.storage_free_bytes == 616384222882
    assert cap.storage_total_bytes == 1636273487872


async def test_capacity_uses_available_not_free():
    """Regression on the DD finding: headroom must be .memory.available."""
    fake = FakePVE()
    cap = await _adapter(fake).get_capacity()
    assert cap.node_mem_available_bytes == 69790195712  # available
    assert cap.node_mem_available_bytes != _LIVE_STATUS["memory"]["free"]  # NOT free
    assert cap.node_mem_total_bytes == 152036208640


async def test_reads_use_audit_token():
    fake = FakePVE()
    await _adapter(fake).get_capacity()
    assert fake.calls, "expected reads"
    assert all(tok == "AUDIT" for (_m, _p, _pa, tok) in fake.calls)


async def test_capacity_unconfigured_is_undetected():
    cfg = ProvisioningConfig(enabled=True, api_host="", node="", vmid=0)
    a = ProxmoxAdapter(cfg, "A", "P")
    cap = await a.get_capacity()
    assert cap.detected is False


# ── grow disk ────────────────────────────────────────────────────────────
async def test_grow_disk_single_put_provision_token_verified():
    fake = FakePVE()
    res = await _adapter(fake).grow_vm_disk("scsi1", 32)
    puts = [c for c in fake.calls if c[0] == "PUT"]
    assert len(puts) == 1, "exactly one mutating PUT"
    method, path, params, token = puts[0]
    assert path.endswith("/resize")
    assert params == {"disk": "scsi1", "size": "+32G"}
    assert token == "PROVISION"
    assert res.ok is True and res.verified is True
    assert res.after == "64.0G"


async def test_grow_disk_shrink_or_zero_issues_no_put():
    fake = FakePVE()
    res = await _adapter(fake).grow_vm_disk("scsi1", 0)
    assert res.ok is False
    assert not [c for c in fake.calls if c[0] == "PUT"]


async def test_grow_disk_missing_disk_no_put():
    fake = FakePVE()
    res = await _adapter(fake).grow_vm_disk("scsi9", 32)
    assert res.ok is False and "not found" in res.error
    assert not [c for c in fake.calls if c[0] == "PUT"]


async def test_grow_disk_unverified_when_config_does_not_change():
    fake = FakePVE()
    fake.apply_writes = False  # PUT 200 but size never changes
    res = await _adapter(fake).grow_vm_disk("scsi1", 32)
    assert res.ok is False and res.verified is False
    assert "did not confirm" in res.error
    # exactly one PUT even though verification failed — never re-issue
    assert len([c for c in fake.calls if c[0] == "PUT"]) == 1


async def test_grow_disk_403_structured_failure():
    fake = FakePVE()
    fake.put_resize_status = 403
    res = await _adapter(fake).grow_vm_disk("scsi1", 32)
    assert res.ok is False and "403" in res.error


# ── grow disk: async task (UPID) polling ─────────────────────────────────
_UPID = "UPID:proxmox:001234AB:0056789A:66AABBCC:resize:100:root@pam:"


async def test_grow_disk_awaits_task_ok_then_verifies():
    """Resize returns a UPID; task runs then stops OK → verified grow."""
    fake = FakePVE()
    fake.resize_upid = _UPID
    fake.task_running_polls = 2  # two 'running' polls, then 'stopped'/OK
    res = await _adapter(fake).grow_vm_disk("scsi1", 32)
    assert res.ok is True and res.verified is True
    assert res.after == "64.0G"
    # exactly one mutating PUT despite the multi-poll wait
    assert len([c for c in fake.calls if c[0] == "PUT"]) == 1
    # the task-status GET url-encodes the UPID's colons (%3A)
    task_gets = [p for (m, p, _pa, _t) in fake.calls if m == "GET" and "/tasks/" in p]
    assert task_gets, "expected a task-status poll"
    assert "%3A" in task_gets[0]
    assert urllib.parse.quote(_UPID, safe="") in task_gets[0]
    # task status polled with the audit token
    assert all(t == "AUDIT" for (m, p, _pa, t) in fake.calls if "/tasks/" in p)


async def test_grow_disk_task_failed_reports_exitstatus_one_put():
    """Resize PUT 200 but the worker task FAILS → ok=False, exitstatus surfaced,
    exactly one PUT, and no confusion with a slow/unverified success."""
    fake = FakePVE()
    fake.resize_upid = _UPID
    fake.apply_writes = False  # a failed resize does not change the disk
    fake.task_exitstatus = "403 Permission check failed (/storage/local-lvm, Datastore.AllocateSpace)"
    res = await _adapter(fake).grow_vm_disk("scsi1", 32)
    assert res.ok is False and res.verified is False
    assert "resize task failed" in res.error
    assert "Datastore.AllocateSpace" in res.error  # the real exitstatus surfaced
    assert res.target_bytes == 64 * _GIB  # diagnostic target preserved
    assert len([c for c in fake.calls if c[0] == "PUT"]) == 1
    # early return on task failure → no config re-read after the task poll
    task_idx = max(i for i, c in enumerate(fake.calls) if "/tasks/" in c[1])
    assert not [c for c in fake.calls[task_idx + 1 :] if c[1].endswith("/config")]


async def test_grow_disk_task_poll_timeout_unverified():
    """Task never reaches 'stopped' → bounded poll gives up, unverified, one PUT."""
    fake = FakePVE()
    fake.resize_upid = _UPID
    fake.task_never_stops = True
    res = await _adapter(fake).grow_vm_disk("scsi1", 32)
    assert res.ok is False and res.verified is False
    assert "timed out" in res.error
    assert len([c for c in fake.calls if c[0] == "PUT"]) == 1


async def test_await_task_status_read_failure_unverified():
    """Persistent non-200 on the status endpoint → unverified, surfaced reason."""
    fake = FakePVE()
    fake.resize_upid = _UPID
    fake.task_status_code = 500
    res = await _adapter(fake).grow_vm_disk("scsi1", 32)
    assert res.ok is False and res.verified is False
    assert "timed out" in res.error and "500" in res.error


# ── grow memory ──────────────────────────────────────────────────────────
async def test_grow_memory_no_task_poll_when_synchronous():
    """Config PUT returns data=null (synchronous) → no /tasks/ poll, still verifies."""
    fake = FakePVE()
    fake.pending = [{"key": "memory", "value": "21500", "pending": "24576"}]
    res = await _adapter(fake).grow_vm_memory(24576)
    assert res.ok is True and res.verified is True
    assert not [c for c in fake.calls if "/tasks/" in c[1]]



async def test_grow_memory_grow_only_guard_no_put():
    fake = FakePVE()
    res = await _adapter(fake).grow_vm_memory(21500)  # == current
    assert res.ok is False and "grow-only" in res.error
    assert not [c for c in fake.calls if c[0] == "PUT"]


async def test_grow_memory_success_requires_reboot():
    fake = FakePVE()
    # pending shows memory staged (active 21500, pending 24576) → reboot needed
    fake.pending = [{"key": "memory", "value": "21500", "pending": "24576"}]
    res = await _adapter(fake).grow_vm_memory(24576)
    puts = [c for c in fake.calls if c[0] == "PUT"]
    assert len(puts) == 1 and puts[0][1].endswith("/config")
    assert puts[0][3] == "PROVISION"
    assert res.ok is True and res.verified is True
    assert res.requires_reboot is True


async def test_grow_memory_403_structured_failure():
    fake = FakePVE()
    fake.put_config_status = 403
    res = await _adapter(fake).grow_vm_memory(24576)
    assert res.ok is False and "403" in res.error


# ── never-raise ──────────────────────────────────────────────────────────
async def test_never_raises_on_transport_error():
    fake = FakePVE()
    fake.transport_dead = True
    a = _adapter(fake)
    cap = await a.get_capacity()
    assert cap.detected is False
    disk = await a.grow_vm_disk("scsi1", 32)
    assert disk.ok is False
    mem = await a.grow_vm_memory(24576)
    assert mem.ok is False
    assert await a.test_connectivity() is False


# ── connectivity + auth header ───────────────────────────────────────────
async def test_connectivity_true():
    assert await _adapter(FakePVE()).test_connectivity() is True


def test_auth_header_prefix_idempotent():
    assert ProxmoxAdapter._auth_header("genesis@pve!ro=uuid") == "PVEAPIToken=genesis@pve!ro=uuid"
    assert ProxmoxAdapter._auth_header("PVEAPIToken=x") == "PVEAPIToken=x"
