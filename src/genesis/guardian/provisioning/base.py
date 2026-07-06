"""Provisioning adapter ABC + typed results.

The adapter contract is deliberately narrow (capacity read + two grow verbs +
connectivity) and every method NEVER RAISES — failures come back as
``HostCapacity(detected=False, ...)`` or ``ProvisionResult(ok=False, error=...)``.
This lets the guardian call provisioning inline on its oneshot tick without a
provisioning bug ever taking the guardian down.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(frozen=True)
class HostCapacity:
    """A read-only snapshot of hypervisor + VM capacity.

    ``detected=False`` means the capacity read failed (adapter disabled,
    unreachable, or an unexpected API shape) — callers must refuse to
    provision on it, never assume headroom.

    NOTE ON MEMORY (2026-07-06 due-diligence finding): ``node_mem_available_bytes``
    is PVE ``/status`` ``.memory.available`` (free + reclaimable cache), NOT
    ``.memory.free`` (raw free, near-zero on a busy host). The RAM-headroom
    check MUST key on available or it spuriously refuses every grow.
    """

    detected: bool
    vm_memory_mib: int | None = None
    cores: int | None = None  # VM cores (from qemu config)
    disks: dict[str, int] = field(default_factory=dict)  # disk name → size bytes
    storage_free_bytes: int | None = None
    storage_total_bytes: int | None = None
    node_mem_total_bytes: int | None = None
    node_mem_available_bytes: int | None = None
    detail: str = ""


@dataclass(frozen=True)
class ProvisionResult:
    """Outcome of a single grow attempt.

    ``ok`` = the mutation call succeeded AND (where applicable) was verified by
    re-read. ``verified=False`` with ``ok`` unclear is treated as a critical,
    never-retried outcome by the flow (the mutation may or may not have landed).
    """

    ok: bool
    action: str
    requested: str = ""
    before: str = ""
    after: str = ""
    verified: bool = False
    requires_reboot: bool = False
    error: str = ""


class ProvisioningAdapter(ABC):
    """Abstract hypervisor provisioning interface.

    Implementations MUST NOT raise from any method — return the typed
    failure form instead.
    """

    @abstractmethod
    async def get_capacity(self) -> HostCapacity:
        """Read current VM + hypervisor capacity (read-only, audit creds)."""

    @abstractmethod
    async def grow_vm_disk(self, disk: str, add_gib: int) -> ProvisionResult:
        """Grow ``disk`` by ``add_gib`` GiB (grow-only), then verify by re-read."""

    @abstractmethod
    async def grow_vm_memory(self, new_mib: int) -> ProvisionResult:
        """Set VM memory to ``new_mib`` MiB (grow-only). May need a reboot."""

    @abstractmethod
    async def test_connectivity(self) -> bool:
        """Cheap read to confirm the API + audit credentials work."""

    async def newest_backup_age_days(self) -> float | None:
        """Age of the newest backup in days, or None if the adapter can't tell.

        Default None — the due-diligence gate treats None as 'unknown' and
        (when ``require_recent_backup`` is set) refuses rather than assuming.
        """
        return None
