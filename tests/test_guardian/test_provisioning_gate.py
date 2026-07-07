"""Due-diligence gate tests — every check evaluated, headroom on available RAM."""

from __future__ import annotations

from genesis.guardian.config import ProvisioningConfig
from genesis.guardian.provisioning.base import HostCapacity
from genesis.guardian.provisioning.gate import (
    evaluate_disk_grow,
    evaluate_memory_grow,
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
