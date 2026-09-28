"""Storage-pool RELIEF — HOST-SIDE.

``pool.py`` measures the pool and alerts in tiers; this module frees space the
guardian owns when the pool is about to run out.

Why it exists: a thin pool once filled to 100% (container rootfs forced
read-only) while CRITICAL alerts fired every six hours for four days. The
space was held by the guardian's own healthy snapshot, which nothing was
allowed to delete. Alerts are not a mechanism. This is.

Two layers stop that incident, and this module is the second:

1. ``SnapshotManager.mark_healthy`` rotates delete-first when a measured pool
   refusal meets a lifeline that LVM shows holds space nothing live maps. That
   ends the refused-rotation loop at its first day (LVM-thin only).
2. Relief (here) is the backstop for everything else: when free DATA space is
   at or below ``min_reserve_pct`` of the pool, or free METADATA space at or
   below ``min_meta_reserve_pct`` of the metadata LV, it deletes ONE
   guardian-owned snapshot per pass — pre-recovery snapshots oldest first,
   then superseded healthy ones, the rollback lifeline last. After acting it
   waits ``_SETTLE`` before the next action, so each effect is re-measured
   first (btrfs frees a deleted subvolume asynchronously).

Relief only ever deletes snapshots with the names the guardian generates
(``SnapshotManager.list_snapshot_meta_strict``). It never grows the pool, and
never deletes anything else; if the pool keeps filling after every guardian
snapshot is gone, it says so in a CRITICAL alert.

Fail-closed rules (each one a review finding):

* an invalid configuration → alert-only, one warning a day;
* an undetected or ambiguous pool (unknown backend, several thin pools in the
  VG) → no action, and a WARNING once that has lasted an hour;
* the pool's identity is re-checked immediately before the delete (the same
  pool, still nameable — not a re-check of the shortfall);
* a delete that fails falls through to the next snapshot in the plan, so one
  undeletable snapshot cannot pin relief; still at most one delete per pass;
* the settle stamp is persisted BEFORE the delete — if it cannot be written,
  nothing is deleted (the next tick would otherwise delete again at once);
* every throttled alert is sent only when its stamp persisted, so an
  unwritable state dir cannot page on every 30s tick.
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from genesis.guardian.pool import StoragePoolStatus
from genesis.guardian.snapshots import HEALTHY_SUFFIX
from genesis.util.atomic import atomic_write_text

logger = logging.getLogger(__name__)

STATE_FILE = "pool_relief_state.json"
RELIEF_MODES = ("live", "alert_only", "off")
# Wait after an action before the next one: long enough for a deleted btrfs
# subvolume's space to come back and be re-measured, short against a fill
# that takes hours.
_SETTLE = timedelta(minutes=5)


# --- configuration -----------------------------------------------------------


def effective_relief_mode(cfg) -> str:
    """The relief mode after the env kill switch; invalid → alert_only.

    Invalid degrades toward LESS authority (alert, never act), not toward off:
    a typo must not silently remove the alerts too. YAML 1.1 reads a bare
    ``off`` as boolean False, so False means off.
    """
    raw = cfg.relief_mode
    if raw is False:
        return "off"
    mode = raw if raw in RELIEF_MODES else "alert_only"
    killed = os.environ.get("GUARDIAN_POOL_RELIEF_DISABLED", "").strip().lower()
    if mode == "live" and killed in ("1", "true", "yes", "on"):
        return "alert_only"
    return mode


def validate_relief_config(config) -> str | None:
    """Why the relief configuration cannot be trusted to ACT, or None.

    ``_build_sub`` copies YAML values without coercing or bounding them, and
    these values steer automatic deletes: ``min_reserve_pct: 300`` would make
    every pool look short, an empty ``snapshots.prefix`` would make every
    timestamp-shaped snapshot "ours".
    """
    cfg = config.storage_pool

    def num(name: str, lo: float, hi: float) -> str | None:
        v = getattr(cfg, name)
        if (
            isinstance(v, bool)
            or not isinstance(v, (int, float))
            or not math.isfinite(v)
            or not lo <= v <= hi
        ):
            return f"storage_pool.{name}={v!r} (expected a number in [{lo}, {hi}])"
        return None

    if not isinstance(cfg.enabled, bool):
        # _build_sub does not coerce: `enabled: "false"` is a truthy string.
        return f"storage_pool.enabled={cfg.enabled!r} (expected true or false)"
    for problem in (
        # The high tiers gate snapshot creation; a refusal there licenses
        # delete-first, so a malformed one (e.g. -1: refuse everything) must
        # not pose as pool pressure (review).
        num("data_high_pct", 1, 100),
        num("metadata_high_pct", 1, 100),
        num("min_reserve_pct", 0, 50),
        num("min_meta_reserve_pct", 0, 50),
        num("realert_hours", 0.1, 24 * 30),
    ):
        if problem:
            return problem
    prefix = config.snapshots.prefix
    if not isinstance(prefix, str) or not prefix.strip():
        return f"snapshots.prefix={prefix!r} (must be a non-empty namespace)"
    return None


# --- state -------------------------------------------------------------------


def _load_state(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text())
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(path: Path, state: dict) -> bool:
    """Persist relief state; False when it could not be written."""
    try:
        # mkstemp-based: 0600, and the temp never outlives a failed write.
        atomic_write_text(path, json.dumps(state))
    except OSError:
        logger.warning("could not persist pool relief state %s", path, exc_info=True)
        return False
    return True


def _due(state: dict, key: str, now: datetime, hours: float) -> bool:
    raw = state.get(key)
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(raw)
        return now - last >= timedelta(hours=hours) or last > now
    except (TypeError, ValueError):
        # Unparseable, or a timezone-less stamp (a hand edit) that cannot be
        # compared with an aware now: due, never a crash.
        return True


def _stamp(path: Path, state: dict, key: str, now: datetime) -> bool:
    """Persist ``key = now``; update ``state`` only when the write succeeded."""
    stamped = dict(state)
    stamped[key] = now.isoformat()
    if not _save_state(path, stamped):
        return False
    state.update(stamped)
    return True


def _throttled(path: Path, state: dict, key: str, now: datetime, hours: float) -> bool:
    """An alert keyed ``key`` is due AND its stamp persisted (else not sent)."""
    return _due(state, key, now, hours) and _stamp(path, state, key, now)


async def _send(dispatcher, severity, title: str, body: str) -> None:
    from genesis.guardian.alert.base import Alert

    try:
        await dispatcher.send(Alert(severity=severity, title=title, body=body))
    except Exception:
        logger.warning("pool relief alert dispatch failed", exc_info=True)


# --- measurement ---------------------------------------------------------------


def pool_key(status: StoragePoolStatus) -> str | None:
    """Stable identity of the measured pool, or None when unknown."""
    if not status.pool_name:
        return None
    return f"{status.pool_name}|{status.vg_name or ''}|{status.thinpool_lv or ''}"


def _ambiguous_pool(status: StoragePoolStatus) -> bool:
    """An LVM pool whose thin-pool LV is unnamed (several thin pools in the VG):
    ANY percentage present may belong to a different pool than the
    container's — a metadata-only reading is exactly as unattributable as a
    data one (review). Relief never acts on a pool it cannot name."""
    return bool(status.vg_name) and not status.thinpool_lv


def unactionable(status: StoragePoolStatus) -> tuple[str, str] | None:
    """(outcome, why) when relief must not act on this measurement, else None.

    The ONE admission rule for both the decision and the pre-delete re-check:
    measured, carrying at least one usage figure, and naming its pool. A
    "detected" status with no figures (lvs answered but reported nothing
    usable) would otherwise read as "no shortfall" forever (review).
    """
    if not status.detected:
        return "unmeasured", f"the storage pool could not be measured ({status.detail or 'no detail'})"
    if status.data_pct is None and status.metadata_pct is None and status.pool_used_pct is None:
        return "no_signal", f"the pool measurement carried no usage figures ({status.detail})"
    if _ambiguous_pool(status) or pool_key(status) is None:
        return "ambiguous_pool", (
            "the pool's thin-pool LV could not be identified (e.g. several thin "
            f"pools in one VG): {status.detail}"
        )
    return None


def shortfall(status: StoragePoolStatus, cfg) -> str | None:
    """Why the pool is at or below a reserve (the reason text), or None.

    Equality counts: the reserve exists to absorb one burst, so a pool with
    exactly the reserve left has none to spare.
    """
    data_pct = status.data_pct if status.data_pct is not None else status.pool_used_pct
    if data_pct is not None:
        free_pct = 100.0 - data_pct
        if free_pct <= cfg.min_reserve_pct:
            return (
                f"free data space {free_pct:.1f}% is at or below the "
                f"{cfg.min_reserve_pct:g}% reserve"
            )
    if status.metadata_pct is not None:
        free_meta = 100.0 - status.metadata_pct
        if free_meta <= cfg.min_meta_reserve_pct:
            return (
                f"free metadata space {free_meta:.1f}% is at or below the "
                f"{cfg.min_meta_reserve_pct:g}% reserve"
            )
    return None


def _numbers(status: StoragePoolStatus) -> str:
    parts = []
    if status.data_pct is not None:
        parts.append(f"data {status.data_pct:.1f}%")
    elif status.pool_used_pct is not None:
        parts.append(f"pool used {status.pool_used_pct:.1f}%")
    if status.metadata_pct is not None:
        parts.append(f"metadata {status.metadata_pct:.1f}%")
    if status.pool_size_bytes:
        parts.append(f"pool {status.pool_size_bytes / 1024**3:.1f}G")
    if status.vg_free_bytes is not None:
        parts.append(f"VG free {status.vg_free_bytes / 1024**3:.1f}G")
    return ", ".join(parts)


async def _same_pool_now(config, status: StoragePoolStatus) -> bool:
    """Re-measure immediately before the delete: the pool must still be the
    one the decision was made on (identity only — a shortfall that eased in
    between still lets this pass free one snapshot)."""
    from genesis.guardian.pool import measure_storage_pool

    try:
        again = await measure_storage_pool(config)
    except Exception:
        logger.warning("pre-delete pool re-check failed", exc_info=True)
        return False
    key = pool_key(status)
    return key is not None and unactionable(again) is None and pool_key(again) == key


# --- planning ------------------------------------------------------------------


@dataclass(frozen=True)
class SnapshotInfo:
    name: str
    created: datetime | None
    healthy: bool


def plan_order(snaps: list[SnapshotInfo]) -> list[str]:
    """Guardian snapshots in the order relief may delete them.

    Pre-recovery (non-healthy) snapshots oldest first — they are captures of
    an already-broken state, and recovery.py ranks them the same way — then
    superseded healthy snapshots (left behind when a rotation's delete
    failed), and the newest healthy snapshot, the rollback lifeline, last.
    """

    def _age_key(s: SnapshotInfo) -> datetime:
        return s.created or datetime.min.replace(tzinfo=UTC)

    others = sorted((s for s in snaps if not s.healthy), key=_age_key)
    healthy = sorted((s for s in snaps if s.healthy), key=_age_key)
    return [s.name for s in others + healthy]


def plan_delete(snaps: list[SnapshotInfo]) -> str | None:
    """The ONE guardian snapshot relief deletes next, or None."""
    order = plan_order(snaps)
    return order[0] if order else None


# --- entry points ----------------------------------------------------------------


def delete_first_allowed(config, now: datetime | None = None) -> bool:
    """Whether mark_healthy may rotate delete-first on this tick.

    Only when relief is live (the kill switch and alert_only stop EVERY
    automatic delete), the configuration is valid, and relief is not settling
    after its own action (the two must not stack two deletes on one tick).
    """
    now = now or datetime.now(UTC)
    cfg = config.storage_pool
    if not cfg.enabled or effective_relief_mode(cfg) != "live":
        return False
    if validate_relief_config(config) is not None:
        return False
    state = _load_state(config.state_path / STATE_FILE)
    return _due(state, "last_action", now, _SETTLE.total_seconds() / 3600.0)


# How long relief must have been unable to act before that is alerted: long
# enough that a one-off probe blip (incus restarting) never pages, short
# against a fill that takes days.
_CANNOT_ACT_GRACE = timedelta(hours=1)


async def _cannot_act(
    state_path: Path,
    state: dict,
    now: datetime,
    dispatcher,
    outcome: str,
    why: str,
) -> str:
    """Relief cannot measure or name the pool: say so once it PERSISTS.

    Without this, a host where relief can never act (lvs not permitted, a VG
    with several thin pools) is silently back to "alerts only" — and the tier
    alerts cannot fire either, since they need the same measurement.
    """
    from genesis.guardian.alert.base import AlertSeverity

    since = None
    raw = state.get("cannot_act_since")
    if raw:
        try:
            since = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            since = None
    if since is not None and since.tzinfo is None:
        since = None  # a timezone-less hand edit: restart the clock, never crash
    if since is None or since > now:
        _stamp(state_path, state, "cannot_act_since", now)
        return outcome
    if now - since >= _CANNOT_ACT_GRACE and _throttled(
        state_path,
        state,
        "cannot_act",
        now,
        24,
    ):
        await _send(
            dispatcher,
            AlertSeverity.WARNING,
            "Pool relief cannot act",
            f"For {(now - since).total_seconds() / 3600:.1f}h {why}. Until that is "
            "fixed the guardian will NOT free pool space on its own, and the storage "
            "tier alerts have no measurement either. Runbook: "
            "docs/reference/thin-pool-recovery.md.",
        )
    return outcome


def record_action(config, now: datetime | None = None) -> bool:
    """Start relief's settle after a delete made elsewhere (delete-first
    rotation), so the two never delete back to back. False if unwritable."""
    path = config.state_path / STATE_FILE
    return _stamp(path, _load_state(path), "last_action", now or datetime.now(UTC))


async def check_pool_relief(
    config,
    dispatcher,
    snapshots,
    *,
    now: datetime | None = None,
    healthy_confirmed: bool = False,
    alert_when_deferred: bool = True,
) -> str:
    """One relief pass. Returns what it did (for logs/tests).

    Healthy (rollback) snapshots are candidates only with ``healthy_confirmed``
    — THIS tick's health probe found the container HEALTHY — and are deleted
    through ``SnapshotManager.delete_healthy``, the one chokepoint for rollback
    targets. ``run_check`` runs a pass BEFORE the recovery cycle with it False
    (the probe has not run yet: a container that failed since the last tick
    still reads HEALTHY there — review, round 3) and a second pass AFTER the
    cycle with the probe's verdict. The pre-cycle pass frees only non-healthy
    snapshots and stays silent when healthy ones are all that is left
    (``alert_when_deferred`` False); the post-cycle pass alerts if it must keep
    them because the container is not healthy.
    """
    from genesis.guardian.alert.base import AlertSeverity
    from genesis.guardian.pool import measure_storage_pool

    now = now or datetime.now(UTC)
    cfg = config.storage_pool
    if not cfg.enabled:
        return "disabled"
    mode = effective_relief_mode(cfg)
    if mode == "off":
        return "off"
    state_path = config.state_path / STATE_FILE
    state = _load_state(state_path)

    problem = validate_relief_config(config)
    if problem is not None:
        if _throttled(state_path, state, "invalid_config", now, 24):
            await _send(
                dispatcher,
                AlertSeverity.WARNING,
                "Pool relief not live",
                f"Invalid relief configuration: {problem}. The guardian will NOT free "
                "pool space on its own until it is fixed; the storage tier alerts "
                "still run.",
            )
        return "invalid_config"
    bad_mode = cfg.relief_mode not in RELIEF_MODES and cfg.relief_mode is not False
    if bad_mode and _throttled(state_path, state, "invalid_mode", now, 24):
        await _send(
            dispatcher,
            AlertSeverity.WARNING,
            "Pool relief not live",
            f"storage_pool.relief_mode={cfg.relief_mode!r} is not one of "
            f"{', '.join(RELIEF_MODES)} — running alert_only (guardian will NOT "
            "free pool space on its own).",
        )

    status = await measure_storage_pool(config)
    blocked = unactionable(status)
    if blocked is not None:
        return await _cannot_act(state_path, state, now, dispatcher, *blocked)
    if state.pop("cannot_act_since", None) is not None:
        _save_state(state_path, state)
    reason = shortfall(status, cfg)
    if reason is None:
        return "ok"
    numbers = _numbers(status)

    if mode == "alert_only":
        if _throttled(state_path, state, "would_act", now, cfg.realert_hours):
            await _send(
                dispatcher,
                AlertSeverity.CRITICAL,
                "Pool short of space — relief is alert-only",
                f"{reason}. {numbers}. Relief would delete guardian snapshots now, but "
                "storage_pool.relief_mode / GUARDIAN_POOL_RELIEF_DISABLED keeps it off.",
            )
        return "alert_only"

    if not _due(state, "last_action", now, _SETTLE.total_seconds() / 3600.0):
        return "settling"

    meta = await snapshots.list_snapshot_meta_strict()
    if meta is None:
        if _throttled(state_path, state, "list_failed", now, cfg.realert_hours):
            await _send(
                dispatcher,
                AlertSeverity.CRITICAL,
                "Pool short of space — cannot list snapshots",
                f"{reason}. {numbers}. `incus snapshot list` failed, so the guardian "
                "cannot tell what it could free. Check incus on the host.",
            )
        return "list_failed"
    order = plan_order([SnapshotInfo(n, c, n.endswith(HEALTHY_SUFFIX)) for n, c in meta])
    # meta is newest-first, so the first healthy name is the rollback lifeline.
    lifeline = next((n for n, _ in meta if n.endswith(HEALTHY_SUFFIX)), None)
    if not healthy_confirmed and any(n.endswith(HEALTHY_SUFFIX) for n in order):
        order = [n for n in order if not n.endswith(HEALTHY_SUFFIX)]
        if not order:
            if not alert_when_deferred:
                return "healthy_deferred"
            if _throttled(state_path, state, "lifeline_protected", now, cfg.realert_hours):
                await _send(
                    dispatcher,
                    AlertSeverity.CRITICAL,
                    "Pool short of space — rollback lifeline kept for recovery",
                    f"{reason}. {numbers}. Only rollback snapshots are left (newest: "
                    f"{lifeline}); they are kept while the container is not healthy, "
                    "because recovery may need it. Grow the pool or free space by hand if "
                    "this persists (docs/reference/thin-pool-recovery.md).",
                )
            return "lifeline_protected"
    if not order:
        if _throttled(state_path, state, "no_target", now, cfg.realert_hours):
            await _send(
                dispatcher,
                AlertSeverity.CRITICAL,
                "Pool filling — nothing left the guardian may free",
                f"{reason}. {numbers}. No guardian snapshots remain. Something other "
                "than guardian snapshots is consuming the pool — the guardian never "
                "deletes what it does not own. Grow the pool or free space by hand "
                "(docs/reference/thin-pool-recovery.md).",
            )
        return "no_target"

    if not await _same_pool_now(config, status):
        return "pool_changed"
    if not _stamp(state_path, state, "last_action", now):
        # The settle stamp could not persist: deleting now would let the next
        # tick delete again at once. Stop; the tier alerts still report.
        return "state_unwritable"
    failed: list[str] = []
    for target in order:
        if failed and target == lifeline:
            # Never fall through TO the lifeline: an earlier failure may be the
            # daemon still deleting another snapshot (a re-delete while one is
            # in flight can fail with a non-timeout error), so the space may be
            # coming back already (review).
            break
        # A snapshot whose delete keeps failing (busy LV, an export in flight)
        # must not pin relief to it forever: fall through to the next one, but
        # still free at most ONE per pass. A failure is only DEFINITE once a
        # re-list shows the snapshot still there and the client did not time
        # out: incus's client giving up says nothing about whether the daemon
        # finished a slow delete (review), so then stop and re-measure.
        if target.endswith(HEALTHY_SUFFIX):
            ok = await snapshots.delete_healthy(target, healthy_confirmed=healthy_confirmed)
        else:
            ok = await snapshots.delete(target)
        if not ok:
            err = getattr(snapshots, "last_delete_error", None)
            after = await snapshots.list_snapshot_meta_strict()
            if after is not None and target not in {n for n, _ in after}:
                pass  # it went after all: count it as this pass's one delete
            elif after is None or err == "timeout":
                failed.append(target)
                if _throttled(state_path, state, "delete_indeterminate", now, 1):
                    await _send(
                        dispatcher,
                        AlertSeverity.CRITICAL,
                        "Guardian delete outcome unknown",
                        f"{reason}. {numbers}. Deleting guardian snapshot {target} did not "
                        "confirm (the client timed out or the snapshot list failed); no "
                        "further snapshot is deleted until a fresh pass re-measures.",
                    )
                return f"delete_indeterminate:{target}"
            else:
                failed.append(target)
                continue
        lifeline_note = (
            " This was the rollback lifeline: SNAPSHOT_ROLLBACK has no target until "
            "the next healthy snapshot."
            if target == lifeline
            else ""
        )
        failed_note = f" (deleting {', '.join(failed)} failed first)" if failed else ""
        await _send(
            dispatcher,
            AlertSeverity.CRITICAL,
            "Guardian freed pool space",
            f"{reason}. {numbers}. Deleted guardian snapshot {target}{failed_note}.{lifeline_note}",
        )
        return f"deleted:{target}"
    if _throttled(state_path, state, "delete_failed", now, 1):
        await _send(
            dispatcher,
            AlertSeverity.CRITICAL,
            "Guardian could not free pool space",
            f"{reason}. {numbers}. Deleting every guardian snapshot failed "
            f"({', '.join(failed)}); retrying after the settle interval.",
        )
    return f"delete_failed:{','.join(failed)}"
