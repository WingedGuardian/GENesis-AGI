"""Due-diligence gate tests — every check evaluated, headroom on available RAM."""

from __future__ import annotations

from genesis.guardian.config import ProvisioningConfig
from genesis.guardian.provisioning.base import HostCapacity
from genesis.guardian.provisioning.gate import (
    evaluate_disk_grow,
    evaluate_memory_grow,
    evaluate_vzdump,
)

_GIB = 1024**3
_MIB = 1024**2


def _cfg(**kw) -> ProvisioningConfig:
    base = dict(
        enabled=True, max_disk_step_gib=32, max_memory_step_mib=4096,
        storage_margin_gib=64, node_memory_margin_mib=8192,
        max_actions_per_week=2, require_recent_backup=True, backup_max_age_days=14,
    )
    base.update(kw)
    return ProvisioningConfig(**base)


def _good_cap() -> HostCapacity:
    return HostCapacity(
        detected=True, vm_memory_mib=21500, cores=5,
        disks={"scsi0": 32 * _GIB, "scsi1": 32 * _GIB},
        storage_free_bytes=574 * _GIB, storage_total_bytes=1500 * _GIB,
        node_mem_total_bytes=141 * _GIB, node_mem_available_bytes=65 * _GIB,
        detail="ok",
    )


def _named(report, name):
    return next(c for c in report.checks if c.name == name)


# ── disk ──────────────────────────────────────────────────────────────────
def test_disk_grow_all_pass():
    r = evaluate_disk_grow(_good_cap(), _cfg(), "scsi1", 32, 0, 1.0)
    assert r.passed is True
    assert all(c.passed for c in r.checks)


def test_disk_grow_undetected_fails():
    cap = HostCapacity(detected=False, detail="unreachable")
    r = evaluate_disk_grow(cap, _cfg(), "scsi1", 32, 0, 1.0)
    assert r.passed is False
    assert _named(r, "capacity detected").passed is False


def test_disk_missing_fails():
    r = evaluate_disk_grow(_good_cap(), _cfg(), "scsi9", 32, 0, 1.0)
    assert _named(r, "disk exists").passed is False
    assert r.passed is False


def test_disk_step_over_cap_fails():
    r = evaluate_disk_grow(_good_cap(), _cfg(max_disk_step_gib=16), "scsi1", 32, 0, 1.0)
    assert _named(r, "step within cap").passed is False


def test_disk_storage_headroom_insufficient_fails():
    cap = HostCapacity(
        detected=True, disks={"scsi1": 32 * _GIB},
        storage_free_bytes=10 * _GIB,  # < 32 + 64 margin
        node_mem_available_bytes=65 * _GIB,
    )
    r = evaluate_disk_grow(cap, _cfg(), "scsi1", 32, 0, 1.0)
    assert _named(r, "storage headroom").passed is False


def test_disk_rate_cap_hit_fails():
    r = evaluate_disk_grow(_good_cap(), _cfg(max_actions_per_week=2), "scsi1", 32, 2, 1.0)
    assert _named(r, "rate cap").passed is False


def test_disk_backup_too_old_fails():
    r = evaluate_disk_grow(_good_cap(), _cfg(backup_max_age_days=14), "scsi1", 32, 0, 30.0)
    assert _named(r, "recent backup").passed is False


def test_disk_backup_unknown_fails_when_required():
    r = evaluate_disk_grow(_good_cap(), _cfg(require_recent_backup=True), "scsi1", 32, 0, None)
    assert _named(r, "recent backup").passed is False


def test_disk_backup_skipped_when_not_required():
    r = evaluate_disk_grow(_good_cap(), _cfg(require_recent_backup=False), "scsi1", 32, 0, None)
    assert _named(r, "recent backup").passed is True
    assert r.passed is True


# ── memory ─────────────────────────────────────────────────────────────────
def test_memory_grow_all_pass():
    r = evaluate_memory_grow(_good_cap(), _cfg(), 24576, 0, 1.0)
    assert r.passed is True


def test_memory_headroom_uses_available():
    # available too small → fail even though total is large
    cap = HostCapacity(
        detected=True, vm_memory_mib=21500,
        node_mem_total_bytes=141 * _GIB, node_mem_available_bytes=1 * _GIB,
    )
    r = evaluate_memory_grow(cap, _cfg(), 24576, 0, 1.0)
    assert _named(r, "node RAM headroom").passed is False


def test_memory_grow_only_guard():
    r = evaluate_memory_grow(_good_cap(), _cfg(), 21500, 0, 1.0)  # == current
    assert _named(r, "grow-only").passed is False


def test_memory_step_over_cap_fails():
    r = evaluate_memory_grow(_good_cap(), _cfg(max_memory_step_mib=1024), 24576, 0, 1.0)
    assert _named(r, "step within cap").passed is False  # delta 3076 > 1024


def test_memory_current_unknown_all_fail():
    cap = HostCapacity(detected=True, vm_memory_mib=None, node_mem_available_bytes=65 * _GIB)
    r = evaluate_memory_grow(cap, _cfg(), 24576, 0, 1.0)
    assert r.passed is False


def test_report_as_lines_render():
    r = evaluate_disk_grow(_good_cap(), _cfg(), "scsi1", 32, 0, 1.0)
    lines = r.as_lines()
    assert len(lines) == len(r.checks)
    assert all(line.startswith(("✅", "❌")) for line in lines)


# ── evaluate_vzdump (backup start gate) ───────────────────────────────────

def _backup_cap(**kw):
    defaults = dict(
        detected=True, vm_memory_mib=21500, cores=5,
        disks={"scsi0": 32 * _GIB, "scsi1": 64 * _GIB},
        storage_free_bytes=574 * _GIB,
        backup_storage_free_bytes=150 * _GIB,
        backup_storage_total_bytes=500 * _GIB,
        vm_agent_enabled=False, detail="ok",
    )
    defaults.update(kw)
    return HostCapacity(**defaults)


def _bcfg(**kw):
    return ProvisioningConfig(
        enabled=True, vmid=100, storage="local-lvm", backup_storage="backup", **kw,
    )


def test_vzdump_passes_with_headroom_and_reports_consistency():
    rep = evaluate_vzdump(_backup_cap(), _bcfg(), backups_in_window=0)
    assert rep.passed and rep.action == "vzdump"
    assert rep.failed_names() == []
    consistency = next(c for c in rep.checks if c.name == "consistency class")
    assert consistency.passed and "crash-consistent" in consistency.detail


def test_vzdump_checks_backup_storage_not_grow_storage():
    """S2: 96G alloc, grow storage has plenty, but the BACKUP storage has 50G
    — the gate must refuse on the backup storage's headroom."""
    cap = _backup_cap(backup_storage_free_bytes=50 * _GIB)
    rep = evaluate_vzdump(cap, _bcfg(), backups_in_window=0)
    assert not rep.passed
    assert rep.failed_names() == ["backup storage headroom"]


def test_vzdump_refuses_on_unknown_backup_storage_space():
    cap = _backup_cap(backup_storage_free_bytes=None)
    rep = evaluate_vzdump(cap, _bcfg(), backups_in_window=0)
    assert not rep.passed
    assert "backup storage headroom" in rep.failed_names()


def test_vzdump_size_multiplier_scales_the_estimate():
    # 96G alloc × 0.5 = 48G est → 50G free passes
    cap = _backup_cap(backup_storage_free_bytes=50 * _GIB)
    rep = evaluate_vzdump(cap, _bcfg(backup_size_multiplier=0.5), backups_in_window=0)
    assert rep.passed, rep.as_lines()


def test_vzdump_backup_rate_cap_is_its_own_class():
    rep = evaluate_vzdump(_backup_cap(), _bcfg(max_backups_per_week=2), backups_in_window=2)
    assert not rep.passed
    assert rep.failed_names() == ["rate cap"]
    rate = next(c for c in rep.checks if c.name == "rate cap")
    assert "backups" in rate.detail, "the detail must name the class"


def test_vzdump_in_flight_latch_refuses():
    rep = evaluate_vzdump(
        _backup_cap(), _bcfg(), backups_in_window=0,
        in_flight_upid="UPID:pve:0A:0B:68765432:vzdump:100:u@pve!t:",
    )
    assert not rep.passed
    assert rep.failed_names() == ["no backup in flight"]


def test_grow_rate_check_names_its_class():
    rep = evaluate_disk_grow(
        _backup_cap(), _bcfg(), "scsi1", 8, actions_in_window=0, backup_age_days=1.0,
    )
    rate = next(c for c in rep.checks if c.name == "rate cap")
    assert "grows" in rate.detail
