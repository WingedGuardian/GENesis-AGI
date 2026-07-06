"""Guardian hypervisor provisioning — rung 5 of the escalation ladder.

Host-side only. Grows the VM's virtual disk / RAM from the hypervisor so a
thin-pool with zero VG free extents can finally be extended (the structural
fix the 2026-07 thin-pool outage proved was missing). Every mutation passes a
fresh per-action Telegram APPROVE/DENY gate; the autonomous path only PROPOSES.

Mutation code never raises — adapters return typed results (never-raise
contract), so a provisioning failure degrades to an alert, never a guardian
crash.
"""

from __future__ import annotations

from genesis.guardian.provisioning.base import (
    HostCapacity,
    ProvisioningAdapter,
    ProvisionResult,
)

__all__ = [
    "HostCapacity",
    "ProvisionResult",
    "ProvisioningAdapter",
]
