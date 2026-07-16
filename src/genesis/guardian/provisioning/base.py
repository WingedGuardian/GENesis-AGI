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
    # The BACKUP datastore's headroom (may differ from ``storage``) — the
    # vzdump gate must check THIS, never storage_free_bytes (wrong datastore).
    backup_storage_free_bytes: int | None = None
    backup_storage_total_bytes: int | None = None
    # QEMU guest agent enabled? None = unknown. Informational: with the agent a
    # vzdump is filesystem-consistent (fsfreeze); without it, crash-consistent.
    vm_agent_enabled: bool | None = None
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
    # Absolute target the mutation aims for, in the resource's native unit
    # (disk: bytes). Lets the flow detect an unverified-but-landed grow and
    # avoid stacking a second RELATIVE disk grow. None when not applicable.
    target_bytes: int | None = None


@dataclass(frozen=True)
class BackupStartResult:
    """Outcome of LAUNCHING a backup task (phase 1 of 2).

    ``ok`` means the hypervisor accepted the job and returned a task handle —
    NOT that the backup succeeded (that is :class:`BackupStatus`'s job).
    ``attempted`` means the start request was actually sent: the caller must
    ledger the action iff attempted (even on failure — the hypervisor may have
    started work), and must NOT ledger pre-flight refusals (no token, bad
    config) where no request ever left the process.
    """

    ok: bool
    upid: str = ""
    requested: str = ""
    attempted: bool = False
    error: str = ""


@dataclass(frozen=True)
class BackupStatus:
    """One probe of a started backup task (phase 2 of 2).

    ``state``: running | verified | failed | unknown — see
    :meth:`ProvisioningAdapter.vzdump_status` for the caller contract.
    """

    state: str
    detail: str = ""
    volid: str = ""
    age_days: float | None = None


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

    # ── two-phase backup verbs (concrete defaults — not every hypervisor
    #    adapter must support backups; the not-supported forms keep the
    #    never-raise contract and fail the gate/flow safely) ────────────────

    async def vzdump_start(self) -> BackupStartResult:
        """START a backup task; return its handle immediately (never block).

        Two-phase by design: a full-VM backup runs for tens of minutes+, so the
        adapter only launches it. Verification is :meth:`vzdump_status`, driven
        by the caller's poll cadence against the returned ``upid``.
        """
        return BackupStartResult(
            ok=False, error="backups not supported by this adapter",
        )

    async def vzdump_status(self, upid: str) -> BackupStatus:
        """One status/verify probe for a started backup (single read, no loop).

        ``state`` contract (poll-outcome discipline): ``running`` = keep
        polling; ``verified`` = task finished AND the new backup is visible in
        the datastore; ``failed`` = the task itself reported terminal failure;
        ``unknown`` = this PROBE could not tell (transient read failure) — the
        caller must treat it as transient and retry, never as failure.
        """
        return BackupStatus(
            state="unknown", detail="backups not supported by this adapter",
        )

    async def prune_backups(self) -> tuple[bool, str]:
        """Rotate this VM's backups down to the configured keep-last.

        Runs only after a VERIFIED new backup. Returns (ok, detail). Rotation
        is the only cleanup path — there is deliberately no delete verb.
        """
        return False, "backups not supported by this adapter"
