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
        request, cap, config, ledger.actions_in_window(),
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
                "checks": report.as_lines()}

    # 2. Execute once + ledger (even if unverified — it may have landed).
    result = await _execute(request, adapter)
    ledger.record_action(result.action, result.requested, result.ok, result.verified)

    expand_result = None
    if request.kind == "disk" and request.absorb_after and result.ok and result.verified:
        expand_result = await expand_storage(config)

    # 3. Result alert.
    if result.ok and result.verified:
        extra = ""
        if result.requires_reboot:
            extra = "\n⚠️ Takes effect after a VM reboot (schedule a downtime window)."
        if expand_result is not None:
            extra += (f"\nstorage-expand: {'ok' if expand_result['ok'] else 'FAILED'} "
                      f"(vg_free={expand_result.get('vg_free_bytes')})")
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
    report = _evaluate(request, cap, config, ledger.actions_in_window(), backup_age)

    if not report.passed:
        await dispatcher.send(Alert(
            severity=AlertSeverity.WARNING,
            title=f"Provisioning refused: {report.requested}",
            body="Due-diligence gate failed:\n" + "\n".join(report.as_lines()),
        ))
        return {"ok": False, "stage": "refused_gate", "requested": report.requested,
                "checks": report.as_lines()}

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
