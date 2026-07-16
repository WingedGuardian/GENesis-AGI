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
        # Backup (vzdump slice) knobs — synthetic identifiers only.
        self.backups: list = []  # content=backup items ({volid, vmid, ctime})
        self.content_status = 200  # non-200 → content list read failure
        self.vzdump_status_code = 200
        self.vzdump_upid = (
            "UPID:pve:000A1B2C:001122DD:68765432:vzdump:100:user@pve!backup:"
        )
        self.prune_status = 200
        self.prune_upid = None  # str → prune POST returns a task UPID
        self.prune_dry: list = []  # GET prunebackups dry-run rows

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
        if path.endswith("/prunebackups"):
            if method == "GET":
                return 200, list(self.prune_dry), ""
            if self.prune_status != 200:
                return self.prune_status, None, f"HTTP {self.prune_status}: denied"
            return 200, self.prune_upid, ""
        if method == "GET" and path.endswith("/content"):
            if self.content_status != 200:
                return self.content_status, None, f"HTTP {self.content_status}: denied"
            return 200, [dict(b) for b in self.backups], ""
        if method == "POST" and path.endswith("/vzdump"):
            if self.vzdump_status_code != 200:
                return self.vzdump_status_code, None, f"HTTP {self.vzdump_status_code}: denied"
            return 200, self.vzdump_upid, ""
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
    a = ProxmoxAdapter(
        cfg, audit_token="AUDIT", provision_token="PROVISION",
        backup_token="BACKUP",
    )
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


# ── backups: two-phase vzdump + age + rotation (all identifiers synthetic) ─

# The FakePVE default UPID's starttime field (index 4) in hex.
_UPID_STARTTIME = int("68765432", 16)


def _backup_item(ctime: int, vmid: int = 100, volid: str | None = None) -> dict:
    return {
        "volid": volid or f"backup:backup/vzdump-qemu-{vmid}-x.vma.zst",
        "vmid": vmid,
        "ctime": ctime,
        "content": "backup",
    }


async def test_backup_age_picks_newest_for_this_vmid_only():
    fake = FakePVE()
    fake.backups = [
        _backup_item(1000),
        _backup_item(5000),
        _backup_item(9000, vmid=1000),  # vmid FIELD filter — not volid substring
    ]
    a = _adapter(fake)
    age = await a.newest_backup_age_days()
    assert age is not None
    import time as _t
    assert age == pytest.approx((_t.time() - 5000) / 86400.0, rel=0.01)
    # audit token on the content read
    method, path, params, token = fake.calls[-1]
    assert token == "AUDIT" and path.endswith("/content") and params == {"content": "backup"}


async def test_backup_age_none_when_no_backups_or_read_fails():
    fake = FakePVE()
    a = _adapter(fake)
    assert await a.newest_backup_age_days() is None  # empty list
    fake.content_status = 500
    assert await a.newest_backup_age_days() is None  # read failure
    fake.transport_dead = True
    assert await a.newest_backup_age_days() is None  # never raises


async def test_backup_age_reads_configured_backup_storage():
    fake = FakePVE()
    cfg = ProvisioningConfig(
        enabled=True, api_host="10.0.0.9", node="pve", vmid=100,
        storage="local-lvm", backup_storage="backup", verify_tls=False,
    )
    a = ProxmoxAdapter(cfg, "AUDIT", "PROVISION", backup_token="BACKUP")
    a._request_sync = fake  # type: ignore[assignment]
    await a.newest_backup_age_days()
    _m, path, _p, _t = fake.calls[-1]
    assert "/storage/backup/content" in path


async def test_vzdump_start_happy_returns_upid_with_backup_token():
    fake = FakePVE()
    a = _adapter(fake)
    res = await a.vzdump_start()
    assert res.ok and res.attempted
    assert res.upid.startswith("UPID:")
    method, path, params, token = fake.calls[-1]
    assert (method, token) == ("POST", "BACKUP")
    assert path.endswith("/vzdump")
    assert params["mode"] == "snapshot" and params["compress"] == "zstd"
    assert "prune-backups" not in params, (
        "inline prune-backups needs Datastore.Allocate — rotation must use the "
        "standalone endpoint instead"
    )


async def test_vzdump_start_without_token_is_preflight_refusal():
    fake = FakePVE()
    cfg = ProvisioningConfig(
        enabled=True, api_host="10.0.0.9", node="pve", vmid=100, verify_tls=False,
    )
    a = ProxmoxAdapter(cfg, "AUDIT", "PROVISION")  # no backup token
    a._request_sync = fake  # type: ignore[assignment]
    res = await a.vzdump_start()
    assert not res.ok and not res.attempted, "pre-flight refusal must not be ledgered"
    assert fake.calls == [], "no request may leave the process without a token"


async def test_vzdump_start_post_failure_is_attempted():
    fake = FakePVE()
    fake.vzdump_status_code = 403
    a = _adapter(fake)
    res = await a.vzdump_start()
    assert not res.ok and res.attempted, "a sent-but-failed POST still counts (ledger it)"


async def test_vzdump_status_running_then_verified_anchored_to_upid_starttime():
    fake = FakePVE()
    a = _adapter(fake)
    upid = fake.vzdump_upid
    # still running
    fake.task_never_stops = True
    assert (await a.vzdump_status(upid)).state == "running"
    # stopped OK + a backup NEWER than the task start → verified
    fake.task_never_stops = False
    fake._task_polls = 0
    fake.backups = [
        _backup_item(_UPID_STARTTIME - 500),   # pre-existing backup: ignored
        _backup_item(_UPID_STARTTIME + 120),   # the new one
    ]
    st = await a.vzdump_status(upid)
    assert st.state == "verified"
    assert st.volid and st.age_days is not None


async def test_vzdump_status_task_ok_but_only_old_backups_is_unknown():
    """An OLD backup must never verify THIS task (restart-safe anchor)."""
    fake = FakePVE()
    a = _adapter(fake)
    fake.backups = [_backup_item(_UPID_STARTTIME - 500)]
    st = await a.vzdump_status(fake.vzdump_upid)
    assert st.state == "unknown"


async def test_vzdump_status_terminal_failure_and_transient_reads():
    fake = FakePVE()
    a = _adapter(fake)
    # terminal task failure
    fake.task_exitstatus = "job errors"
    assert (await a.vzdump_status(fake.vzdump_upid)).state == "failed"
    # status endpoint unreadable → unknown (caller retries), never failed
    fake.task_status_code = 500
    assert (await a.vzdump_status(fake.vzdump_upid)).state == "unknown"
    # task OK but content list unreadable → unknown
    fake.task_status_code = 200
    fake.task_exitstatus = "OK"
    fake._task_polls = 0
    fake.content_status = 500
    assert (await a.vzdump_status(fake.vzdump_upid)).state == "unknown"


async def test_prune_dry_runs_then_posts_with_backup_token():
    fake = FakePVE()
    fake.prune_dry = [
        {"volid": "backup:backup/vzdump-qemu-100-old.vma.zst", "mark": "remove"},
    ]
    a = _adapter(fake)
    ok, detail = await a.prune_backups()
    assert ok and "keep-last=2" in detail
    prune_calls = [c for c in fake.calls if c[1].endswith("/prunebackups")]
    assert [(m, t) for m, _p, _pa, t in prune_calls] == [
        ("GET", "AUDIT"), ("POST", "BACKUP"),
    ]
    _m, _p, params, _t = prune_calls[1]
    assert params == {"prune-backups": "keep-last=2", "type": "qemu", "vmid": 100}


async def test_prune_awaits_task_upid_and_reports_failure():
    fake = FakePVE()
    fake.prune_upid = "UPID:pve:000A1B2C:001122DD:68765433:imgdel:100:user@pve!backup:"
    fake.task_exitstatus = "removal failed"
    a = _adapter(fake)
    ok, detail = await a.prune_backups()
    assert not ok and "removal failed" in detail


async def test_prune_without_token_refuses():
    fake = FakePVE()
    cfg = ProvisioningConfig(
        enabled=True, api_host="10.0.0.9", node="pve", vmid=100, verify_tls=False,
    )
    a = ProxmoxAdapter(cfg, "AUDIT", "PROVISION")
    a._request_sync = fake  # type: ignore[assignment]
    ok, _detail = await a.prune_backups()
    assert not ok and fake.calls == []


async def test_capacity_carries_backup_storage_headroom_and_agent_flag():
    fake = FakePVE()
    fake.storage.append(
        {"storage": "backup", "total": 500 * _GIB, "avail": 150 * _GIB, "used": 1},
    )
    cfg = ProvisioningConfig(
        enabled=True, api_host="10.0.0.9", node="pve", vmid=100,
        storage="local-lvm", backup_storage="backup", verify_tls=False,
    )
    a = ProxmoxAdapter(cfg, "AUDIT", "PROVISION", backup_token="BACKUP")
    a._request_sync = fake  # type: ignore[assignment]
    cap = await a.get_capacity()
    assert cap.backup_storage_free_bytes == 150 * _GIB
    assert cap.backup_storage_total_bytes == 500 * _GIB
    assert cap.storage_free_bytes == 616384222882, "grow storage unchanged"
    assert cap.vm_agent_enabled is False, "no agent key = not enabled"


async def test_capacity_backup_storage_defaults_to_grow_storage():
    fake = FakePVE()
    a = _adapter(fake)  # backup_storage unset
    cap = await a.get_capacity()
    assert cap.backup_storage_free_bytes == cap.storage_free_bytes


def test_agent_flag_parsing():
    parse = ProxmoxAdapter._agent_enabled
    assert parse({}) is False
    assert parse({"agent": "1"}) is True
    assert parse({"agent": "0"}) is False
    assert parse({"agent": "enabled=1,fstrim_cloned_disks=1"}) is True
    assert parse({"agent": "enabled=0"}) is False
    assert parse({"agent": "weird"}) is None


def test_upid_starttime_parses_hex_field():
    parse = ProxmoxAdapter._upid_starttime
    assert parse("UPID:pve:000A1B2C:001122DD:68765432:vzdump:100:u@pve!t:") == _UPID_STARTTIME
    assert parse("not-a-upid") is None
    assert parse("UPID:pve:x:y:GGGG:vzdump:100:u@pve!t:") is None
