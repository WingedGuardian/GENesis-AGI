"""Snapshot manager — HOST-SIDE. Incus snapshot create/restore/prune.

Supports both dir and BTRFS storage backends. BTRFS is strongly recommended
(instant CoW snapshots vs. slow full copies). The delete-before-create
strategy ensures at most `retention` guardian snapshots exist at a time.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta

from genesis.guardian._subprocess import run_subprocess as _run_subprocess
from genesis.guardian.config import GuardianConfig
from genesis.guardian.pool import measure_storage_pool, pool_mount_path

logger = logging.getLogger(__name__)

# Label suffix marking the offline snapshot-rollback lifeline. take(label=
# _HEALTHY_LABEL) produces names ending in this suffix; prune()/take()
# eviction exempt the latest one and mark_healthy() rotates the rest.
_HEALTHY_LABEL = "healthy"
_HEALTHY_SUFFIX = f"-{_HEALTHY_LABEL}"
# Public alias: pool_pressure classifies the lifeline by the SAME suffix.
HEALTHY_SUFFIX = _HEALTHY_SUFFIX


def _parse_created_at(raw: object) -> datetime | None:
    """Parse an incus snapshot `created_at` into an aware UTC datetime.

    incus emits RFC3339 (often with a `Z` suffix and up to nanosecond
    fractional seconds, which Python's fromisoformat can't take). Returns None
    on absent/unparseable values → the snapshot is treated as unknown-age and
    is never age-pruned (retention still applies).
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    s = raw.strip()
    # Truncate fractional seconds to 6 digits (drop nanosecond precision).
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


_NAME_TS_RE = re.compile(r"(\d{8})-(\d{6})")


def _created_from_name(name: str) -> datetime | None:
    """Creation time from a guardian snapshot name (``<prefix>YYYYmmdd-HHMMSS…``).

    take() stamps the UTC creation time into every name, so this is the
    fallback when incus omits ``created_at``.
    """
    m = _NAME_TS_RE.search(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None


# Substrings of an `incus snapshot create` failure that mean "the pool has no
# room" rather than some other fault. Measured on an LVM-thin pool whose
# autoextend profile threshold was exceeded: "Error creating LVM logical volume
# snapshot: ... Cannot create new thin volume, free space in thin pool
# vg0/IncusThinPool reached threshold."
_POOL_SPACE_ERRORS = ("free space in thin pool", "no space left", "insufficient free space")

# Delete-first rotation only replaces a lifeline at least this old. The
# healthy snapshot is refreshed once per ~24h maintenance pass; one hour of
# slack absorbs tick jitter so a normally-aged lifeline still qualifies, while
# a lifeline taken minutes ago (a retry storm) never does.
_DELETE_FIRST_MIN_AGE = timedelta(hours=23)

# take() outcomes that mean the POOL refused the snapshot (vs any other fault).
REFUSED_POOL_GATE = "pool_gate"
REFUSED_POOL_SPACE = "pool_space"
REFUSED_OTHER = "create_failed"


class SnapshotManager:
    """Manage incus snapshots for the Genesis container."""

    def __init__(self, config: GuardianConfig) -> None:
        self._config = config
        self._container = config.container_name
        self._prefix = config.snapshots.prefix
        self._retention = config.snapshots.retention
        # The names take() generates: <prefix><YYYYmmdd>-<HHMMSS>[-<label>].
        self._owned_re = re.compile(
            re.escape(self._prefix if isinstance(self._prefix, str) else "")
            + r"\d{8}-\d{6}(?:-[A-Za-z0-9-]+)?"
        )
        # Why the last take() returned None (REFUSED_*), or None after success.
        self.last_refusal: str | None = None
        # What mark_healthy() had to do beyond a plain rotation, for alerting.
        self.last_rotation_note: str | None = None

    async def check_pool_space(self) -> float:
        """Check genesis pool disk usage. Returns usage percentage (0-100).

        Auto-detects the pool mount point via incus device config.
        Returns 100.0 on any error (fail-safe: assume full).
        """
        # Discover pool name for this container
        rc, pool_name, _ = await _run_subprocess(
            "incus", "config", "device", "get", self._container, "root", "pool",
            timeout=10.0,
        )
        if rc != 0 or not pool_name.strip():
            logger.warning("Failed to detect storage pool — assuming full")
            return 100.0

        pool_path = pool_mount_path(pool_name.strip())
        rc, stdout, stderr = await _run_subprocess(
            "df", "--output=pcent", pool_path,
            timeout=10.0,
        )
        if rc != 0:
            logger.warning("Failed to check pool space at %s: %s", pool_path, stderr)
            return 100.0

        try:
            lines = stdout.strip().splitlines()
            if len(lines) < 2:
                return 100.0
            pct_str = lines[-1].strip().rstrip("%")
            return float(pct_str)
        except (ValueError, IndexError):
            logger.warning("Failed to parse pool space output: %s", stdout)
            return 100.0

    async def _get_pool_free_bytes(self) -> tuple[int, int] | None:
        """Get (total_bytes, free_bytes) for the storage pool. None on failure."""
        rc, pool_name, _ = await _run_subprocess(
            "incus", "config", "device", "get", self._container, "root", "pool",
            timeout=10.0,
        )
        if rc != 0 or not pool_name.strip():
            return None

        pool_path = pool_mount_path(pool_name.strip())
        rc, stdout, stderr = await _run_subprocess(
            "df", "--output=size,avail", "--block-size=1", pool_path,
            timeout=10.0,
        )
        if rc != 0:
            logger.warning("Failed to get pool bytes at %s: %s", pool_path, stderr)
            return None

        try:
            lines = stdout.strip().splitlines()
            if len(lines) < 2:
                return None
            parts = lines[-1].split()
            if len(parts) < 2:
                return None
            return int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            return None

    async def safe_to_snapshot(
        self, snapshot_size_history: list[int] | None = None,
    ) -> bool:
        """Check if it's safe to take a snapshot using headroom-based gating.

        Strategy:
        - LVM-thin pool detected: gate on REAL pool allocation (lvs data% /
          metadata%) at the storage-pool `high` tiers. The df paths below
          measure the host rootfs on LVM backends — the exact blindness behind
          the thin-pool-exhaustion incident — so lvs is authoritative here.
        - With snapshot size history: require free > max(min_headroom_gb, 2x avg
          of last 3 snapshot sizes). Adapts to actual snapshot sizes.
        - Without history: require at least 10% of pool free (safe default for
          first snapshots before any size data is available).
        - If pool detection fails entirely: fall back to percentage threshold
          (max_pool_usage_pct) for robustness.
        """
        try:
            pool_status = await measure_storage_pool(self._config)
        except Exception:
            logger.warning("Pool measurement failed — using df fallback", exc_info=True)
            pool_status = None
        if pool_status is not None and pool_status.detected and (
            pool_status.data_pct is not None or pool_status.metadata_pct is not None
        ):
            tiers = self._config.storage_pool
            data = pool_status.data_pct
            meta = pool_status.metadata_pct
            if data is not None and data >= tiers.data_high_pct:
                logger.error(
                    "Pool data %.1f%% >= %.0f%% high tier — refusing snapshot",
                    data, tiers.data_high_pct,
                )
                return False
            if meta is not None and meta >= tiers.metadata_high_pct:
                logger.error(
                    "Pool metadata %.1f%% >= %.0f%% high tier — refusing snapshot",
                    meta, tiers.metadata_high_pct,
                )
                return False
            return True

        pool_info = await self._get_pool_free_bytes()
        if pool_info is None:
            # Can't get byte-level info — fall back to percentage check
            max_pct = self._config.snapshots.max_pool_usage_pct
            usage = await self.check_pool_space()
            if usage > max_pct:
                logger.error(
                    "Pool usage %.0f%% exceeds %.0f%% threshold — refusing snapshot",
                    usage, max_pct,
                )
                return False
            return True

        total_bytes, free_bytes = pool_info
        min_headroom = int(self._config.snapshots.min_headroom_gb * 1024**3)

        history = snapshot_size_history or []
        if not history:
            # No history — require at least 10% of pool free
            threshold = int(total_bytes * 0.10)
            if free_bytes < threshold:
                logger.error(
                    "Pool free %d bytes < 10%% threshold %d bytes — refusing snapshot",
                    free_bytes, threshold,
                )
                return False
            return True

        # History available — require free > max(min_headroom, 2x avg last 3)
        recent = history[-3:]
        avg_size = sum(recent) // len(recent)
        required = max(min_headroom, 2 * avg_size)

        if free_bytes < required:
            logger.error(
                "Pool free %d bytes < required headroom %d bytes "
                "(min_headroom=%d, 2x_avg=%d, history=%d samples) — refusing snapshot",
                free_bytes, required, min_headroom, 2 * avg_size, len(recent),
            )
            return False

        logger.info(
            "Headroom check passed: %d bytes free, %d required "
            "(avg snapshot %d bytes, %d samples)",
            free_bytes, required, avg_size, len(recent),
        )
        return True

    async def take(
        self,
        label: str = "",
        snapshot_size_history: list[int] | None = None,
    ) -> str | None:
        """Create a snapshot. Returns the snapshot name or None on failure.

        Checks disk space before proceeding. Deletes excess snapshots
        before creating the new one to stay within retention limit.
        """
        self.last_refusal = None
        if not await self.safe_to_snapshot(snapshot_size_history):
            self.last_refusal = REFUSED_POOL_GATE
            return None

        # Delete-before-create: remove excess snapshots to stay within
        # retention — but NEVER evict the latest healthy snapshot (the offline
        # snapshot-rollback lifeline). prune() has the same exemption; without
        # it here, a pre-recovery take at retention=1 would delete the healthy
        # snapshot right before the risky action that might need it. Rotation
        # of superseded healthy snapshots is mark_healthy()'s job (after a
        # successful create — no zero-lifeline window).
        existing = await self.list_snapshots()
        latest_healthy = next(
            (n for n in existing if n.endswith(_HEALTHY_SUFFIX)), None,
        )
        candidates = [n for n in existing if n != latest_healthy]
        if candidates and len(candidates) >= self._retention:
            for old_name in candidates[self._retention - 1:]:
                if await self.delete(old_name):
                    logger.info("Deleted snapshot before create: %s", old_name)

        ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        name = f"{self._prefix}{ts}"
        if label:
            name = f"{name}-{label}"

        # Measure pool free before snapshot (for size estimation after)
        pre_info = await self._get_pool_free_bytes()

        rc, stdout, stderr = await _run_subprocess(
            "incus", "snapshot", "create", self._container, name,
            timeout=120.0,  # BTRFS is instant, dir is slow — compromise
        )
        if rc != 0:
            logger.error("Failed to create snapshot %s: %s", name, stderr)
            low = (stderr or "").lower()
            self.last_refusal = (
                REFUSED_POOL_SPACE if any(m in low for m in _POOL_SPACE_ERRORS)
                else REFUSED_OTHER
            )
            return None

        logger.info("Created snapshot: %s", name)

        # Record snapshot size estimate (delta in pool free space)
        if snapshot_size_history is not None and pre_info is not None:
            post_info = await self._get_pool_free_bytes()
            if post_info is not None:
                _, pre_free = pre_info
                _, post_free = post_info
                size_estimate = max(0, pre_free - post_free)
                if size_estimate > 0:
                    snapshot_size_history.append(size_estimate)
                    # Keep last 5 entries
                    del snapshot_size_history[:-5]
                    logger.info(
                        "Snapshot size estimate: %d bytes (%d samples in history)",
                        size_estimate, len(snapshot_size_history),
                    )

        return name

    async def delete(self, name: str) -> bool:
        """Delete a snapshot by name. Returns True on success.

        120s timeout: deleting a long-lived LVM-thin snapshot with heavy CoW
        divergence involves real kernel metadata work (the incident snapshots
        were months old) — 60s can genuinely be exceeded.
        """
        rc, _, stderr = await _run_subprocess(
            "incus", "snapshot", "delete", self._container, name,
            timeout=120.0,
        )
        if rc != 0:
            logger.warning("Failed to delete snapshot %s: %s", name, stderr)
            return False
        return True

    async def restore(self, name: str) -> bool:
        """Restore a snapshot. Returns True on success."""
        rc, stdout, stderr = await _run_subprocess(
            "incus", "snapshot", "restore", self._container, name,
            timeout=300.0,
        )
        if rc != 0:
            logger.error("Failed to restore snapshot %s: %s", name, stderr)
            return False

        logger.info("Restored snapshot: %s", name)
        return True

    async def _list_snapshots_with_meta(self) -> list[tuple[str, datetime | None]]:
        """List guardian snapshots as (name, created_at), newest-first by name.

        created_at is None when incus omits it or it can't be parsed (such
        snapshots are never age-pruned — only retention applies).
        """
        rc, stdout, stderr = await _run_subprocess(
            "incus", "snapshot", "list", self._container, "--format", "json",
            timeout=30.0,
        )
        if rc != 0:
            logger.warning("Failed to list snapshots: %s", stderr)
            return []

        try:
            snapshots = json.loads(stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("Failed to parse snapshot list: %s", exc)
            return []

        result: list[tuple[str, datetime | None]] = [
            (s.get("name", ""), _parse_created_at(s.get("created_at")))
            for s in snapshots
            if isinstance(s, dict) and s.get("name", "").startswith(self._prefix)
        ]
        # Sort by name (contains timestamp) — newest first
        result.sort(key=lambda t: t[0], reverse=True)
        return result

    async def list_snapshot_meta_strict(self) -> list[tuple[str, datetime | None]] | None:
        """Guardian-GENERATED snapshots only, or None when the list FAILED.

        Two differences from :meth:`_list_snapshots_with_meta`, both because
        this listing feeds AUTOMATIC deletes:

        * None, not [], on an incus or parse error — a caller asking "are there
          any guardian snapshots left?" would otherwise read "none".
        * Ownership is the full generated name (``<prefix>YYYYmmdd-HHMMSS`` plus
          an optional ``-<label>``), not the bare prefix: a snapshot someone
          named ``guardian-demo`` by hand is not the guardian's to delete.

        Missing ``created_at`` falls back to the name's timestamp.
        """
        if not isinstance(self._prefix, str) or not self._prefix.strip():
            # An empty prefix would make every timestamp-shaped snapshot "ours".
            logger.warning("snapshots.prefix is empty — refusing to claim ownership")
            return None
        rc, stdout, stderr = await _run_subprocess(
            "incus", "snapshot", "list", self._container, "--format", "json",
            timeout=30.0,
        )
        if rc != 0:
            logger.warning("Failed to list snapshots: %s", stderr)
            return None
        try:
            snapshots = json.loads(stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("Failed to parse snapshot list: %s", exc)
            return None
        if not isinstance(snapshots, list):
            return None
        out: list[tuple[str, datetime | None]] = []
        for s in snapshots:
            if not isinstance(s, dict):
                continue
            name = s.get("name", "")
            if not isinstance(name, str) or not self._owned_re.fullmatch(name):
                continue
            created = _parse_created_at(s.get("created_at")) or _created_from_name(name)
            out.append((name, created))
        out.sort(key=lambda t: t[0], reverse=True)
        return out

    async def list_snapshots(self) -> list[str]:
        """List all guardian snapshots, newest first."""
        return [name for name, _ in await self._list_snapshots_with_meta()]

    async def prune(self) -> int:
        """Prune guardian snapshots. Returns count deleted.

        Two independent rules, unioned:
        - **Retention**: keep the newest ``retention`` + the most-recent healthy;
          delete the rest.
        - **Age**: delete anything older than ``max_age_days`` that is NOT the
          most-recent healthy snapshot — *even if it currently sorts as the
          "newest"*. This is the incident backstop: a stale
          ``guardian-pre-recovery`` snapshot sorts newest by name suffix and
          would otherwise be protected by retention forever, accumulating CoW
          divergence. The most-recent healthy snapshot is always exempt — it is
          the offline snapshot-rollback lifeline.
        """
        meta = await self._list_snapshots_with_meta()
        if not meta:
            return 0

        names = [name for name, _ in meta]
        healthy = [n for n in names if n.endswith(_HEALTHY_SUFFIX)]
        latest_healthy = healthy[0] if healthy else None

        # Retention rule: keep newest N + latest healthy.
        to_keep = set(names[:self._retention])
        if latest_healthy:
            to_keep.add(latest_healthy)
        to_delete = {n for n in names if n not in to_keep}

        # Age rule: delete stale snapshots (except the healthy lifeline),
        # including a stale "newest" non-healthy snapshot that retention keeps.
        max_age_days = self._config.snapshots.max_age_days
        if max_age_days > 0:
            cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
            for name, created in meta:
                if name == latest_healthy:
                    continue  # never delete the rollback lifeline
                if created is not None and created < cutoff:
                    to_delete.add(name)

        deleted = 0
        for name in sorted(to_delete):
            if await self.delete(name):
                logger.info("Pruned snapshot: %s", name)
                deleted += 1

        return deleted

    async def enforce_expiry_policy(self) -> bool:
        """Idempotently set incus ``snapshots.expiry`` on the container.

        This makes the incus daemon auto-delete aged SCHEDULED snapshots even if
        the guardian process is dead — a guardian-independent safety net layered
        under :meth:`prune`. Deliberately does NOT set ``snapshots.expiry.manual``
        (that is instance-wide and would silently expire snapshots the user
        creates by hand; guardian-prefixed snapshots are handled by age-prune).
        Returns True when the policy was applied (or intentionally skipped).
        """
        expiry = self._config.snapshots.expiry
        if not expiry:
            return False
        rc, _, stderr = await _run_subprocess(
            "incus", "config", "set", self._container,
            "snapshots.expiry", expiry,
            timeout=10.0,
        )
        if rc != 0:
            logger.warning("Failed to set snapshots.expiry=%s: %s", expiry, stderr)
            return False
        logger.debug("Enforced snapshots.expiry=%s on %s", expiry, self._container)
        return True

    async def mark_healthy(
        self,
        snapshot_size_history: list[int] | None = None,
        *,
        under_pressure: bool = False,
    ) -> str | None:
        """Take a 'healthy' snapshot, then rotate superseded healthy ones.

        Create-then-delete ordering by default: the new lifeline must exist
        before the old one goes, so an ordinary failed create leaves the
        previous healthy snapshot intact with no zero-lifeline window.

        EXCEPT when the POOL refused the create (the guardian's pool gate, or
        LVM's own "free space in thin pool reached threshold"), the caller's
        MEASURED model says the pool is under pressure (``under_pressure`` —
        pool_pressure's runway assessment, not the static gate that refused),
        and the lifeline is at least a rotation interval old. Then
        create-first cannot succeed while the old snapshot's divergence keeps
        filling the pool — retrying it daily is how one snapshot once survived
        a week and filled the pool to 100%. So delete the stale lifeline FIRST,
        then create. The zero-lifeline window is a few seconds, or lasts until
        the pool recovers if the retry is still refused; either is recorded in
        ``last_rotation_note`` for the caller to alert on.

        Why the pressure condition: a pool that is merely FULL but stable
        (sitting above the static gate, growing little) also refuses the
        create. There the old snapshot holds almost nothing, deleting it frees
        nothing, the retry is refused again, and the rollback target is gone
        for good — a regression, measured in review. Stable-but-full keeps its
        aging lifeline; relief (pool_pressure) acts if that ever changes.
        """
        self.last_rotation_note = None
        name = await self.take(
            label=_HEALTHY_LABEL, snapshot_size_history=snapshot_size_history,
        )
        if (
            name is None
            and under_pressure
            and self.last_refusal in (REFUSED_POOL_GATE, REFUSED_POOL_SPACE)
        ):
            refusal = self.last_refusal
            stale = await self._stale_lifeline(datetime.now(UTC))
            if stale is not None and await self.delete(stale):
                logger.warning(
                    "Pool refused the healthy snapshot (%s): deleted stale lifeline %s "
                    "first, retrying the create", refusal, stale,
                )
                name = await self.take(
                    label=_HEALTHY_LABEL, snapshot_size_history=snapshot_size_history,
                )
                self.last_rotation_note = (
                    f"pool refused the create ({refusal}); deleted the stale lifeline "
                    f"{stale} first, then "
                    + (f"created {name}" if name else
                       f"the retry was refused too ({self.last_refusal}) — NO rollback "
                       "lifeline until the pool recovers")
                )
        if name is None:
            return None

        for old_name in await self.list_snapshots():
            is_superseded = old_name.endswith(_HEALTHY_SUFFIX) and old_name != name
            if is_superseded and await self.delete(old_name):
                logger.info("Rotated superseded healthy snapshot: %s", old_name)
        return name

    async def _stale_lifeline(self, now: datetime) -> str | None:
        """The healthy snapshot to delete first, or None.

        None unless the NEWEST healthy snapshot is at least a rotation old (a
        fresh lifeline is never deleted-first). When it is, return the OLDEST
        healthy snapshot: a superseded one left behind by a failed rotation
        delete holds the most divergence and is the worse rollback target, so
        it goes before the newest.
        """
        meta = await self.list_snapshot_meta_strict()
        if not meta:
            return None
        healthy = [(n, c) for n, c in meta if n.endswith(_HEALTHY_SUFFIX)]  # newest first
        if not healthy:
            return None
        newest_created = healthy[0][1]
        if newest_created is None or now - newest_created < _DELETE_FIRST_MIN_AGE:
            return None
        return healthy[-1][0]

    async def get_latest_healthy(self) -> str | None:
        """Get the name of the most recent 'healthy' snapshot."""
        snapshots = await self.list_snapshots()
        for name in snapshots:
            if name.endswith(_HEALTHY_SUFFIX):
                return name
        return None
