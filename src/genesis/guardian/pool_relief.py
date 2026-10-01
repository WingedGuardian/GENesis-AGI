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

Relief also acts EARLY, from the pool's MEASURED growth (``pool_runway``):
when data or metadata would fill within ``early_horizon_hours``. Early relief
has less authority than the reserve. It may take pre-recovery snapshots,
superseded healthy ones, and the rollback lifeline only once it is older than
``lifeline_max_age_hours``; a young lifeline is only ever taken at the
reserve. A rate read from step-shaped samples can be wrong, so a wrong one may
cost an expendable snapshot, never the one recovery needs.

Relief only ever deletes snapshots with the names the guardian generates
(``SnapshotManager.list_snapshot_meta_strict``), and never deletes anything
else; if the pool keeps filling after every guardian snapshot is gone, it says
so in a CRITICAL alert. Its one other mutation is ``pool_extend``: growing an
LVM thin pool that carries the install's autoextend profile into VG space
LVM's own autoextend cannot use, never while metadata is what runs short.
After a successful extend VG free is down to the keep, so it does not extend
again until space is added; a failed one backs off 24h.

Fail-closed rules (each one a review finding):

* an invalid configuration → alert-only, one warning a day;
* an undetected or ambiguous pool (unknown backend, several thin pools in the
  VG) → no action, and a WARNING once that has lasted an hour;
* the pool is re-measured immediately before every mutation: it must be the
  same, still-nameable pool AND still under pressure, at a level that still
  allows the target (eased → ``eased``, nothing deleted); the alert then
  reports that fresh measurement;
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
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from genesis.guardian.pool import StoragePoolStatus
from genesis.guardian.pool_runway import (
    HISTORY_FILE,
    PoolSample,
    Runway,
    _bad_num,
    compute_runway,
    early_allowed,
    early_lifeline_ok,
    early_reason,
    gap_threshold,
    load_history,
    record_sample,
    sample_from_status,
    validate_early_config,
)
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
        return _bad_num(cfg, name, lo, hi)

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
    """Stable identity of the measured pool, or None when unknown.

    LVM pools are named by VG and thin-pool LV. A btrfs/dir pool has neither,
    so its backing source is part of the identity: a pool recreated on other
    storage under the same incus name must not share history or pass a
    pre-delete re-check as the same pool (review).
    """
    if not status.pool_name:
        return None
    key = f"{status.pool_name}|{status.vg_name or ''}|{status.thinpool_lv or ''}"
    if not status.vg_name and status.pool_source:
        key = f"{key}|{status.pool_source}"
    return key


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


def _data_short(status: StoragePoolStatus, cfg) -> str | None:
    data_pct = status.data_pct if status.data_pct is not None else status.pool_used_pct
    if data_pct is not None:
        free_pct = 100.0 - data_pct
        if free_pct <= cfg.min_reserve_pct:
            return (
                f"free data space {free_pct:.1f}% is at or below the "
                f"{cfg.min_reserve_pct:g}% reserve"
            )
    return None


def _meta_short(status: StoragePoolStatus, cfg) -> str | None:
    if status.metadata_pct is not None:
        free_meta = 100.0 - status.metadata_pct
        if free_meta <= cfg.min_meta_reserve_pct:
            return (
                f"free metadata space {free_meta:.1f}% is at or below the "
                f"{cfg.min_meta_reserve_pct:g}% reserve"
            )
    return None


def shortfall(status: StoragePoolStatus, cfg) -> str | None:
    """Why the pool is at or below a reserve (the reason text), or None.

    Equality counts: the reserve exists to absorb one burst, so a pool with
    exactly the reserve left has none to spare.
    """
    return _data_short(status, cfg) or _meta_short(status, cfg)


# Two levels of pressure. RESERVE (at or below a reserve) may free any
# guardian snapshot, the rollback lifeline last. EARLY (the measured growth
# fills the pool within early_horizon_hours) has less authority: a young
# lifeline is never its target, because a rate read from step-shaped samples
# can be wrong and the lifeline is the one snapshot recovery needs.
LEVEL_RESERVE = "reserve"
LEVEL_EARLY = "early"
_NO_RUNWAY = Runway(None, None, None, None)


@dataclass(frozen=True)
class Pressure:
    level: str | None  # LEVEL_RESERVE, LEVEL_EARLY or None
    reason: str | None
    runway: Runway


def runway_for(status: StoragePoolStatus, cfg, history: list[PoolSample], now: datetime) -> Runway:
    """The runway of ``status`` against ``history``; unknown on any error."""
    try:
        sample = sample_from_status(status, now, pool_key(status))
        if sample is None:
            return _NO_RUNWAY
        return compute_runway(history, sample, gap_threshold(cfg.history_sample_interval_s))
    except Exception:
        logger.warning("pool runway computation failed", exc_info=True)
        return _NO_RUNWAY


def assess(
    status: StoragePoolStatus, cfg, history: list[PoolSample], now: datetime, *, early: bool = True,
) -> Pressure:
    """The ONE pressure rule for the decision and every pre-mutation re-check.

    ``early`` False (an invalid early-level configuration) leaves only the
    reserve rule.
    """
    runway = runway_for(status, cfg, history, now) if early else _NO_RUNWAY
    reason = shortfall(status, cfg)
    if reason is not None:
        return Pressure(LEVEL_RESERVE, reason, runway)
    reason = early_reason(runway, cfg.early_horizon_hours) if early else None
    if reason is not None:
        return Pressure(LEVEL_EARLY, reason, runway)
    return Pressure(None, None, runway)


def extend_plan(status: StoragePoolStatus, cfg, p: Pressure) -> int | None:
    """Bytes the LVM partial extend would grow the pool by now, or None.

    Autoextend's own trigger (``pool_extend.plan_extend``), not the growth
    estimate: a thin pool cannot shrink, so this never hangs on a rate
    (review). The one pressure input is metadata: the extend grows only the
    data LV, so it never spends the pass while metadata is short at its
    reserve or by its measured growth.
    """
    from genesis.guardian.pool_extend import plan_extend

    if _meta_short(status, cfg) is not None:
        return None
    meta_only = Runway(None, None, p.runway.meta_rate, p.runway.meta_hours)
    if early_reason(meta_only, cfg.early_horizon_hours) is not None:
        return None
    return plan_extend(status, cfg.extend_keep_free_mib)


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


async def _recheck_before_delete(
    config, status: StoragePoolStatus, cfg, history: list[PoolSample], now: datetime,
    *, early: bool = True, extend: bool = False,
) -> tuple[str | None, StoragePoolStatus | None, Pressure | None]:
    """Re-measure immediately before a mutation: ``(stop, fresh, pressure)``.

    ``stop`` is None — go ahead, with ``fresh`` the measurement to report and
    ``pressure`` its assessment — only when the pool is still the one the
    decision was made on AND it is still under pressure: a shortfall that
    eased in between (an autoextend landing, space freed elsewhere) is no
    longer a reason to act (review). Otherwise ``stop`` is ``"pool_changed"``
    (unmeasurable now, or a different pool) or ``"eased"``. The caller checks
    that its target is still allowed at the fresh level. With ``extend`` the
    pool need not be under pressure (the extend has autoextend's own trigger,
    which the caller re-plans from ``fresh``); identity must still hold.
    """
    from genesis.guardian.pool import measure_storage_pool

    try:
        again = await measure_storage_pool(config)
    except Exception:
        logger.warning("pre-delete pool re-check failed", exc_info=True)
        return "pool_changed", None, None
    key = pool_key(status)
    if key is None or unactionable(again) is not None or pool_key(again) != key:
        return "pool_changed", None, None
    p = assess(again, cfg, history, now, early=early)
    if p.level is None and not extend:
        logger.info("pool relief: the shortfall eased before the delete (%s)", _numbers(again))
        return "eased", None, None
    return None, again, p


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


def _record_history(config, status: StoragePoolStatus, now: datetime) -> list[PoolSample]:
    """Record this measurement in the bounded pool history; return the history."""
    cfg = config.storage_pool
    path = config.state_path / HISTORY_FILE
    try:
        sample = sample_from_status(status, now, pool_key(status))
        if sample is None:
            return load_history(path)
        return record_sample(
            path,
            sample,
            min_interval_s=cfg.history_sample_interval_s,
            max_samples=cfg.history_max_samples,
        )
    except Exception:
        logger.warning("pool history update failed", exc_info=True)
        return []


def _describe(status: StoragePoolStatus, p: Pressure) -> str:
    """Every figure the decision used, for an alert body."""
    numbers = _numbers(status)
    if p.runway.data_rate is not None or p.runway.meta_rate is not None:
        numbers = f"{numbers}; {p.runway.describe()}"
    return numbers


# After a failed or unconfirmed extend, wait before trying again: the space is
# permanent, and a wedged LVM must not be retried on every settle.
_EXTEND_BACKOFF_HOURS = 24


def _backed_off(state: dict, key: str, now: datetime, hours: float) -> bool:
    """An extend backoff is still running. Unlike ``_due``, a stamp in the
    FUTURE (the clock stepped back) keeps it running: retrying a permanent
    mutation early is not the safe direction (review)."""
    raw = state.get(key)
    if not raw:
        return False
    try:
        last = datetime.fromisoformat(raw)
        return last > now or now - last < timedelta(hours=hours)
    except (TypeError, ValueError):
        return False


async def _maybe_extend(
    config, dispatcher, status: StoragePoolStatus, p: Pressure, grow: int,
    history: list[PoolSample], state_path: Path, state: dict, now: datetime, run,
    fresh_out: dict | None = None,
) -> str | None:
    """Run the LVM partial extend (pool_extend). Returns an outcome that ends
    the pass, or None to go on to snapshot deletion.

    Only as this pass's single action: the settle is stamped right before
    ``lvextend``, after a re-measure shows the same pool and a fresh plan, and
    the extend never exceeds that fresh plan (review). An unreadable extent
    size stamps nothing and relief goes on to delete (retrying the read after
    an hour). A re-measure that shows a changed pool, or no plan, ends the pass
    (``extend_stopped``): the delete path would stop on the same re-measure.
    The alert reports the fresh measurement.
    """
    from genesis.guardian.alert.base import AlertSeverity
    from genesis.guardian.pool_extend import (
        GUARD_STOP,
        TIMED_OUT,
        autoextend_reason,
        extend_thinpool,
    )

    cfg = config.storage_pool
    if _backed_off(state, "extend_backoff", now, _EXTEND_BACKOFF_HOURS):
        return None
    if _backed_off(state, "extend_read_backoff", now, 1):
        return None
    seen = {"status": status, "p": p, "plan_gone_under_pressure": False}

    async def before_mutation() -> int | None:
        stop, fresh, fp = await _recheck_before_delete(
            config, status, cfg, history, now, extend=True,
        )
        if stop is not None:
            return None
        fresh_grow = extend_plan(fresh, cfg, fp)
        if fresh_grow is None:
            # The extend no longer applies (metadata went short, the profile
            # was withdrawn), but the pool may still need relief: that must
            # not be suppressed by the extend (review).
            seen["plan_gone_under_pressure"] = fp.level is not None
            if fresh_out is not None and fp.level is not None:
                # The caller relieves on THIS measurement, which may be early
                # pressure the pass did not see when it started (review).
                fresh_out["status"], fresh_out["p"] = fresh, fp
            return None
        if not _stamp(state_path, state, "last_action", now):
            return None
        seen["status"], seen["p"] = fresh, fp
        return fresh_grow

    if run is None:
        from genesis.guardian._subprocess import run_subprocess as run
    ok, attempted, detail = await extend_thinpool(status, grow, run, before_mutation)
    if attempted:
        # Every issued mutation of the host's storage is recorded where the
        # provisioning flow records its own (the owner-visible audit trail),
        # verified or not: a timed-out extend may well have landed.
        try:
            from genesis.guardian.provisioning.ledger import ProvisioningLedger

            ProvisioningLedger(config.state_dir).record_action(
                "pool_extend", f"{status.vg_name}/{status.thinpool_lv}: {detail}", ok, ok,
            )
        except Exception:
            logger.warning("could not record the pool extend in the ledger", exc_info=True)
    # Report what the mutation acted on: the fresh re-measure (review).
    reason = seen["p"].reason or autoextend_reason(seen["status"])
    numbers = _describe(seen["status"], seen["p"])
    if ok:
        await _send(
            dispatcher,
            AlertSeverity.CRITICAL,
            "Guardian extended the thin pool",
            f"{reason}. {numbers}. {detail}, into VG space LVM's autoextend could not "
            "use. A thin pool cannot shrink: grow the VM disk to restore autoextend "
            "headroom (docs/reference/thin-pool-recovery.md).",
        )
        return "extended"
    if detail == GUARD_STOP:
        if seen["plan_gone_under_pressure"]:
            return None  # go on to snapshot relief; it re-measures before deleting
        return "extend_stopped"
    if not attempted:
        # An unreadable extent size: no mutation, so relief goes on to delete;
        # the read is retried in an hour, not on every pass (review).
        _stamp(state_path, state, "extend_read_backoff", now)
        logger.warning("pool extend skipped before the mutation: %s", detail)
        return None
    _stamp(state_path, state, "extend_backoff", now)
    if detail == TIMED_OUT:
        # The pool may have grown; nothing else is done until a fresh pass
        # re-measures (the settle is already stamped).
        await _send(
            dispatcher,
            AlertSeverity.CRITICAL,
            "Guardian extend outcome unknown",
            f"{reason}. {numbers}. {detail}. No snapshot is deleted this pass; the "
            "next pass re-measures (sudo lvs <vg> shows the pool size).",
        )
        return "extend_indeterminate"
    if _throttled(state_path, state, "extend_failed", now, cfg.realert_hours):
        await _send(
            dispatcher,
            AlertSeverity.WARNING,
            "Guardian could not extend the thin pool",
            f"{reason}. {numbers}. {detail}. Not retried for "
            f"{_EXTEND_BACKOFF_HOURS}h; freeing guardian snapshots instead if the "
            "pool is short.",
        )
    return None


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
    run=None,
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
    early_problem = validate_early_config(config)
    early_ok = early_problem is None
    if not early_ok and _throttled(state_path, state, "early_invalid_config", now, 24):
        await _send(
            dispatcher,
            AlertSeverity.WARNING,
            "Early pool relief off",
            f"Invalid configuration: {early_problem}. Early relief and the LVM extend "
            "are off until it is fixed; the reserve and delete-first rotation still run.",
        )
    history = _record_history(config, status, now) if early_ok else []
    p = assess(status, cfg, history, now, early=early_ok)
    grow = extend_plan(status, cfg, p) if early_ok else None
    if p.level is None and grow is None:
        return "ok"
    early = p.level == LEVEL_EARLY
    severity = AlertSeverity.WARNING if early else AlertSeverity.CRITICAL

    if mode == "alert_only":
        if p.level is None:
            # An extend is an action and alert_only never acts, but the
            # operator should hear that relief would have grown the pool.
            if _throttled(state_path, state, "would_extend", now, cfg.realert_hours):
                from genesis.guardian.pool_extend import autoextend_reason

                await _send(
                    dispatcher,
                    AlertSeverity.WARNING,
                    "Pool at LVM's autoextend threshold — relief is alert-only",
                    f"{autoextend_reason(status)}. {_describe(status, p)}. Relief would grow "
                    "the pool into that VG space now, but storage_pool.relief_mode / "
                    "GUARDIAN_POOL_RELIEF_DISABLED keeps it off.",
                )
            return "alert_only"
        # Keyed per level: an early WARNING must not mute the reserve's
        # CRITICAL for realert_hours (review).
        if _throttled(state_path, state, f"would_act_{p.level}", now, cfg.realert_hours):
            await _send(
                dispatcher,
                severity,
                "Pool filling — relief is alert-only" if early
                else "Pool short of space — relief is alert-only",
                f"{p.reason}. {_describe(status, p)}. Relief would act now, but "
                "storage_pool.relief_mode / GUARDIAN_POOL_RELIEF_DISABLED keeps it off.",
            )
        return "alert_only"

    if not _due(state, "last_action", now, _SETTLE.total_seconds() / 3600.0):
        return "settling"

    if grow is not None:
        fresh_out: dict = {}
        extended = await _maybe_extend(
            config, dispatcher, status, p, grow, history, state_path, state, now, run,
            fresh_out=fresh_out,
        )
        if extended is not None:
            return extended
        if fresh_out:
            status, p = fresh_out["status"], fresh_out["p"]
            early = p.level == LEVEL_EARLY
            severity = AlertSeverity.WARNING if early else AlertSeverity.CRITICAL
    if p.level is None:
        return "ok"
    reason, numbers = p.reason, _describe(status, p)

    meta = await snapshots.list_snapshot_meta_strict()
    if meta is None:
        if _throttled(state_path, state, f"list_failed_{p.level}", now, cfg.realert_hours):
            await _send(
                dispatcher,
                severity,
                "Pool short of space — cannot list snapshots",
                f"{reason}. {numbers}. `incus snapshot list` failed, so the guardian "
                "cannot tell what it could free. Check incus on the host.",
            )
        return "list_failed"
    order = plan_order([SnapshotInfo(n, c, n.endswith(HEALTHY_SUFFIX)) for n, c in meta])
    # meta is newest-first, so the first healthy name is the rollback lifeline.
    lifeline = next((n for n, _ in meta if n.endswith(HEALTHY_SUFFIX)), None)
    lifeline_created = next((c for n, c in meta if n == lifeline), None)
    held_for_recovery = False
    if not healthy_confirmed and any(n.endswith(HEALTHY_SUFFIX) for n in order):
        order = [n for n in order if not n.endswith(HEALTHY_SUFFIX)]
        held_for_recovery = True
        if not order and not alert_when_deferred:
            return "healthy_deferred"
        if not order and not early:
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

    # Only measured when early relief is what decides (it costs host
    # subprocesses). At the reserve it stays False: a pass that eases to early
    # before its delete then stops rather than take the lifeline (review).
    lifeline_ok = early and lifeline in order and await early_lifeline_ok(
        status, snapshots, lifeline_created, now, cfg.lifeline_max_age_hours,
    )

    def allowed(level: str) -> list[str]:
        if level == LEVEL_RESERVE:
            return order
        return early_allowed(order, lifeline, lifeline_ok)

    targets = allowed(p.level)
    if not targets:
        if early:
            if held_for_recovery:
                why = (
                    "The rollback snapshots are kept while the container is not "
                    "healthy, because recovery may need them."
                )
            else:
                why = (
                    "Early relief frees only pre-recovery snapshots, superseded healthy "
                    "ones and a rollback lifeline older than "
                    f"{cfg.lifeline_max_age_hours:g}h that LVM shows holding space, and "
                    "none qualifies."
                )
            if _throttled(state_path, state, "early_no_target", now, cfg.realert_hours):
                await _send(
                    dispatcher,
                    AlertSeverity.WARNING,
                    "Pool filling — nothing the guardian may free yet",
                    f"{reason}. {numbers}. {why} Relief acts on the reserve if the pool "
                    "gets there; growing the pool or freeing space now avoids that "
                    "(docs/reference/thin-pool-recovery.md).",
                )
            return "early_no_target"
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

    async def recheck(target: str) -> tuple[str | None, str, str, bool]:
        """Re-measure; stop unless ``target`` is still allowed at the fresh level."""
        stop, fresh, fp = await _recheck_before_delete(
            config, status, cfg, history, now, early=early_ok,
        )
        if stop is not None:
            return stop, "", "", False
        if target not in allowed(fp.level):
            # Eased from the reserve to early pressure, and this target (a young
            # lifeline) is not early relief's to take.
            return "eased", "", "", False
        return None, fp.reason, _describe(fresh, fp), fp.level == LEVEL_EARLY

    stop, reason, numbers, early = await recheck(targets[0])
    if stop is not None:
        return stop
    if not _stamp(state_path, state, "last_action", now):
        # The settle stamp could not persist: deleting now would let the next
        # tick delete again at once. Stop; the tier alerts still report.
        return "state_unwritable"
    failed: list[str] = []
    for target in targets:
        if failed and target == lifeline:
            # Never fall through TO the lifeline: an earlier failure may be the
            # daemon still deleting another snapshot (a re-delete while one is
            # in flight can fail with a non-timeout error), so the space may be
            # coming back already (review).
            break
        if failed:
            # The failed delete can have taken minutes (incus's client waits),
            # long enough for the pool to recover. Every delete, not only the
            # first, acts on a measurement taken just before it (review).
            stop, reason, numbers, early = await recheck(target)
            if stop is not None:
                return stop
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
            # Taking the rollback lifeline is CRITICAL at either level (review).
            AlertSeverity.WARNING if early and target != lifeline else AlertSeverity.CRITICAL,
            "Guardian freed pool space early" if early else "Guardian freed pool space",
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
