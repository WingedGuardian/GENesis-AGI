"""Provisioning flow — approval ownership split under ONE shared Telegram bot.

Telegram allows exactly one ``getUpdates`` consumer per bot token, and the main
Genesis bot polls continuously while it is up. So approval is owned by whoever
can actually read the user's reply this instant:

  • **Genesis UP → the CONTAINER owns approval.** It approves via its own bot
    (``outreach_send_and_wait``) — zero contention — then invokes the host
    gateway verb, which runs :func:`execute_provisioning_action` (execute-only:
    fresh re-check + execute + ledger, NO Telegram gate).
  • **Genesis DOWN → the GUARDIAN owns approval** via ``getUpdates`` (uncontended
    — the main bot is dead). This is the pool-full → rootfs-RO → Genesis-down
    outage-recovery path. :func:`run_provisioning_flow` is that path: it layers
    the getUpdates gate on top of the shared execute core.

:func:`execute_provisioning_action` is the shared execute-only core used by BOTH
owners AFTER approval:

  1. RE-RUN capacity+gate fresh (state may have moved since approval; also
     enforces the rate cap) — fail ⇒ abort + alert, no mutation
  2. execute exactly once, ledger it (even unverified — it may have landed),
     optionally run in-process storage-expand to absorb a disk grow
  3. result alert — CRITICAL + "no auto-retry" on failure/unverified

:func:`run_provisioning_flow` wraps that core with the getUpdates approval gate
(capacity → gate → propose → APPROVE/DENY → core). Kept import-light and free of
any check.py import so check.py can import THIS for the pool-crit propose hook
without a cycle.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape as html_escape

from genesis.guardian.alert.base import Alert, AlertSeverity
from genesis.guardian.alert.dispatcher import AlertDispatcher
from genesis.guardian.alert.telegram import CONFLICT_SENTINEL, TelegramAlertChannel
from genesis.guardian.config import GuardianConfig
from genesis.guardian.provisioning.base import ProvisioningAdapter
from genesis.guardian.provisioning.expand import expand_storage
from genesis.guardian.provisioning.gate import (
    DueDiligenceReport,
    evaluate_disk_grow,
    evaluate_memory_grow,
    evaluate_vzdump,
)
from genesis.guardian.provisioning.ledger import ProvisioningLedger

logger = logging.getLogger(__name__)

_CONFLICT_BACKOFF_S = 5
_CONFLICT_ALERT_THRESHOLD = 3
_APPROVE_DENY = frozenset({"APPROVE", "DENY"})


@dataclass
class ProvisionRequest:
    kind: str  # "disk" | "memory"
    disk: str = ""
    add_gib: int = 0
    new_mib: int = 0
    absorb_after: bool = False  # run storage-expand after a verified disk grow
    origin: str = "manual"  # "manual" | "autonomous"


def _find_telegram_channel(dispatcher: AlertDispatcher) -> TelegramAlertChannel | None:
    # Duplicated (3 lines) rather than importing check.py — flow.py must stay
    # importable BY check.py without a cycle.
    for channel in dispatcher._channels:
        if isinstance(channel, TelegramAlertChannel):
            return channel
    return None


def _evaluate(
    request: ProvisionRequest,
    cap,
    config: GuardianConfig,
    actions_in_window: int,
    backup_age_days: float | None,
) -> DueDiligenceReport:
    pc = config.provisioning
    if request.kind == "disk":
        return evaluate_disk_grow(
            cap, pc, request.disk, request.add_gib, actions_in_window, backup_age_days,
        )
    return evaluate_memory_grow(
        cap, pc, request.new_mib, actions_in_window, backup_age_days,
    )


def _proposal_html(request: ProvisionRequest, report: DueDiligenceReport) -> str:
    header = "🔧 <b>Provisioning proposal</b>"
    action = (
        f"Action: <b>{html_escape(report.action)}</b> — "
        f"{html_escape(report.requested)}"
    )
    origin = f"Origin: {html_escape(request.origin)}"
    checks = "\n".join(html_escape(line) for line in report.as_lines())
    footer = (
        "\nReply <b>APPROVE</b> to execute or <b>DENY</b> to cancel.\n"
        "⚠️ This grow is irreversible. One attempt, no auto-retry."
    )
    return f"{header}\n{action}\n{origin}\n\nDue diligence:\n{checks}\n{footer}"


async def _wait_for_provision_reply(
    channel: TelegramAlertChannel,
    dispatcher: AlertDispatcher,
    gate_msg_id: int,
    timeout_s: int,
) -> str | None:
    """Bounded wait for APPROVE/DENY. Returns the keyword, or None on timeout.

    Guardian-only (Genesis-DOWN) path. A getUpdates 409 here means the main
    Genesis bot started polling again mid-approval — i.e. Genesis RECOVERED and
    now owns approval. We back off and keep trying to the deadline (the user can
    still re-initiate the grow from Genesis). Unlike the recovery gate, this
    NEVER self-cancels on health recovery and IS time-bounded.
    """
    deadline = time.monotonic() + timeout_s
    conflicts = 0
    while time.monotonic() < deadline:
        kw = await channel.poll_for_keyword(gate_msg_id, _APPROVE_DENY)
        if kw == CONFLICT_SENTINEL:
            conflicts += 1
            logger.warning(
                "provisioning approval getUpdates 409 (consecutive=%d)", conflicts,
            )
            if conflicts >= _CONFLICT_ALERT_THRESHOLD:
                await dispatcher.send(Alert(
                    severity=AlertSeverity.WARNING,
                    title="Guardian approval yielded — Genesis is back",
                    body=(
                        "The main Genesis bot is polling again, so Genesis has "
                        "recovered and now owns approval. Re-initiate the grow "
                        "from Genesis (it can read your reply); Guardian stands "
                        "down on this one."
                    ),
                ))
                conflicts = 0
            await asyncio.sleep(_CONFLICT_BACKOFF_S)
            continue
        conflicts = 0
        if kw in _APPROVE_DENY:
            return kw
        # None → poll window elapsed with no reply; loop until the deadline.
    return None


async def _execute(request: ProvisionRequest, adapter: ProvisioningAdapter):
    if request.kind == "disk":
        return await adapter.grow_vm_disk(request.disk, request.add_gib)
    return await adapter.grow_vm_memory(request.new_mib)


async def execute_provisioning_action(
    config: GuardianConfig,
    request: ProvisionRequest,
    adapter: ProvisioningAdapter,
    dispatcher: AlertDispatcher,
    ledger: ProvisioningLedger,
) -> dict:
    """Execute an ALREADY-APPROVED provisioning action. No approval gate here.

    Shared execute-only core for both owners of approval (container-when-up,
    guardian-when-down). Re-runs the due-diligence gate FRESH (state may have
    moved since approval; also enforces the rate cap), executes exactly once,
    ledgers even an unverified mutation, optionally absorbs a disk grow, and
    emits a result alert (CRITICAL on failure/unverified — never auto-retries).

    Callers MUST have obtained a fresh APPROVE before invoking this.
    """
    # 1. Fresh re-check (capacity + backup + rate cap) — no mutation on failure.
    cap = await adapter.get_capacity()
    report = _evaluate(
        request, cap, config, ledger.actions_in_window(action_prefix="grow_"),
        await adapter.newest_backup_age_days(),
    )
    if not report.passed:
        await dispatcher.send(Alert(
            severity=AlertSeverity.WARNING,
            title=f"Provisioning aborted before execution: {report.requested}",
            body="Due-diligence re-check failed at execute time:\n"
                 + "\n".join(report.as_lines()),
        ))
        return {"ok": False, "stage": "recheck_failed", "requested": report.requested,
                "checks": report.as_lines(),
                "failed_checks": report.failed_names()}

    # 1b. Anti-stack guard (disk only): a RELATIVE disk grow is non-idempotent.
    # If a prior grow of this disk was recorded unverified but the live size has
    # since reached that grow's target, it DID land — issuing another +NG would
    # stack a second grow. Detect it, clear the latch, and refuse to stack.
    # (Memory grows are absolute + grow-only, so they need no such guard.)
    if request.kind == "disk":
        current = cap.disks.get(request.disk)
        pending = ledger.latest_unverified_disk(request.disk)
        target = pending.get("target_bytes") if pending else None
        if target and current is not None and current >= target:
            ledger.mark_latest_disk_verified(request.disk)
            await dispatcher.send(Alert(
                severity=AlertSeverity.INFO,
                title=f"Prior disk grow already landed: {request.disk}",
                body=(f"{request.disk} is already {current / 1024**3:.0f}G — a "
                      "previously-unverified grow did land. Not stacking another "
                      "grow. Re-request only if you truly want MORE space."),
            ))
            return {"ok": True, "stage": "already_landed", "action": "grow_vm_disk",
                    "requested": f"{request.disk} +{request.add_gib}G", "verified": True}

    # 2. Execute once + ledger (even if unverified — it may have landed).
    result = await _execute(request, adapter)
    ledger.record_action(
        result.action, result.requested, result.ok, result.verified,
        target_bytes=result.target_bytes,
    )

    expand_result = None
    if request.kind == "disk" and request.absorb_after and result.ok and result.verified:
        # Bound the absorb to exactly the approved grow amount (btrfs substrate
        # extends its backing LV by this; LVM-thin ignores it — pvresize only).
        expand_result = await expand_storage(config, add_gib=request.add_gib or None)

    # 3. Result alert.
    if result.ok and result.verified:
        extra = ""
        if result.requires_reboot:
            extra = "\n⚠️ Takes effect after a VM reboot (schedule a downtime window)."
        if expand_result is not None:
            if expand_result.get("driver") == "btrfs":
                detail = f"fs_size={expand_result.get('fs_size_bytes')}"
            else:
                detail = f"vg_free={expand_result.get('vg_free_bytes')}"
            extra += (f"\nstorage-expand: {'ok' if expand_result['ok'] else 'FAILED'} "
                      f"({detail})")
        await dispatcher.send(Alert(
            severity=AlertSeverity.INFO,
            title=f"Provisioning done: {result.requested}",
            body=f"{result.before} → {result.after}.{extra}",
        ))
    else:
        await dispatcher.send(Alert(
            severity=AlertSeverity.CRITICAL,
            title=f"Provisioning FAILED/unverified: {result.requested}",
            body=f"{result.error or 'unknown'}\nNo auto-retry — manual check needed.",
        ))

    return {
        "ok": result.ok and result.verified,
        "stage": "executed",
        "action": result.action,
        "requested": result.requested,
        "before": result.before,
        "after": result.after,
        "verified": result.verified,
        "requires_reboot": result.requires_reboot,
        "error": result.error,
        "expand": expand_result,
    }


async def run_provisioning_flow(
    config: GuardianConfig,
    request: ProvisionRequest,
    adapter: ProvisioningAdapter,
    dispatcher: AlertDispatcher,
    ledger: ProvisioningLedger,
) -> dict:
    """Genesis-DOWN approval path: getUpdates gate → shared execute core.

    Used ONLY when the main Genesis bot is not polling (container down/dead) —
    otherwise getUpdates 409s against it. When Genesis is UP the CONTAINER owns
    approval (outreach) and calls :func:`execute_provisioning_action` via the
    host gateway verb.
    """
    pc = config.provisioning

    # 1-2. Capacity + due-diligence gate — fail ⇒ refuse + alert, NEVER propose.
    cap = await adapter.get_capacity()
    backup_age = await adapter.newest_backup_age_days()
    report = _evaluate(
        request, cap, config,
        ledger.actions_in_window(action_prefix="grow_"), backup_age,
    )

    if not report.passed:
        body = "Due-diligence gate failed:\n" + "\n".join(report.as_lines())
        if "recent backup" in report.failed_names():
            # Deliberate, audited escape hatch — named, not hidden. This path
            # runs when Genesis is DOWN (host likely degraded); an hour-scale
            # vzdump is NOT chained here in that state.
            body += (
                "\nBackup is stale/unknown. Take one from Genesis when it is "
                "up (provision vzdump), or for THIS emergency only: gateway "
                "verb `configure-provisioning require_recent_backup=false` "
                "(audited; re-enable after)."
            )
        await dispatcher.send(Alert(
            severity=AlertSeverity.WARNING,
            title=f"Provisioning refused: {report.requested}",
            body=body,
        ))
        return {"ok": False, "stage": "refused_gate", "requested": report.requested,
                "checks": report.as_lines(),
                "failed_checks": report.failed_names()}

    # 3. No channel ⇒ we cannot obtain approval ⇒ never mutate.
    channel = _find_telegram_channel(dispatcher)
    if channel is None:
        await dispatcher.send(Alert(
            severity=AlertSeverity.WARNING,
            title=f"Provisioning not executed: {report.requested}",
            body="No Telegram channel configured — cannot obtain approval; refusing.",
        ))
        return {"ok": False, "stage": "no_channel", "requested": report.requested}

    # 4. Propose + block for approval (getUpdates).
    msg_id = await channel.send_text(_proposal_html(request, report))
    if not msg_id:
        return {"ok": False, "stage": "no_channel", "requested": report.requested,
                "error": "failed to send proposal"}
    kw = await _wait_for_provision_reply(channel, dispatcher, msg_id, pc.approval_timeout_s)
    if kw != "APPROVE":
        stage = "denied" if kw == "DENY" else "timeout"
        await dispatcher.send(Alert(
            severity=AlertSeverity.INFO if kw == "DENY" else AlertSeverity.WARNING,
            title=f"Provisioning {stage}: {report.requested}",
            body=("Denied by user." if kw == "DENY"
                  else f"No reply within {pc.approval_timeout_s}s — not executed."),
        ))
        return {"ok": False, "stage": stage, "requested": report.requested}

    # 5. Approved → hand to the shared execute core (fresh re-check inside).
    return await execute_provisioning_action(
        config, request, adapter, dispatcher, ledger,
    )


async def maybe_propose_pool_grow(
    config: GuardianConfig,
    adapter: ProvisioningAdapter,
    dispatcher: AlertDispatcher,
    ledger: ProvisioningLedger,
) -> dict | None:
    """Autonomous path (pool TIER_CRIT): PROPOSE a disk grow, damped by
    min_repropose_hours. Never executes without the same APPROVE gate."""
    pc = config.provisioning
    hrs = ledger.hours_since_last_proposal("pool_grow")
    if hrs is not None and hrs < pc.min_repropose_hours:
        logger.info(
            "pool-grow proposal damped (%.1fh since last, min %dh)",
            hrs, pc.min_repropose_hours,
        )
        return None
    ledger.mark_proposed("pool_grow")  # damp even if the flow blocks/fails
    request = ProvisionRequest(
        kind="disk", disk=pc.target_disk, add_gib=pc.max_disk_step_gib,
        absorb_after=True, origin="autonomous (pool critical)",
    )
    return await run_provisioning_flow(config, request, adapter, dispatcher, ledger)


def _vzdump_in_flight_upid(config: GuardianConfig, ledger: ProvisioningLedger) -> str:
    """The UPID of a still-latched backup, or "".

    Latch = the latest vzdump ledger entry is unverified, HAS a upid (an entry
    without one is a failed start — nothing to resume, must never latch), is
    NOT yet resolved (a terminal-failed row carries ``resolved_ts`` while
    staying ``verified: false`` — the task is over, so it must not latch), and
    is younger than the vzdump wall bound. Past the wall bound the latch
    self-expires: the operation ends as UNVERIFIED, never blocks forever.
    """
    entry = ledger.latest_backup()
    if (not entry or entry.get("verified") or entry.get("resolved_ts")
            or not entry.get("upid")):
        return ""
    try:
        started = datetime.fromisoformat(str(entry.get("ts")))
    except (TypeError, ValueError):
        return ""
    age_s = (datetime.now(UTC) - started).total_seconds()
    if age_s >= config.provisioning.vzdump_timeout_s:
        return ""
    return str(entry.get("upid"))


async def execute_vzdump_start(
    config: GuardianConfig,
    adapter: ProvisioningAdapter,
    dispatcher: AlertDispatcher,
    ledger: ProvisioningLedger,
) -> dict:
    """START an ALREADY-APPROVED vzdump (phase 1 of 2). No approval gate here.

    Mirrors :func:`execute_provisioning_action`'s execute-only contract: fresh
    gate re-check → launch once → ledger AT START (the POST consumed real
    hypervisor work whether or not verification ever runs — the start row is
    the rate-cap entry, the in-flight latch, and the restart-resume handle).
    Returns immediately with the UPID; verification is
    :func:`verify_vzdump_step`, driven on the caller's cadence.
    """
    cap = await adapter.get_capacity()
    report = evaluate_vzdump(
        cap, config.provisioning,
        ledger.actions_in_window(action_prefix="vzdump"),
        in_flight_upid=_vzdump_in_flight_upid(config, ledger),
    )
    if not report.passed:
        await dispatcher.send(Alert(
            severity=AlertSeverity.WARNING,
            title=f"Backup aborted before start: {report.requested}",
            body="Due-diligence re-check failed at start time:\n"
                 + "\n".join(report.as_lines()),
        ))
        return {"ok": False, "stage": "recheck_failed", "requested": report.requested,
                "checks": report.as_lines(),
                "failed_checks": report.failed_names()}

    res = await adapter.vzdump_start()
    if res.attempted:
        # B1: ledger at start, even on a failed launch (conservative — the
        # hypervisor may have begun work). A upid-less failure never latches.
        ledger.record_action(
            "vzdump", res.requested, ok=res.ok, verified=False, upid=res.upid,
        )
    if not res.ok:
        await dispatcher.send(Alert(
            severity=AlertSeverity.CRITICAL,
            title=f"Backup start FAILED: {res.requested}",
            body=f"{res.error or 'unknown'}\nNo auto-retry — manual check needed.",
        ))
        return {"ok": False, "stage": "start_failed", "requested": res.requested,
                "error": res.error}
    logger.info("vzdump started: %s (%s)", res.requested, res.upid)
    return {"ok": True, "stage": "started", "requested": res.requested,
            "upid": res.upid}


async def verify_vzdump_step(
    config: GuardianConfig,
    adapter: ProvisioningAdapter,
    dispatcher: AlertDispatcher,
    ledger: ProvisioningLedger,
    upid: str = "",
) -> dict:
    """One verification probe for a started vzdump (phase 2 of 2).

    No ``upid`` = resume the latest unverified ledger entry (restart-safe: a
    new process can pick up an in-flight backup with zero extra state). The
    result's ``state`` is the caller's contract — running/unknown are
    TRANSIENT (retry to the wall bound), only ``failed`` is terminal;
    ``ok`` only means "this probe reached a usable answer".

    On ``verified``: flip the start row, rotate (prune to keep-last), alert.
    On ``failed``: flip the row terminally, alert CRITICAL. Never auto-retries
    the backup itself.
    """
    if not upid:
        entry = ledger.latest_backup()
        if not entry or entry.get("verified") or not entry.get("upid"):
            return {"ok": False, "stage": "no_backup_in_flight",
                    "error": "no unverified backup with a task handle in the ledger"}
        upid = str(entry["upid"])

    status = await adapter.vzdump_status(upid)

    if status.state == "verified":
        ledger.mark_latest_backup_verified(upid, ok=True)
        prune_ok, prune_detail = await adapter.prune_backups()
        await dispatcher.send(Alert(
            severity=AlertSeverity.INFO,
            title="Backup verified",
            body=(f"{status.volid or 'backup'} is on the datastore "
                  f"(age {status.age_days:.2f}d).\n"
                  f"Rotation: {'ok' if prune_ok else 'FAILED'} — {prune_detail}"
                  if status.age_days is not None else
                  f"{status.volid or 'backup'} is on the datastore.\n"
                  f"Rotation: {'ok' if prune_ok else 'FAILED'} — {prune_detail}"),
        ))
        return {"ok": True, "stage": "verified", "state": "verified",
                "upid": upid, "volid": status.volid,
                "prune_ok": prune_ok, "prune": prune_detail}

    if status.state == "failed":
        ledger.mark_latest_backup_verified(upid, ok=False)
        await dispatcher.send(Alert(
            severity=AlertSeverity.CRITICAL,
            title="Backup task FAILED",
            body=f"{upid}\n{status.detail}\nNo auto-retry — manual check needed.",
        ))
        return {"ok": False, "stage": "task_failed", "state": "failed",
                "upid": upid, "error": status.detail}

    # running / unknown — transient by contract; no ledger change, no alert.
    return {"ok": True, "stage": "pending", "state": status.state,
            "upid": upid, "detail": status.detail}
