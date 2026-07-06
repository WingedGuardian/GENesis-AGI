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
