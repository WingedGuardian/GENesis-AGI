"""Automaton Supervisor module.

Cognitive oversight of Conway Research Automaton instances running on Conway
Cloud. Genesis provides strategic direction, monitors health, enforces
treasury policy, and closes the learning loop.

Probes run on an internal schedule — no awareness loop coupling.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from .client import ConwayCloudClient
from .types import (
    AutomatonInstance,
    InstanceStatus,
    ProbeResult,
    SurvivalTier,
    TreasuryPolicy,
)

logger = logging.getLogger(__name__)

# Default probe interval (configurable via update_config)
_DEFAULT_PROBE_INTERVAL_S = 300  # 5 minutes


class AutomatonSupervisorModule:
    """CapabilityModule for supervising Automatons on Conway Cloud.

    Manages its own probe schedule internally — does not wire into the
    awareness loop. Alerts fire through the event bus.
    """

    def __init__(self) -> None:
        self._enabled: bool = False
        self._runtime: Any = None
        self._client: ConwayCloudClient | None = None
        self._instances: dict[str, AutomatonInstance] = {}
        self._treasury: TreasuryPolicy = TreasuryPolicy()
        self._probe_interval_s: int = _DEFAULT_PROBE_INTERVAL_S
        self._probe_task: asyncio.Task | None = None
        self._description: str = ""

    @property
    def name(self) -> str:
        return "automaton_supervisor"

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        was_enabled = self._enabled
        self._enabled = value
        # Start/stop probe loop on state change
        if value and not was_enabled:
            self._start_probe_loop()
        elif not value and was_enabled:
            self._stop_probe_loop()

    async def register(self, runtime: Any) -> None:
        """Wire into Genesis runtime.

        - Load Conway Cloud API key from secrets
        - Initialize the client
        - Load managed instances from DB
        - Start probe loop if enabled
        """
        self._runtime = runtime

        # Load API key from secrets or config
        api_key = ""
        if hasattr(runtime, "_secrets") and runtime._secrets:
            api_key = runtime._secrets.get("CONWAY_API_KEY", "")

        if not api_key:
            logger.warning(
                "No CONWAY_API_KEY found in secrets. "
                "Module will load but cannot connect to Conway Cloud. "
                "Set the key in secrets.env and restart."
            )

        self._client = ConwayCloudClient(api_key=api_key)

        # Load managed instances from DB
        await self._load_instances()

        # Start probe loop if already enabled (restored from DB state)
        if self._enabled and self._instances:
            self._start_probe_loop()

        logger.info(
            "Automaton supervisor registered (%d managed instances)",
            len(self._instances),
        )

    async def deregister(self) -> None:
        """Clean shutdown."""
        self._stop_probe_loop()
        if self._client:
            await self._client.close()
            self._client = None
        self._instances.clear()

    def get_research_profile_name(self) -> str | None:
        """No research pipeline subscription.

        Automaton supervision is poll-based via internal probe loop,
        not signal-driven via the research pipeline.
        """
        return None

    async def handle_opportunity(self, opportunity: dict) -> dict | None:
        """Evaluate whether to take action on an Automaton-related signal.

        Returns an action proposal for user approval, or None.
        """
        action_type = opportunity.get("type", "")

        if action_type == "provision":
            return {
                "action": "provision_automaton",
                "name": opportunity.get("name", "genesis-worker"),
                "genesis_prompt": opportunity.get("genesis_prompt", ""),
                "estimated_cost_usd": 5.0,
                "requires_approval": True,
            }

        if action_type == "fund":
            instance_id = opportunity.get("instance_id", "")
            amount = opportunity.get("amount_cents", self._treasury.auto_topup_amount_cents)
            requires_approval = amount > self._treasury.require_approval_above_cents
            return {
                "action": "fund_automaton",
                "instance_id": instance_id,
                "amount_cents": amount,
                "requires_approval": requires_approval,
            }

        if action_type == "inject_strategy":
            return {
                "action": "inject_strategy",
                "instance_id": opportunity.get("instance_id", ""),
                "channel": opportunity.get("channel", "inbox"),
                "content": opportunity.get("content", ""),
                "requires_approval": opportunity.get("high_impact", False),
            }

        return None

    async def record_outcome(self, outcome: dict) -> None:
        """Record an Automaton-related outcome."""
        instance_id = outcome.get("instance_id", "")
        event_type = outcome.get("event_type", "unknown")
        details = outcome.get("details", "")

        if not self._runtime or not hasattr(self._runtime, "_db"):
            return

        db = self._runtime._db
        await db.execute(
            "INSERT INTO automaton_events (instance_id, event_type, details) "
            "VALUES (?, ?, ?)",
            (instance_id, event_type, details),
        )
        await db.commit()

    async def extract_generalizable(self, outcome: dict) -> list[dict] | None:
        """Extract lessons from Automaton outcomes for Genesis core memory.

        Phase 3 — not yet implemented.
        """
        return None

    def configurable_fields(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "probe_interval_s",
                "label": "Probe Interval (seconds)",
                "type": "int",
                "value": self._probe_interval_s,
                "description": "How often to check Automaton health",
            },
            {
                "name": "auto_topup_amount_cents",
                "label": "Auto-topup Amount (cents)",
                "type": "int",
                "value": self._treasury.auto_topup_amount_cents,
                "description": "Credits to add when balance drops below minimum reserve",
            },
            {
                "name": "min_reserve_cents",
                "label": "Minimum Reserve (cents)",
                "type": "int",
                "value": self._treasury.min_reserve_cents,
                "description": "Balance threshold that triggers auto-topup",
            },
            {
                "name": "daily_cap_cents",
                "label": "Daily Spending Cap (cents)",
                "type": "int",
                "value": self._treasury.daily_cap_cents,
                "description": "Maximum credits to spend per day across all instances",
            },
        ]

    def update_config(self, updates: dict[str, Any]) -> dict[str, Any]:
        if "probe_interval_s" in updates:
            self._probe_interval_s = max(60, int(updates["probe_interval_s"]))
        for key in ("auto_topup_amount_cents", "min_reserve_cents", "daily_cap_cents"):
            if key in updates:
                setattr(self._treasury, key, int(updates[key]))
        return {
            "probe_interval_s": self._probe_interval_s,
            "auto_topup_amount_cents": self._treasury.auto_topup_amount_cents,
            "min_reserve_cents": self._treasury.min_reserve_cents,
            "daily_cap_cents": self._treasury.daily_cap_cents,
        }

    # ── Internal Probe Loop ────────────────────────────────────────

    def _start_probe_loop(self) -> None:
        """Start the background probe task."""
        if self._probe_task and not self._probe_task.done():
            return  # Already running
        self._probe_task = asyncio.create_task(
            self._probe_loop(), name="automaton-supervisor-probes"
        )
        logger.info(
            "Automaton probe loop started (interval=%ds)", self._probe_interval_s
        )

    def _stop_probe_loop(self) -> None:
        """Stop the background probe task."""
        if self._probe_task and not self._probe_task.done():
            self._probe_task.cancel()
            self._probe_task = None
            logger.info("Automaton probe loop stopped")

    async def _probe_loop(self) -> None:
        """Background loop that probes all managed instances on a schedule."""
        try:
            while self._enabled:
                if self._instances and self._client:
                    try:
                        results = await self._run_probes()
                        await self._persist_probes(results)
                        await self._handle_alerts(results)
                    except Exception:
                        logger.exception("Probe cycle failed")
                await asyncio.sleep(self._probe_interval_s)
        except asyncio.CancelledError:
            pass

    async def _run_probes(self) -> list[ProbeResult]:
        """Run all probes across all managed instances."""
        results: list[ProbeResult] = []
        for instance in self._instances.values():
            if instance.status == InstanceStatus.DEAD:
                continue
            try:
                results.extend(await self._probe_instance(instance))
            except Exception:
                logger.exception("Probe failed for %s", instance.id)
                results.append(
                    ProbeResult(
                        probe_type="alive",
                        success=False,
                        message=f"Probe failed for {instance.name}",
                        alerts=[f"Automaton '{instance.name}' unreachable"],
                    )
                )
        return results

    async def _persist_probes(self, results: list[ProbeResult]) -> None:
        """Write probe results to the database."""
        if not self._runtime or not hasattr(self._runtime, "_db"):
            return
        db = self._runtime._db
        now = datetime.now(UTC).isoformat()
        for r in results:
            await db.execute(
                "INSERT INTO automaton_probes "
                "(instance_id, probe_type, result, value_numeric, timestamp) "
                "VALUES (?, ?, ?, ?, ?)",
                ("_all", r.probe_type, r.message, r.value, now),
            )
        await db.commit()

    async def _handle_alerts(self, results: list[ProbeResult]) -> None:
        """Fire alerts for any probe results that need attention."""
        alerts = [a for r in results for a in r.alerts]
        if not alerts:
            return

        # Fire via event bus if available
        if self._runtime and hasattr(self._runtime, "_event_bus"):
            bus = self._runtime._event_bus
            for alert in alerts:
                bus.emit(
                    "automaton.alert",
                    {"message": alert, "module": self.name},
                )
                logger.warning("Automaton alert: %s", alert)

    async def _probe_instance(self, instance: AutomatonInstance) -> list[ProbeResult]:
        """Probe a single Automaton instance."""
        assert self._client is not None
        results: list[ProbeResult] = []

        # 1. Agent state
        state = await self._client.get_agent_state(instance.sandbox_id)
        alive = state not in ("dead", "unknown")
        results.append(
            ProbeResult(
                probe_type="alive",
                success=alive,
                message=f"state={state}",
                alerts=[] if alive else [f"Automaton '{instance.name}' is {state}"],
            )
        )

        if state == "dead":
            instance.status = InstanceStatus.DEAD
        elif state in ("running", "waking", "sleeping", "low_compute", "critical"):
            instance.status = InstanceStatus.ACTIVE

        # 2. Turn count
        turn_count = await self._client.get_turn_count(instance.sandbox_id)
        turns_delta = turn_count - instance.total_turns
        instance.total_turns = turn_count
        results.append(
            ProbeResult(
                probe_type="turns",
                success=True,
                value=float(turns_delta),
                message=f"total={turn_count}, delta={turns_delta}",
                alerts=(
                    [f"Automaton '{instance.name}' idle — 0 new turns"]
                    if turns_delta == 0 and instance.status == InstanceStatus.ACTIVE
                    else []
                ),
            )
        )

        # 3. Credit balance
        try:
            balance = await self._client.get_credits_balance()
            tier = self._classify_tier(balance)
            instance.survival_tier = tier
            alerts = []
            if tier in (SurvivalTier.CRITICAL, SurvivalTier.DEAD):
                alerts.append(
                    f"Automaton '{instance.name}' at {tier.value} tier "
                    f"(balance: ${balance / 100:.2f})"
                )
            elif balance < self._treasury.min_reserve_cents:
                alerts.append(
                    f"Automaton '{instance.name}' below min reserve "
                    f"(${balance / 100:.2f} < ${self._treasury.min_reserve_cents / 100:.2f})"
                )
            results.append(
                ProbeResult(
                    probe_type="wallet",
                    success=True,
                    value=float(balance),
                    message=f"balance=${balance / 100:.2f}, tier={tier.value}",
                    alerts=alerts,
                )
            )
        except Exception as exc:
            results.append(
                ProbeResult(
                    probe_type="wallet",
                    success=False,
                    message=str(exc),
                    alerts=[f"Cannot read wallet for '{instance.name}'"],
                )
            )

        return results

    @staticmethod
    def _classify_tier(balance_cents: int) -> SurvivalTier:
        """Classify survival tier from credit balance."""
        if balance_cents > 500:
            return SurvivalTier.HIGH
        if balance_cents > 50:
            return SurvivalTier.NORMAL
        if balance_cents > 10:
            return SurvivalTier.LOW_COMPUTE
        if balance_cents > 0:
            return SurvivalTier.CRITICAL
        return SurvivalTier.DEAD

    # ── Instance Management ────────────────────────────────────────

    async def _load_instances(self) -> None:
        """Load managed instances from the Genesis database."""
        if not self._runtime or not hasattr(self._runtime, "_db"):
            return
        db = self._runtime._db
        try:
            async with db.execute(
                "SELECT id, sandbox_id, name, wallet_address, genesis_prompt, "
                "status, survival_tier, created_at, last_probe, "
                "total_earnings_cents, total_spent_cents, total_turns "
                "FROM automaton_instances"
            ) as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    inst = AutomatonInstance(
                        id=row[0],
                        sandbox_id=row[1],
                        name=row[2],
                        wallet_address=row[3] or "",
                        genesis_prompt=row[4] or "",
                        status=InstanceStatus(row[5]) if row[5] else InstanceStatus.PROVISIONING,
                        survival_tier=SurvivalTier(row[6]) if row[6] else SurvivalTier.NORMAL,
                        created_at=row[7] or "",
                        last_probe=row[8] or "",
                        total_earnings_cents=row[9] or 0,
                        total_spent_cents=row[10] or 0,
                        total_turns=row[11] or 0,
                    )
                    self._instances[inst.id] = inst
        except Exception:
            logger.debug("automaton_instances table not found — will be created on first use")
