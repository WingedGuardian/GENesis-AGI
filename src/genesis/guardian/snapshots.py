"""Snapshot manager — HOST-SIDE. Incus snapshot create/restore/prune.

Supports both dir and BTRFS storage backends. BTRFS is strongly recommended
(instant CoW snapshots vs. slow full copies). The delete-before-create
strategy ensures at most `retention` guardian snapshots exist at a time.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from genesis.guardian._subprocess import run_subprocess as _run_subprocess
from genesis.guardian.config import GuardianConfig
from genesis.guardian.pool import (
    THIN_LV_FIELDS,
    THIN_LV_SELECT,
    incus_container_lv_name,
    incus_snapshot_lv_name,
    measure_storage_pool,
    pool_mount_path,
    snapshot_only_bytes,
)

logger = logging.getLogger(__name__)

# Label suffix marking the offline snapshot-rollback lifeline. take(label=
# _HEALTHY_LABEL) produces names ending in this suffix; prune() and take()'s
# eviction never delete one, and every deletion goes through delete_healthy().
_HEALTHY_LABEL = "healthy"
_HEALTHY_SUFFIX = f"-{_HEALTHY_LABEL}"
# Public alias: pool_relief classifies the lifeline by the SAME suffix.
HEALTHY_SUFFIX = _HEALTHY_SUFFIX
PRE_RECOVERY_LABEL = "pre-recovery"
# Every label take() may stamp. Ownership (which snapshots the guardian may
# delete) is the exact generated name, so take() refuses any other label: a
# new label must be added here, where the ownership pattern can see it.
_GENERATED_LABELS = (_HEALTHY_LABEL, PRE_RECOVERY_LABEL)


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


_NAME_TS_RE = re.compile(r"(\d{8})-(\d{6})(?:-|$)")


def _created_from_name(name: str, prefix: str) -> datetime | None:
    """Creation time from a guardian snapshot name (``<prefix>YYYYmmdd-HHMMSS…``).

    take() stamps the UTC creation time into every name, so this is the
    fallback when incus omits ``created_at``. Parsed at the exact boundary
    after ``prefix``, never the first timestamp-shaped text anywhere: a prefix
    like ``archive-20200101-000000-`` would otherwise date every snapshot 2020
    (review).
    """
    if not isinstance(prefix, str) or not prefix or not name.startswith(prefix):
        return None
    m = _NAME_TS_RE.match(name, len(prefix))
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None


# Substrings of an `incus snapshot create` failure that mean "the THIN POOL has
# no room" rather than some other fault. Only LVM's thin-pool refusal: generic
# ENOSPC text ("no space left") can come from the incus daemon, its database or
# the host filesystem, and a pool refusal is what licenses delete-first
# (review). Measured on an LVM-thin pool whose
# autoextend profile threshold was exceeded: "Error creating LVM logical volume
# snapshot: ... Cannot create new thin volume, free space in thin pool
# vg0/IncusThinPool reached threshold."
_POOL_SPACE_ERRORS = ("free space in thin pool",)

# Delete-first rotation only replaces a lifeline at least this old. The
# healthy snapshot is refreshed once per ~24h maintenance pass; one hour of
# slack absorbs tick jitter so a normally-aged lifeline still qualifies, while
# a lifeline taken minutes ago (a retry storm) never does.
_DELETE_FIRST_MIN_AGE = timedelta(hours=23)

# take() outcomes that mean the POOL refused the snapshot (vs any other fault).
REFUSED_POOL_GATE = "pool_gate"
REFUSED_POOL_SPACE = "pool_space"
REFUSED_PROBE = "probe_failed"
REFUSED_OTHER = "create_failed"
REFUSED_CONFIG = "invalid_config"

# Delete-first needs EVIDENCE that deleting the lifeline frees real space:
# LVM must show the guardian's healthy snapshots hold at least this much that
# no live volume maps (pool.snapshot_only_bytes — a measured lower bound, not
# an inference from pool growth, which new container data also causes). Below
# it, deleting the lifeline would free next to nothing and leave no rollback
# target. The floor is measurement noise, not a policy threshold: 1 GiB, or 1%
# of the pool on a large one. Only LVM-thin can measure this; on other
# backends delete-first never fires and pool relief's reserve is the guard.
_SNAPSHOT_ONLY_MIN_BYTES = 1024**3
_SNAPSHOT_ONLY_MIN_FRAC = 0.01


def _sort_newest_first(rows: list[tuple[str, datetime | None]], prefix: str) -> None:
    """Order (name, created) rows newest first by CREATION TIME, in place.

    Never by the name string: every "latest healthy" decision (prune's
    exemption, the rollback target, delete-first's staleness) reads this order,
    and ``guardian-99999999-999999-healthy`` fits the ownership pattern and
    would outrank the real lifeline (security review). incus stamps
    ``created_at`` itself. Only a row incus left UNDATED is ordered by the
    timestamp take() wrote into its name (review: sorting it oldest made a
    fresh, undated lifeline lose to an older dated one); that fallback orders
    only — prune's age rule still needs a real created_at. Rows with neither
    sort oldest; the name breaks exact ties.
    """
    floor = datetime.min.replace(tzinfo=UTC)
    rows.sort(
        key=lambda t: (t[1] or _created_from_name(t[0], prefix) or floor, t[0]),
        reverse=True,
    )


class SnapshotManager:
    """Manage incus snapshots for the Genesis container."""

    def __init__(self, config: GuardianConfig) -> None:
        self._config = config
        self._container = config.container_name
        self._prefix = config.snapshots.prefix
        self._retention = config.snapshots.retention
        # The names take() generates: <prefix><YYYYmmdd>-<HHMMSS>[-<label>].
        # The ONE ownership test for every listing (and so every delete path):
        # a hand-made snapshot that merely starts with the prefix is not ours.
        self._owned_re = re.compile(
            re.escape(self._prefix if isinstance(self._prefix, str) else "")
            + r"\d{8}-\d{6}(?:-(?:"
            + "|".join(re.escape(label) for label in _GENERATED_LABELS)
            + "))?"
        )
        # Whether safe_to_snapshot's last refusal came from a real measurement.
        self.last_gate_measured = False
        # Why the last take() returned None (REFUSED_*), or None after success.
        self.last_refusal: str | None = None
        # Why the last delete() failed (the runner's "timeout" = outcome
        # unknown), or None after a success.
        self.last_delete_error: str | None = None
        # Whether the last take() evicted a snapshot before its create.
        self.last_take_evicted = False
        # Whether mark_healthy() deleted a lifeline first (alerted at once).
        self.last_rotation_deleted = False
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
        self.last_gate_measured = False
        try:
            pool_status = await measure_storage_pool(self._config)
        except Exception:
            logger.warning("Pool measurement failed — using df fallback", exc_info=True)
            pool_status = None
        pool_measured = pool_status is not None and pool_status.detected
        if pool_measured:
            # ONE admission rule with pool relief (review, round 2: the gate
            # still trusted what relief rejects). An LVM measurement that
            # cannot name its thin-pool LV (several thin pools: the figures may
            # be another pool's) or carries no figures at all proves nothing
            # about THIS pool — refuse, as a probe refusal, rather than admit on
            # another pool's number or fall through to df of the host fs.
            from genesis.guardian.pool_relief import (
                shortfall,
                unactionable,
                validate_relief_config,
            )

            blocked = unactionable(pool_status)
            if blocked is not None:
                logger.error("Pool measurement unusable (%s) — refusing snapshot", blocked[1])
                return False
            # Never looser than pool relief: a snapshot taken inside relief's
            # reserve would be deleted again within minutes, freeing nothing
            # and costing the lifeline (review: on a large non-LVM pool the
            # headroom gate below admits creates relief then undoes).
            config_trusted = validate_relief_config(self._config) is None
            # Only with a config relief itself trusts: an invalid reserve
            # (e.g. "3" as a string, or 60) must neither crash the gate — it
            # runs before a recovery action — nor pose as a pool refusal.
            reason = shortfall(pool_status, self._config.storage_pool) if config_trusted else None
            if reason is not None:
                logger.error("Pool at its relief reserve (%s) — refusing snapshot", reason)
                self.last_gate_measured = True
                return False
        if pool_measured and (
            pool_status.data_pct is not None or pool_status.metadata_pct is not None
        ):
            tiers = self._config.storage_pool
            if not all(
                isinstance(v, (int, float)) and not isinstance(v, bool)
                and math.isfinite(v) and 1 <= v <= 100
                for v in (tiers.data_high_pct, tiers.metadata_high_pct)
            ):
                # A malformed tier (a string, -1) cannot gate: refuse, but as a
                # PROBE refusal — it must never license delete-first (review).
                logger.error("storage_pool high tiers invalid — refusing snapshot")
                return False
            data = pool_status.data_pct
            meta = pool_status.metadata_pct
            if data is not None and data >= tiers.data_high_pct:
                logger.error(
                    "Pool data %.1f%% >= %.0f%% high tier — refusing snapshot",
                    data, tiers.data_high_pct,
                )
                self.last_gate_measured = True
                return False
            if meta is not None and meta >= tiers.metadata_high_pct:
                logger.error(
                    "Pool metadata %.1f%% >= %.0f%% high tier — refusing snapshot",
                    meta, tiers.metadata_high_pct,
                )
                self.last_gate_measured = True
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
        # A refusal below is a real POOL measurement only when the backend was
        # positively identified as non-LVM (measure_storage_pool detected it via
        # df on the pool mount). On an LVM pool whose lvs failed, this df reads
        # the HOST filesystem, so that refusal stays a probe refusal.
        measured_here = (
            pool_measured and pool_status.vg_name is None
            and pool_status.pool_used_pct is not None
        )

        history = snapshot_size_history or []
        if not history:
            # No history — require at least 10% of pool free
            threshold = int(total_bytes * 0.10)
            if free_bytes < threshold:
                logger.error(
                    "Pool free %d bytes < 10%% threshold %d bytes — refusing snapshot",
                    free_bytes, threshold,
                )
                self.last_gate_measured = measured_here
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
            self.last_gate_measured = measured_here
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
        if label and label not in _GENERATED_LABELS:
            # A snapshot this manager could not recognise as its own would never
            # be pruned, rotated or freed — refuse it at the source.
            raise ValueError(f"unknown snapshot label {label!r}; add it to _GENERATED_LABELS")
        self.last_refusal = None
        self.last_take_evicted = False
        if not isinstance(self._prefix, str) or not self._prefix.strip():
            # No ownership namespace: a snapshot created now could never be
            # listed back as the guardian's, so it would never be rotated,
            # pruned or freed (review). Refuse at the source.
            logger.error("snapshots.prefix is empty or invalid — refusing to create a snapshot")
            self.last_refusal = REFUSED_CONFIG
            return None
        if not await self.safe_to_snapshot(snapshot_size_history):
            # Only a refusal on a real pool measurement counts as the POOL
            # refusing (LVM lvs, or df on a positively non-LVM pool). The
            # percentage fallback also returns False when incus/df could not be
            # queried (it assumes "full"), and a probe failure must never
            # license deleting the lifeline first.
            self.last_refusal = (
                REFUSED_POOL_GATE if self.last_gate_measured else REFUSED_PROBE
            )
            return None

        # Delete-before-create: remove excess snapshots to stay within
        # retention — but NEVER evict a healthy snapshot here. Not the latest
        # (the offline snapshot-rollback lifeline: a pre-recovery take at
        # retention=1 would delete it right before the risky action that might
        # need it), and not a superseded one either: evicting it BEFORE a
        # create that then fails, with delete-first taking the other, would
        # leave no rollback target (review). Superseded healthy snapshots are
        # mark_healthy()'s to rotate, after a successful create.
        existing = await self.list_snapshots()
        candidates = [n for n in existing if not n.endswith(_HEALTHY_SUFFIX)]
        if candidates and len(candidates) >= self._retention:
            for old_name in candidates[self._retention - 1:]:
                ok = await self.delete(old_name)
                if ok:
                    logger.info("Deleted snapshot before create: %s", old_name)
                if ok or self.last_delete_error == "timeout":
                    # Its space may still be coming back (btrfs frees
                    # asynchronously; a timed-out delete may still be running
                    # in the daemon): mark_healthy must not delete-first on top
                    # of it in the same attempt (review).
                    self.last_take_evicted = True

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
        """Delete a NON-healthy snapshot by name. Returns True on success.

        A healthy snapshot (a rollback target) is refused here: the only way
        GUARDIAN CODE deletes one is :meth:`delete_healthy`, the single
        chokepoint that checks a rollback cannot still need it (review, round 3:
        three paths deleted rollback targets under three different
        preconditions). Outside the guardian, incus's own ``snapshots.expiry``
        (asserted by :meth:`enforce_expiry_policy`) can still expire one on
        Incus versions that apply it to manual snapshots — see #2486.
        """
        if name.endswith(_HEALTHY_SUFFIX):
            logger.error("refusing to delete rollback snapshot %s outside delete_healthy()", name)
            self.last_delete_error = "healthy snapshot: use delete_healthy()"
            return False
        return await self._delete_raw(name)

    async def delete_healthy(
        self, name: str, *, healthy_confirmed: bool = False, replaced_by: str | None = None,
    ) -> bool:
        """THE chokepoint for deleting a rollback (healthy) snapshot.

        Allowed only when a rollback cannot need it:

        * ``replaced_by`` — a newer healthy snapshot was just created (plain
          rotation), so a rollback target still exists; or
        * ``healthy_confirmed`` — THIS tick's health probe (not a stale
          persisted state) found the container HEALTHY, so no recovery in this
          tick will reach for SNAPSHOT_ROLLBACK.

        Everything else — a pre-probe pass, an unhealthy tick — is refused.
        """
        if not name.endswith(_HEALTHY_SUFFIX):
            return await self.delete(name)
        if replaced_by is None and not healthy_confirmed:
            logger.warning("refusing to delete rollback snapshot %s: health not confirmed", name)
            self.last_delete_error = "health not confirmed"
            return False
        return await self._delete_raw(name)

    async def _delete_raw(self, name: str) -> bool:
        """``incus snapshot delete``. 120s timeout: deleting a long-lived
        LVM-thin snapshot with heavy CoW divergence involves real kernel
        metadata work (the incident snapshots were months old) — 60s can
        genuinely be exceeded."""
        rc, _, stderr = await _run_subprocess(
            "incus", "snapshot", "delete", self._container, name,
            timeout=120.0,
        )
        if rc != 0:
            logger.warning("Failed to delete snapshot %s: %s", name, stderr)
            # "timeout" (the runner's marker) means the OUTCOME is unknown:
            # the daemon may still finish the delete. Callers that chain
            # deletes read this (pool_relief).
            self.last_delete_error = stderr or "failed"
            return False
        self.last_delete_error = None
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
        """List guardian snapshots as (name, created_at), newest-first by created_at.

        created_at is None when incus omits it or it can't be parsed (such
        snapshots are never age-pruned — only retention applies).

        Only guardian-GENERATED names (``_owned_re``), never the bare prefix:
        this listing feeds prune, retention eviction, rotation and the rollback
        target, so a hand-made ``guardian-mine-healthy`` must not become any of
        them. An empty prefix claims nothing.
        """
        if not isinstance(self._prefix, str) or not self._prefix.strip():
            logger.warning("snapshots.prefix is empty — refusing to claim ownership")
            return []
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

        if not isinstance(snapshots, list):
            return []
        result: list[tuple[str, datetime | None]] = [
            (s["name"], _parse_created_at(s.get("created_at")))
            for s in snapshots
            if isinstance(s, dict)
            and isinstance(s.get("name"), str)
            and self._owned_re.fullmatch(s["name"])
        ]
        # Newest first by incus's own created_at (never by the name string).
        _sort_newest_first(result, self._prefix)
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
            created = _parse_created_at(s.get("created_at")) or _created_from_name(
                name, self._prefix,
            )
            out.append((name, created))
        _sort_newest_first(out, self._prefix)
        return out

    async def list_snapshots(self) -> list[str]:
        """List all guardian snapshots, newest first."""
        return [name for name, _ in await self._list_snapshots_with_meta()]

    async def prune(self) -> int:
        """Prune guardian snapshots. Returns count deleted.

        Only NON-healthy snapshots; two independent rules, unioned:
        - **Retention**: keep the newest ``retention`` non-healthy snapshots;
          delete the rest.
        - **Age**: delete any non-healthy snapshot older than ``max_age_days``
          — *even if it is the newest*. This is the incident backstop: a stale
          ``guardian-pre-recovery`` snapshot would otherwise be protected by
          retention forever, accumulating CoW divergence.

        Healthy (rollback) snapshots are never pruned. The newest is the
        offline lifeline; an older one is the fallback if the next refresh is
        refused. They leave only via ``delete_healthy``: rotation after a
        successful create, pool relief on a probe-confirmed healthy tick, or
        delete-first. (Consequence, stated: a superseded healthy snapshot whose
        rotation delete keeps failing is bounded only by the next successful
        rotation, relief under pressure, and incus's own expiry where it
        applies.)
        """
        meta = await self._list_snapshots_with_meta()
        if not meta:
            return 0

        # Healthy snapshots are never pruned — not even a superseded one: it is
        # the fallback if the next refresh is refused and delete-first takes the
        # newer one (review: prune removed it right before such a refresh).
        # They are rotated by mark_healthy after a successful create, and freed
        # by pool relief under pressure, both through delete_healthy().
        meta = [(n, c) for n, c in meta if not n.endswith(_HEALTHY_SUFFIX)]
        names = [name for name, _ in meta]

        # Retention rule: keep the newest N non-healthy snapshots.
        to_keep = set(names[:self._retention])
        to_delete = {n for n in names if n not in to_keep}

        # Age rule: delete stale snapshots, including a stale "newest"
        # non-healthy snapshot that retention keeps.
        max_age_days = self._config.snapshots.max_age_days
        if max_age_days > 0:
            cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
            for name, created in meta:
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
        delete_first_allowed: bool = False,
        reserve_settle: Callable[[], bool] | None = None,
        healthy_confirmed: bool = False,
    ) -> str | None:
        """Take a 'healthy' snapshot, then rotate superseded healthy ones.

        Create-then-delete ordering by default: the new lifeline must exist
        before the old one goes, so an ordinary failed create leaves the
        previous healthy snapshot intact with no zero-lifeline window.

        EXCEPT when all of these hold:

        * the POOL refused the create, on a real measurement — the guardian's
          own gate (``REFUSED_POOL_GATE``) or LVM's "free space in thin pool
          reached threshold" (``REFUSED_POOL_SPACE``); never a probe failure;
        * the newest lifeline is at least a rotation interval old;
        * LVM MEASURES that the healthy snapshots hold at least
          ``_SNAPSHOT_ONLY_MIN_*`` no live volume maps, so deleting frees it
          (``_lifeline_holds_space``; LVM-thin only);
        * the caller passes ``delete_first_allowed`` — pool relief is live
          (not ``alert_only``/``off``, not killed by GUARDIAN_POOL_RELIEF_DISABLED,
          valid config) and is not settling after its own action;
        * ``reserve_settle`` (when given) succeeds: it persists relief's settle
          stamp BEFORE the delete, so relief cannot delete again on the next
          tick if the state file turns out to be unwritable;
        * ``healthy_confirmed``: this tick's probe found the container HEALTHY
          (the delete goes through the ``delete_healthy`` chokepoint);
        * the create attempt did not already delete a snapshot (retention
          eviction) whose space may still be coming back.

        Then create-first cannot succeed while that snapshot keeps filling the
        pool — retrying it daily is how one snapshot once survived a week and
        filled the pool to 100% — so the stale lifeline is deleted FIRST. The
        zero-lifeline window is seconds, or lasts until the pool recovers if the
        retry is still refused; either is recorded in ``last_rotation_note``.

        A pool that is FULL of live data also refuses the create; there the old
        snapshot holds little, so it is kept (no evidence, no delete). Pool
        relief (pool_relief.py) acts if the pool then runs short.
        """
        self.last_rotation_note = None
        self.last_rotation_deleted = False
        name = await self.take(
            label=_HEALTHY_LABEL, snapshot_size_history=snapshot_size_history,
        )
        if (
            name is None
            and delete_first_allowed
            and self.last_refusal in (REFUSED_POOL_GATE, REFUSED_POOL_SPACE)
        ):
            refusal = self.last_refusal
            stale = await self._stale_lifeline(datetime.now(UTC))
            held = await self._lifeline_holds_space() if stale is not None else None
            deleted = False
            if self.last_take_evicted:
                logger.info("delete-first deferred: this attempt already deleted a snapshot")
            elif (
                stale is not None
                and held is not None
                and healthy_confirmed
                and (reserve_settle is None or reserve_settle())
            ):
                deleted = await self.delete_healthy(stale, healthy_confirmed=True)
                if not deleted and self.last_delete_error == "timeout":
                    # The client gave up; the daemon may still finish the
                    # delete. Say so rather than retry as if nothing happened.
                    self.last_rotation_note = (
                        f"pool refused the create ({refusal}); deleting the lifeline "
                        f"{stale} did not confirm (the client timed out) — the rollback "
                        "lifeline may be gone"
                    )
                    return None
            if deleted:
                self.last_rotation_deleted = True
                logger.warning(
                    "Pool refused the healthy snapshot (%s): deleted lifeline %s first "
                    "(snapshots held >= %.1f GiB no live volume maps), retrying the create",
                    refusal, stale, held / 1024**3,
                )
                name = await self.take(
                    label=_HEALTHY_LABEL, snapshot_size_history=snapshot_size_history,
                )
                if name:
                    outcome = f"created {name}"
                else:
                    # When a failed rotation had left two healthy snapshots,
                    # the older one went and the newer still stands.
                    remaining = await self.get_latest_healthy()
                    outcome = f"the retry was refused too ({self.last_refusal}) — " + (
                        f"rollback falls back to {remaining}" if remaining
                        else "NO rollback lifeline until the pool recovers"
                    )
                self.last_rotation_note = (
                    f"pool refused the create ({refusal}); the healthy snapshots held at "
                    f"least {held / 1024**3:.1f} GiB no live volume maps, so the lifeline "
                    f"{stale} was deleted first, then {outcome}"
                )
        if name is None:
            return None

        for old_name in await self.list_snapshots():
            is_superseded = old_name.endswith(_HEALTHY_SUFFIX) and old_name != name
            if is_superseded and await self.delete_healthy(old_name, replaced_by=name):
                logger.info("Rotated superseded healthy snapshot: %s", old_name)
        return name

    async def _lifeline_holds_space(self) -> float | None:
        """Bytes the healthy snapshots MEASURABLY hold alone, or None.

        None — no evidence, keep the lifeline — unless the pool is an LVM thin
        pool identified by name and the lower bound from
        :func:`pool.snapshot_only_bytes` reaches the floor. Any other backend,
        an unidentified pool, a non-guardian snapshot (or any other volume)
        whose mapping cannot be read, or a failed ``lvs`` → None.
        """
        try:
            status = await measure_storage_pool(self._config)
        except Exception:
            logger.warning("could not measure the pool for delete-first", exc_info=True)
            return None
        if (
            not status.detected or not status.vg_name or not status.thinpool_lv
            or not status.pool_size_bytes or status.data_pct is None
        ):
            return None
        meta = await self.list_snapshot_meta_strict()
        if not meta:
            return None
        snapshot_lvs = {
            incus_snapshot_lv_name(self._container, n)
            for n, _ in meta if n.endswith(_HEALTHY_SUFFIX)
        }
        rc, out, err = await _run_subprocess(
            "sudo", "-n", "lvs", "--reportformat", "json", "--nosuffix", "--units", "b",
            "-S", THIN_LV_SELECT, "-o", ",".join(THIN_LV_FIELDS), status.vg_name,
            timeout=10.0,
        )
        if rc != 0:
            logger.warning("lvs (thin volumes) failed for delete-first: %s", err[:120])
            return None
        held = snapshot_only_bytes(
            out, status.thinpool_lv, incus_container_lv_name(self._container), snapshot_lvs,
        )
        floor = max(_SNAPSHOT_ONLY_MIN_BYTES, _SNAPSHOT_ONLY_MIN_FRAC * status.pool_size_bytes)
        return held if held is not None and held >= floor else None

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
