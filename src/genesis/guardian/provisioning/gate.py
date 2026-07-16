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

    def failed_names(self) -> list[str]:
        """Machine-readable failure surface — callers (e.g. the JIT
        backup→grow chain) must branch on THIS, never parse as_lines()."""
        return [c.name for c in self.checks if not c.passed]


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


def _rate_check(count: int, cap: int, label: str) -> Check:
    """Per-action-CLASS rate cap (grows and backups have separate budgets)."""
    ok = count < cap
    return Check(
        "rate cap", ok,
        f"{count} {label} executed in last 7d (cap {cap})",
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

    checks.append(_rate_check(actions_in_window, config.max_actions_per_week, "grows"))
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
        checks.append(_rate_check(actions_in_window, config.max_actions_per_week, "grows"))
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

    checks.append(_rate_check(actions_in_window, config.max_actions_per_week, "grows"))
    checks.append(_backup_check(config, backup_age_days))

    return DueDiligenceReport(
        passed=all(c.passed for c in checks),
        action="grow_vm_memory", requested=requested, checks=checks,
    )


def evaluate_vzdump(
    cap: HostCapacity,
    config: ProvisioningConfig,
    backups_in_window: int,
    in_flight_upid: str = "",
) -> DueDiligenceReport:
    """Due diligence for STARTING a vzdump (non-destructive, but not free:
    it reads the whole live VM disk and lands multi-GB on the datastore).

    ``in_flight_upid``: a still-latched earlier backup (unverified, younger
    than the vzdump wall bound) — a second start is refused while one may be
    running; PVE would reject it anyway, but only as an opaque task failure
    after another approval round.
    """
    storage_name = config.backup_storage or config.storage
    requested = f"vzdump vmid {config.vmid} -> {storage_name}"
    checks: list[Check] = []

    checks.append(Check("capacity detected", cap.detected, cap.detail or ""))

    # Estimated worst-case dump size = the VM's total disk allocation ×
    # the configured multiplier (1.0 default = incompressible worst case).
    # NOTE: prune runs AFTER the new backup lands, so the datastore
    # transiently holds keep_last+1 backups — live avail self-enforces this;
    # the detail names it so a post-rotation refusal reads as policy, not bug.
    alloc = sum(cap.disks.values()) if cap.disks else 0
    est = int(alloc * config.backup_size_multiplier)
    avail = cap.backup_storage_free_bytes
    space_ok = alloc > 0 and avail is not None and avail >= est
    if avail is None:
        space_detail = f"backup storage '{storage_name}' free space unknown — refusing"
    elif alloc <= 0:
        space_detail = "VM disk allocation unknown — cannot estimate dump size"
    else:
        space_detail = (
            f"{avail / _GIB:.0f}G free ≥ ~{est / _GIB:.0f}G est. dump "
            f"(alloc {alloc / _GIB:.0f}G × {config.backup_size_multiplier}; "
            f"store holds keep-last+1 until rotation)"
        )
    checks.append(Check("backup storage headroom", space_ok, space_detail))

    checks.append(_rate_check(backups_in_window, config.max_backups_per_week, "backups"))

    checks.append(Check(
        "no backup in flight", not in_flight_upid,
        f"unverified backup still in flight ({in_flight_upid}) — "
        "run provision-vzdump-status first" if in_flight_upid else "none in flight",
    ))

    # Informational only (always passes): which consistency class this buys.
    if cap.vm_agent_enabled is True:
        agent_detail = "guest agent enabled → filesystem-consistent (fsfreeze)"
    elif cap.vm_agent_enabled is False:
        agent_detail = ("no guest agent → crash-consistent "
                        "(like a power cut: journaled fs + SQLite WAL recover)")
    else:
        agent_detail = "guest agent state unknown"
    checks.append(Check("consistency class", True, agent_detail))

    return DueDiligenceReport(
        passed=all(c.passed for c in checks),
        action="vzdump", requested=requested, checks=checks,
    )
