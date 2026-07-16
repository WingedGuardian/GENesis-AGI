"""Provisioning ledger tests — rate window counts executed (incl. unverified)."""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta

from genesis.guardian.provisioning.ledger import ProvisioningLedger


def test_record_then_count(tmp_path):
    led = ProvisioningLedger(tmp_path)
    assert led.actions_in_window() == 0
    led.record_action("grow_vm_disk", "scsi1 +32G", ok=True, verified=True)
    assert led.actions_in_window() == 1


def test_unverified_still_counts(tmp_path):
    led = ProvisioningLedger(tmp_path)
    led.record_action("grow_vm_disk", "scsi1 +32G", ok=False, verified=False)
    assert led.actions_in_window() == 1, "unverified mutation must count against cap"


def test_old_entries_outside_window_not_counted(tmp_path):
    led = ProvisioningLedger(tmp_path)
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    ledger_file = tmp_path / "provisioning" / "ledger.json"
    ledger_file.parent.mkdir(parents=True, exist_ok=True)
    ledger_file.write_text(json.dumps([
        {"ts": old, "action": "grow_vm_disk", "requested": "x", "ok": True, "verified": True},
    ]))
    assert led.actions_in_window(days=7) == 0


def test_atomic_write_is_0600(tmp_path):
    led = ProvisioningLedger(tmp_path)
    led.record_action("grow_vm_memory", "24576MiB", ok=True, verified=True)
    f = tmp_path / "provisioning" / "ledger.json"
    assert f.exists()
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600


def test_corrupt_ledger_returns_zero(tmp_path):
    led = ProvisioningLedger(tmp_path)
    f = tmp_path / "provisioning" / "ledger.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("{ not json")
    assert led.actions_in_window() == 0  # never raises


def test_proposal_damper(tmp_path):
    led = ProvisioningLedger(tmp_path)
    assert led.hours_since_last_proposal() is None
    led.mark_proposed()
    hrs = led.hours_since_last_proposal()
    assert hrs is not None and hrs < 0.1


_GIB = 1024**3


def test_latest_unverified_disk_none_when_empty(tmp_path):
    led = ProvisioningLedger(tmp_path)
    assert led.latest_unverified_disk("scsi1") is None


def test_latest_unverified_disk_returns_unverified_entry(tmp_path):
    led = ProvisioningLedger(tmp_path)
    led.record_action("grow_vm_disk", "scsi1 +32G", ok=False, verified=False,
                      target_bytes=64 * _GIB)
    pend = led.latest_unverified_disk("scsi1")
    assert pend is not None and pend["target_bytes"] == 64 * _GIB


def test_latest_unverified_disk_none_when_verified(tmp_path):
    led = ProvisioningLedger(tmp_path)
    led.record_action("grow_vm_disk", "scsi1 +32G", ok=True, verified=True,
                      target_bytes=64 * _GIB)
    assert led.latest_unverified_disk("scsi1") is None


def test_latest_unverified_disk_is_per_disk_and_latest(tmp_path):
    led = ProvisioningLedger(tmp_path)
    led.record_action("grow_vm_disk", "scsi1 +32G", ok=False, verified=False,
                      target_bytes=64 * _GIB)          # scsi1 unverified
    led.record_action("grow_vm_disk", "virtio0 +8G", ok=True, verified=True,
                      target_bytes=40 * _GIB)          # different disk, verified
    # scsi1 substring must not false-match "scsi10"
    led.record_action("grow_vm_disk", "scsi10 +8G", ok=False, verified=False,
                      target_bytes=8 * _GIB)
    assert led.latest_unverified_disk("scsi1")["target_bytes"] == 64 * _GIB
    assert led.latest_unverified_disk("virtio0") is None


def test_mark_latest_disk_verified_clears_latch(tmp_path):
    led = ProvisioningLedger(tmp_path)
    led.record_action("grow_vm_disk", "scsi1 +32G", ok=False, verified=False,
                      target_bytes=64 * _GIB)
    led.mark_latest_disk_verified("scsi1")
    assert led.latest_unverified_disk("scsi1") is None
    # does NOT add a new mutation (rate cap unchanged)
    assert led.actions_in_window() == 1


# ── per-class rate caps + two-phase backup state (vzdump slice) ───────────


def test_action_prefix_scopes_the_count(tmp_path):
    """Grows and backups have separate budgets — neither consumes the other's."""
    led = ProvisioningLedger(tmp_path)
    led.record_action("grow_vm_disk", "scsi1 +32G", ok=True, verified=True)
    led.record_action("grow_vm_memory", "24576MiB", ok=True, verified=True)
    led.record_action("vzdump", "vmid 100 -> storage backup", ok=True,
                      verified=False, upid="UPID:pve:0001:0002:0003:vzdump:100:u@pve!t:")
    assert led.actions_in_window() == 3, "no prefix = legacy all-entries count"
    assert led.actions_in_window(action_prefix="grow_") == 2
    assert led.actions_in_window(action_prefix="vzdump") == 1


def test_record_action_stores_upid_only_when_given(tmp_path):
    led = ProvisioningLedger(tmp_path)
    led.record_action("grow_vm_disk", "scsi1 +8G", ok=True, verified=True)
    led.record_action("vzdump", "vmid 100", ok=True, verified=False,
                      upid="UPID:pve:0001:0002:0003:vzdump:100:u@pve!t:")
    entries = json.loads((tmp_path / "provisioning" / "ledger.json").read_text())
    assert "upid" not in entries[0]
    assert entries[1]["upid"].startswith("UPID:")


def test_latest_backup_returns_newest_vzdump_entry(tmp_path):
    led = ProvisioningLedger(tmp_path)
    assert led.latest_backup() is None
    led.record_action("vzdump", "first", ok=True, verified=True, upid="UPID:a:1:1:1:vzdump:100:u@p!t:")
    led.record_action("grow_vm_disk", "scsi1 +8G", ok=True, verified=True)
    led.record_action("vzdump", "second", ok=True, verified=False, upid="UPID:a:2:2:2:vzdump:100:u@p!t:")
    latest = led.latest_backup()
    assert latest is not None
    assert latest["requested"] == "second"
    assert latest["verified"] is False


def test_mark_latest_backup_verified_flips_in_place_no_new_entry(tmp_path):
    """Verification flips the START record — never appends (rate cap unchanged)."""
    led = ProvisioningLedger(tmp_path)
    upid = "UPID:a:2:2:2:vzdump:100:u@p!t:"
    led.record_action("vzdump", "vmid 100", ok=True, verified=False, upid=upid)
    assert led.actions_in_window(action_prefix="vzdump") == 1
    assert led.mark_latest_backup_verified(upid) is True
    entries = json.loads((tmp_path / "provisioning" / "ledger.json").read_text())
    assert len(entries) == 1, "flip must not append a second entry"
    assert entries[0]["verified"] is True
    assert entries[0]["ok"] is True
    assert "resolved_ts" in entries[0]
    assert led.actions_in_window(action_prefix="vzdump") == 1


def test_mark_latest_backup_verified_terminal_failure(tmp_path):
    led = ProvisioningLedger(tmp_path)
    upid = "UPID:a:3:3:3:vzdump:100:u@p!t:"
    led.record_action("vzdump", "vmid 100", ok=True, verified=False, upid=upid)
    assert led.mark_latest_backup_verified(upid, ok=False) is True
    entries = json.loads((tmp_path / "provisioning" / "ledger.json").read_text())
    assert entries[0]["ok"] is False
    assert entries[0]["verified"] is False
    assert led.actions_in_window(action_prefix="vzdump") == 1, \
        "a terminally-failed backup still counts against the backup cap"


def test_mark_latest_backup_verified_unknown_upid_is_noop(tmp_path):
    led = ProvisioningLedger(tmp_path)
    led.record_action("vzdump", "vmid 100", ok=True, verified=False,
                      upid="UPID:a:4:4:4:vzdump:100:u@p!t:")
    assert led.mark_latest_backup_verified("UPID:a:9:9:9:vzdump:100:u@p!t:") is False
    entries = json.loads((tmp_path / "provisioning" / "ledger.json").read_text())
    assert entries[0]["verified"] is False
