"""Provisioning flow — capacity → gate → approval → execute → verify → ledger.

The single orchestration path for every grow, manual or autonomous:

  1. read capacity + rate/backup context
  2. due-diligence gate (ALL checks) — fail ⇒ refuse + alert, NEVER propose
  3. no Telegram channel ⇒ refuse + alert (can't gate ⇒ never mutate)
  4. send the proposal (full check table) and block for an APPROVE/DENY reply,
     bounded by approval_timeout_s (tolerating getUpdates 409 with backoff)
  5. on APPROVE, RE-RUN capacity+gate fresh (state may have moved) before mutating
  6. execute exactly once, ledger it (even unverified — it may have landed),
     optionally run in-process storage-expand to absorb a disk grow
  7. result alert — CRITICAL + "no auto-retry" on failure/unverified

Kept import-light and free of any check.py import so check.py can import THIS
for the pool-crit propose hook without a cycle.
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

    Unlike the recovery gate, this NEVER self-cancels on health recovery
    (provisioning is not tied to a down/up transition) and IS time-bounded.
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
                    title="Provisioning approval blocked (getUpdates conflict)",
                    body=(
                        "The main Genesis bot is polling the same Telegram token, "
                        "so Guardian can't read your APPROVE/DENY. Retrying; a "
                        "dedicated guardian bot token removes this contention."
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


async def run_provisioning_flow(
    config: GuardianConfig,
    request: ProvisionRequest,
    adapter: ProvisioningAdapter,
    dispatcher: AlertDispatcher,
    ledger: ProvisioningLedger,
) -> dict:
    pc = config.provisioning

    # 1-2. Capacity + due-diligence gate.
    cap = await adapter.get_capacity()
    backup_age = await adapter.newest_backup_age_days()
    actions = ledger.actions_in_window()
    report = _evaluate(request, cap, config, actions, backup_age)

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

    # 4. Propose + block for approval.
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

    # 5. Re-run capacity + gate FRESH after approval (state may have moved).
    cap2 = await adapter.get_capacity()
    report2 = _evaluate(request, cap2, config, ledger.actions_in_window(), backup_age)
    if not report2.passed:
        await dispatcher.send(Alert(
            severity=AlertSeverity.WARNING,
            title=f"Provisioning aborted after approval: {report.requested}",
            body="State changed between approval and execution:\n"
                 + "\n".join(report2.as_lines()),
        ))
        return {"ok": False, "stage": "recheck_failed", "requested": report.requested,
                "checks": report2.as_lines()}

    # 6. Execute once + ledger (even if unverified — it may have landed).
    result = await _execute(request, adapter)
    ledger.record_action(result.action, result.requested, result.ok, result.verified)

    expand_result = None
    if request.kind == "disk" and request.absorb_after and result.ok and result.verified:
        expand_result = await expand_storage(config)

    # 7. Result alert.
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
