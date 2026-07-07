"""Due-diligence gate for provisioning grows (pure, never-raises).

Given a fresh HostCapacity + the requested grow + rate-window count + backup
age, produce a DueDiligenceReport where EVERY check is evaluated (not fail-fast)
so the Telegram proposal can show the operator the full table. ``passed`` is the
AND of all checks. This runs twice per action: once to decide whether to
propose, and again — freshly — AFTER approval, immediately before mutating.
"""

from __future__ import annotations

from dataclasses import dataclass

from genesis.guardian.config import ProvisioningConfig
from genesis.guardian.provisioning.base import HostCapacity

_GIB = 1024**3
_MIB = 1024**2


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class DueDiligenceReport:
    passed: bool
    action: str
    requested: str
    checks: list[Check]

    def as_lines(self) -> list[str]:
        return [
            f"{'✅' if c.passed else '❌'} {c.name} — {c.detail}"
            for c in self.checks
        ]


def _backup_check(
    config: ProvisioningConfig, backup_age_days: float | None,
) -> Check:
    if not config.require_recent_backup:
        return Check("recent backup", True, "skipped (require_recent_backup=false)")
    if backup_age_days is None:
        return Check("recent backup", False, "backup age unknown — refusing")
    ok = backup_age_days <= config.backup_max_age_days
    return Check(
        "recent backup", ok,
        f"newest backup {backup_age_days:.1f}d old (limit {config.backup_max_age_days}d)",
    )


def _rate_check(config: ProvisioningConfig, actions_in_window: int) -> Check:
    ok = actions_in_window < config.max_actions_per_week
    return Check(
        "rate cap", ok,
        f"{actions_in_window} executed in last 7d (cap {config.max_actions_per_week})",
    )


def evaluate_disk_grow(
    cap: HostCapacity,
    config: ProvisioningConfig,
    disk: str,
    add_gib: int,
    actions_in_window: int,
    backup_age_days: float | None,
) -> DueDiligenceReport:
    requested = f"{disk} +{add_gib}G"
    checks: list[Check] = []

    checks.append(Check("capacity detected", cap.detected, cap.detail or ""))

    disk_bytes = cap.disks.get(disk)
    checks.append(Check(
        "disk exists", disk_bytes is not None,
        f"{disk} = {disk_bytes / _GIB:.0f}G" if disk_bytes else f"{disk} not on VM",
    ))

    step_ok = 0 < add_gib <= config.max_disk_step_gib
    checks.append(Check(
        "step within cap", step_ok,
        f"+{add_gib}G (per-action cap {config.max_disk_step_gib}G)",
    ))

    free = cap.storage_free_bytes
    need = add_gib * _GIB + config.storage_margin_gib * _GIB
    hdr_ok = free is not None and free >= need
    checks.append(Check(
        "storage headroom", hdr_ok,
        f"{(free or 0) / _GIB:.0f}G free ≥ {add_gib}G + {config.storage_margin_gib}G margin"
        if free is not None else "storage free unknown",
    ))

    checks.append(_rate_check(config, actions_in_window))
    checks.append(_backup_check(config, backup_age_days))

    return DueDiligenceReport(
        passed=all(c.passed for c in checks),
        action="grow_vm_disk", requested=requested, checks=checks,
    )


def evaluate_memory_grow(
    cap: HostCapacity,
    config: ProvisioningConfig,
    new_mib: int,
    actions_in_window: int,
    backup_age_days: float | None,
) -> DueDiligenceReport:
    requested = f"{new_mib}MiB"
    checks: list[Check] = []

    checks.append(Check("capacity detected", cap.detected, cap.detail or ""))

    current = cap.vm_memory_mib
    if current is None:
        checks.append(Check("current memory known", False, "VM memory unreadable"))
        # Without current we can't evaluate grow-only/step/headroom meaningfully.
        checks.append(Check("grow-only", False, "cannot verify (current unknown)"))
        checks.append(Check("step within cap", False, "cannot verify (current unknown)"))
        checks.append(Check("node RAM headroom", False, "cannot verify (current unknown)"))
        checks.append(_rate_check(config, actions_in_window))
        checks.append(_backup_check(config, backup_age_days))
        return DueDiligenceReport(
            passed=False, action="grow_vm_memory", requested=requested, checks=checks,
        )

    delta = new_mib - current
    grow_ok = delta > 0
    checks.append(Check(
        "grow-only", grow_ok, f"{current}→{new_mib} MiB (Δ{delta})",
    ))

    step_ok = 0 < delta <= config.max_memory_step_mib
    checks.append(Check(
        "step within cap", step_ok,
        f"Δ{delta} MiB (per-action cap {config.max_memory_step_mib})",
    ))

    # DD finding: headroom keys on node_mem_available (free + reclaimable),
    # never raw free.
    avail = cap.node_mem_available_bytes
    need = max(delta, 0) * _MIB + config.node_memory_margin_mib * _MIB
    hdr_ok = avail is not None and avail >= need
    checks.append(Check(
        "node RAM headroom", hdr_ok,
        f"{(avail or 0) / _GIB:.1f}G available ≥ Δ{delta}MiB + "
        f"{config.node_memory_margin_mib}MiB margin"
        if avail is not None else "node available memory unknown",
    ))

    checks.append(_rate_check(config, actions_in_window))
    checks.append(_backup_check(config, backup_age_days))

    return DueDiligenceReport(
        passed=all(c.passed for c in checks),
        action="grow_vm_memory", requested=requested, checks=checks,
    )
