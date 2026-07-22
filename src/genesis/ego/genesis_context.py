"""Genesis ego context builder — assembles Genesis-internal operational briefing.

The Genesis ego (COO) sees only infrastructure and system state. It handles
self-maintenance so the user doesn't have to. When it encounters issues
beyond its scope, it escalates to the user ego via observations.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from genesis.ego.user_context import _USER_WORLD_CATEGORIES

logger = logging.getLogger(__name__)


class GenesisEgoContextBuilder:
    """Builds the Genesis-internal operational briefing.

    Reads from:
    - HealthDataService.snapshot() for live subsystem state
    - capabilities.json for registered capabilities
    - awareness_ticks for recent signal values
    - observations for unresolved Genesis-internal items
    - follow_ups for maintenance threads
    """

    def __init__(
        self,
        *,
        db: aiosqlite.Connection,
        health_data: Any | None = None,
        capabilities: dict[str, str] | None = None,
    ) -> None:
        self._db = db
        self._health_data = health_data
        self._capabilities = capabilities or {}

    async def build(
        self,
        *,
        context_weights: dict[str, str] | None = None,
        focus_id: str | None = None,
    ) -> str:
        """Assemble the full Genesis ego context.

        Parameters
        ----------
        context_weights:
            Per-section weight dict from the focus selector.
            Keys that match genesis section names are applied; unknown
            keys are ignored (default to "deep").
        focus_id:
            The focused target for this cycle. On a capability_improvement
            cycle it is the weak domain name, surfaced as a "Focused
            deficiency" row in the capability performance section.
        """
        import asyncio

        from genesis.ego.focus import _ALWAYS_SECTIONS

        weights = dict(context_weights) if context_weights else {}
        # Defense-in-depth: primary enforcement in compaction.assemble_context()
        for section in _ALWAYS_SECTIONS:
            if weights.get(section) in ("skip", "light"):
                weights[section] = "deep"

        # Capability-improvement cycles target a specific weak domain, carried
        # as focus_id. Stash it for _capability_performance_section so the
        # focused (low-confidence, hence off the top-N table) domain's row is
        # always surfaced. Reset each build — a stale value would mislabel a
        # later cycle's context.
        self._focus_id = focus_id

        sections: list[str] = []
        sections.append("# GENESIS_EGO_CONTEXT — Operations Briefing\n")
        sections.append(
            "*You are Genesis's operations intelligence. "
            "Keep the system healthy so the user doesn't have to.*\n"
        )

        # Map weight keys → section methods. Genesis ego sections differ
        # from user ego sections. Keys not in focus.py weight table
        # default to "deep" via weights.get(key, "deep").
        section_map: list[tuple[str, Any]] = [
            ("system_health", self._system_health_section),
            ("intentions", self._intentions_section),
            ("settled_decisions", self._settled_decisions_section),
            ("signals", self._signals_section),
            ("observations", self._observations_section),
            ("follow_ups", self._follow_ups_section),
            ("own_goals", self._own_goals_section),
            # Cost section removed — the genesis ego's identity doc says
            # "Do NOT opine on config values... budget caps... are user
            # decisions."  Feeding cost data every cycle caused a 10+
            # escalation loop.  Cost data remains visible via health_status
            # MCP and the dashboard for human review.
            ("proposal_history", self._proposal_history_section),
            ("proposal_board", self._proposal_board_section),
            ("execution_outcomes", self._execution_outcomes_section),
            ("capability_performance", self._capability_performance_section),
            ("confidence_calibration", self._confidence_calibration_section),
            ("output_contract", self._output_contract_section),
        ]

        for key, method in section_map:
            depth = weights.get(key, "deep")
            if depth == "skip":
                continue
            is_async = asyncio.iscoroutinefunction(method)
            if depth == "light":
                result = (
                    await method(depth="light") if is_async
                    else method(depth="light")
                )
            else:
                result = await method() if is_async else method()
            sections.append(result)

        return "\n".join(sections)

    # -- Section builders --

    async def _intentions_section(self, *, depth: str = "deep") -> str:
        """Deferred intentions for review.

        depth is accepted for interface consistency but ignored — intentions
        are always rendered at full depth.
        """
        from genesis.ego.intentions_context import build_intentions_section
        return await build_intentions_section(self._db, "genesis_ego_cycle")

    async def _system_health_section(self, *, depth: str = "deep") -> str:
        """Live system health from health_data snapshot."""
        lines = ["## System Health\n"]

        if not self._health_data:
            lines.append("*Health data service not available.*\n")
            return "\n".join(lines)

        try:
            snap = await self._health_data.snapshot()
        except Exception:
            logger.error("Failed to get health snapshot", exc_info=True)
            lines.append("*Health snapshot failed.*\n")
            return "\n".join(lines)

        # Infrastructure
        infra = snap.get("infrastructure", {})
        lines.append("### Infrastructure")
        for key, val in sorted(infra.items()):
            if isinstance(val, dict):
                status = val.get("status", "?")
                latency = val.get("latency_ms")
                detail = f"{status}"
                if latency is not None:
                    detail += f" ({latency:.0f}ms)"
                lines.append(f"- **{key}**: {detail}")
            else:
                lines.append(f"- **{key}**: {val}")

        resilience = snap.get("resilience", "unknown")
        lines.append(f"\n**Composite state**: {resilience}")

        # Queues
        queues = snap.get("queues", {})
        if queues:
            lines.append("\n### Queues")
            deferred = queues.get("deferred_work_queue", {})
            dead_letter = queues.get("dead_letter_queue", {})
            if isinstance(deferred, dict):
                lines.append(
                    f"- Deferred work: {deferred.get('pending', 0)} pending"
                )
            if isinstance(dead_letter, dict):
                lines.append(
                    f"- Dead letter: {dead_letter.get('count', 0)} items"
                )

        # Surplus
        surplus = snap.get("surplus", {})
        if surplus:
            lines.append("\n### Surplus")
            lines.append(
                f"- Queue depth: {surplus.get('queue_depth', 0)}"
            )
            last_dispatch = surplus.get("last_dispatch")
            if last_dispatch:
                lines.append(f"- Last dispatch: {last_dispatch}")

        lines.append("")
        return "\n".join(lines)

    async def _settled_decisions_section(self, *, depth: str = "deep") -> str:
        """Settled user decisions for the Genesis ego — same contract as the
        user-ego section: rulings, not signals."""
        try:
            from genesis.db.crud import ego as ego_crud

            decisions, total = await ego_crud.list_active_decisions(
                self._db, ego_target="genesis_ego", limit=7,
            )
        except Exception:
            logger.warning("Failed to query settled decisions", exc_info=True)
            return (
                "## Settled Decisions\n\n"
                "*Settled decisions unavailable (query error — see logs).*\n"
            )

        if not decisions:
            return ""

        lines = ["## Settled Decisions\n"]
        lines.append(
            "*User rulings — constraints, not signals. Do not re-propose, "
            "re-litigate, or engineer workarounds for anything below. Only "
            "the user can supersede a ruling; if you believe circumstances "
            "have changed, ASK — never act on the assumption.*\n"
        )
        for d in decisions:
            when = (d.get("last_reaffirmed_at") or d.get("created_at") or "?")[:10]
            reaff = d.get("reaffirm_count") or 0
            marker = f" (reaffirmed ×{reaff})" if reaff else ""
            lines.append(f"- [{when}]{marker} {d.get('content', '?')[:400]}")
        if total > len(decisions):
            lines.append(
                f"\n*(+{total - len(decisions)} older active rulings not shown)*"
            )
        lines.append("")
        return "\n".join(lines)

    async def _signals_section(self, *, depth: str = "deep") -> str:
        """Recent awareness loop signal values with trend indicators."""
        lines = ["## Awareness Signals (latest tick)\n"]

        try:
            cursor = await self._db.execute(
                "SELECT signals_json, classified_depth, created_at "
                "FROM awareness_ticks "
                "ORDER BY created_at DESC LIMIT 3"
            )
            rows = await cursor.fetchall()
        except Exception:
            logger.error("Failed to query awareness_ticks", exc_info=True)
            lines.append("*No awareness data available.*\n")
            return "\n".join(lines)

        if not rows:
            lines.append("*No awareness ticks recorded.*\n")
            return "\n".join(lines)

        # Parse signals from the most recent tick (display) and previous
        # tick (trend comparison).
        current_row = rows[0]
        signals_json, classified_depth, created_at = current_row
        lines.append(f"**Last tick**: {created_at} (depth: {classified_depth})\n")

        signals = self._parse_signals_json(signals_json)

        # Build previous tick's signal values for trend comparison
        prev_values: dict[str, float] = {}
        if len(rows) >= 2:
            prev_signals = self._parse_signals_json(rows[1][0])
            for name, info in prev_signals.items():
                if isinstance(info, dict):
                    v = info.get("value")
                    if isinstance(v, (int, float)):
                        prev_values[name] = float(v)

        if signals:
            lines.append("| Signal | Value | Trend | Source |")
            lines.append("|--------|-------|-------|--------|")
            for sig_name, sig_info in sorted(signals.items()):
                if isinstance(sig_info, dict):
                    val = sig_info.get("value", "?")
                    src = sig_info.get("source", "?")
                    trend = self._signal_trend(sig_name, val, prev_values)
                    if isinstance(val, float):
                        val = f"{val:.3f}"
                    lines.append(
                        f"| {sig_name} | {val} | {trend} | {src} |"
                    )
                else:
                    lines.append(f"| {sig_name} | {sig_info} | \u2192 | ? |")

        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _parse_signals_json(signals_json: str | None) -> dict:
        """Parse signals_json (list or dict format) into a name-keyed dict."""
        try:
            signals = json.loads(signals_json) if signals_json else {}
        except (json.JSONDecodeError, TypeError):
            return {}
        if isinstance(signals, list):
            return {
                s["name"]: s
                for s in signals
                if isinstance(s, dict) and "name" in s
            }
        return signals if isinstance(signals, dict) else {}

    @staticmethod
    def _signal_trend(
        name: str,
        current_value: object,
        prev_values: dict[str, float],
    ) -> str:
        """Compute trend arrow: \u2191 (up), \u2193 (down), \u2192 (stable/new)."""
        if not isinstance(current_value, (int, float)):
            return "\u2192"
        curr = float(current_value)
        prev = prev_values.get(name)
        if prev is None:
            return "\u2192"  # New signal, no history
        delta = curr - prev
        if abs(delta) < 0.01:  # Within noise threshold
            return "\u2192"
        return "\u2191" if delta > 0 else "\u2193"

    async def _observations_section(self, *, depth: str = "deep") -> str:
        """Genesis-internal observations — system issues needing attention."""
        lines = ["## Unresolved Observations (last 48h, max 20)\n"]

        try:
            # Exclude user-world categories and user-only relevance tags.
            # Genesis ego sees: NULL category, :genesis, :both, and anything
            # not in _USER_WORLD_CATEGORIES that isn't :user tagged.
            exclude_placeholders = ",".join("?" for _ in _USER_WORLD_CATEGORIES)
            cursor = await self._db.execute(
                f"SELECT id, source, type, category, content, priority, created_at "  # noqa: S608 - literal SQL fragments; values bound as parameters
                f"FROM observations "
                f"WHERE resolved = 0 "
                f"AND created_at >= datetime('now', '-48 hours') "
                f"AND (category IS NULL "
                f"     OR (category NOT IN ({exclude_placeholders}) "
                f"         AND category NOT LIKE '%:user')) "
                f"AND type != 'escalation_to_user_ego' "
                f"ORDER BY "
                f"  CASE priority "
                f"    WHEN 'critical' THEN 0 "
                f"    WHEN 'high' THEN 1 "
                f"    WHEN 'medium' THEN 2 "
                f"    ELSE 3 "
                f"  END, "
                f"  (retrieved_count > 0), "
                f"  created_at DESC "
                f"LIMIT 20",
                tuple(_USER_WORLD_CATEGORIES),
            )
            rows = await cursor.fetchall()
        except Exception:
            logger.error("Failed to query observations", exc_info=True)
            lines.append("*Could not query observations.*\n")
            return "\n".join(lines)

        if not rows:
            lines.append("*No unresolved Genesis-internal observations.*\n")
            return "\n".join(lines)

        # Read-receipt (non-fatal): record that these observations were pulled
        # into the ego's reasoning context this cycle, so already-seen items
        # demote below unread ones next cycle (blares iff unread AND unresolved).
        try:
            from genesis.db.crud import observations as _obs_crud

            await _obs_crud.increment_retrieved_batch(self._db, [r[0] for r in rows])
        except Exception:
            logger.debug("Failed to record observation read-receipts", exc_info=True)

        lines.append(f"**{len(rows)} items** (sorted by priority):\n")
        for _id, source, obs_type, category, content, priority, _created_at in rows:
            short = content[:200] + "..." if len(content) > 200 else content
            short = short.replace("\n", " ")
            cat_str = f"/{category}" if category else ""
            lines.append(
                f"- [{priority}] **{source}{cat_str}** ({obs_type}): {short}"
            )

        # Count redirect observations requiring in-cycle investigation.
        # row[2] is the observation type (id=row[0], source=row[1], type=row[2]).
        redirect_count = sum(
            1 for row in rows
            if row[2] in ("cross_domain_redirect", "realist_redirect")
        )
        if redirect_count:
            lines.append(
                f"\n**{redirect_count} redirect{'s' if redirect_count != 1 else ''} "
                f"require in-cycle investigation.** Use your MCP tools "
                f"(health_status, observation_query, memory_recall) to "
                f"investigate each during THIS cycle. Then propose a "
                f"concrete fix or escalate findings to user ego.\n"
            )

        lines.append("")
        return "\n".join(lines)

    async def _follow_ups_section(self, *, depth: str = "deep") -> str:
        """Maintenance follow-ups for the Genesis ego."""
        lines = ["## Maintenance Follow-ups\n"]

        try:
            from genesis.db.crud import follow_ups as follow_up_crud

            actionable = await follow_up_crud.get_actionable(self._db)
        except Exception:
            logger.error("Failed to query follow-ups", exc_info=True)
            lines.append("*Could not query follow-ups.*\n")
            return "\n".join(lines)

        if not actionable:
            lines.append("*No follow-ups requiring attention.*\n")
            return "\n".join(lines)

        # Filter to maintenance/infrastructure follow-ups
        genesis_relevant = [
            fu for fu in actionable
            if fu.get("strategy") in ("scheduled_task", "surplus_task")
            or fu.get("source", "").startswith(("sentinel", "guardian", "health"))
        ]

        if not genesis_relevant:
            lines.append(
                f"*{len(actionable)} follow-ups exist but none are "
                f"Genesis-maintenance.*\n"
            )
            return "\n".join(lines)

        lines.append(f"**{len(genesis_relevant)} items**:\n")
        for fu in genesis_relevant[:10]:
            fid = fu.get("id", "?")
            status = fu.get("status", "?")
            priority = fu.get("priority", "medium")
            content = fu.get("content", "")[:200]
            content = content.replace("\n", " ")
            strategy = fu.get("strategy", "?")

            pin_tag = " [pinned]" if fu.get("pinned") else ""
            lines.append(f"- [id:{fid}] [{priority}/{status}]{pin_tag} **{strategy}**: {content}")

        lines.append("")
        return "\n".join(lines)

    async def _proposal_history_section(self, *, depth: str = "deep") -> str:
        """Recent proposal outcomes for self-calibration.

        Split into Active (pending/approved/executed) and Recently Tried
        (withdrawn/tabled/rejected/failed/expired) so active proposals
        are always visible regardless of withdrawn noise volume.
        """
        lines = ["## Active Proposals\n"]
        table_header = (
            "| Action | Content | Status | Response |\n"
            "|--------|---------|--------|----------|"
        )

        try:
            # Section 1: Active proposals (genesis ego only)
            cursor = await self._db.execute(
                "SELECT action_type, content, status, "
                "user_response, created_at "
                "FROM ego_proposals "
                "WHERE created_at >= datetime('now', '-7 days') "
                "AND status IN ('pending', 'approved', 'executed') "
                "AND (ego_source = 'genesis_ego_cycle' OR ego_source IS NULL) "
                "ORDER BY created_at DESC "
                "LIMIT 15"
            )
            active_rows = await cursor.fetchall()

            if not active_rows:
                lines.append("*No active proposals.*\n")
            else:
                lines.append(table_header)
                for action_type, content, status, response, _created in active_rows:
                    short = content[:80] + "..." if len(content) > 80 else content
                    short = short.replace("\n", " ").replace("|", "/")
                    resp = (response or "\u2014")[:50]
                    lines.append(
                        f"| {action_type} | {short} | {status} | {resp} |"
                    )
                lines.append("")

            # Section 2: Recently tried (genesis ego only)
            lines.append("## Recently Tried (do not re-propose)\n")
            cursor2 = await self._db.execute(
                "SELECT action_type, content, status, "
                "user_response, created_at "
                "FROM ego_proposals "
                "WHERE created_at >= datetime('now', '-7 days') "
                "AND status IN ('withdrawn', 'tabled', 'rejected', 'failed', 'expired') "
                "AND (ego_source = 'genesis_ego_cycle' OR ego_source IS NULL) "
                "ORDER BY created_at DESC "
                "LIMIT 10"
            )
            tried_rows = await cursor2.fetchall()

            if not tried_rows:
                lines.append("*No recently tried proposals.*\n")
            else:
                lines.append(table_header)
                for action_type, content, status, response, _created in tried_rows:
                    short = content[:80] + "..." if len(content) > 80 else content
                    short = short.replace("\n", " ").replace("|", "/")
                    resp = (response or "\u2014")[:50]
                    lines.append(
                        f"| {action_type} | {short} | {status} | {resp} |"
                    )
                lines.append("")

        except Exception:
            logger.warning("Failed to build proposal history section", exc_info=True)
            lines.append("*Proposal history unavailable (query error — see logs).*\n")

        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _proposal_age(iso_timestamp: str | None) -> str:
        """Short age label for a proposal: '<1h', '4h', '2d', etc."""
        if not iso_timestamp:
            return "?"
        try:
            dt = datetime.fromisoformat(iso_timestamp)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            delta = datetime.now(UTC) - dt
            if delta.days >= 1:
                return f"{delta.days}d"
            hours = int(delta.total_seconds() / 3600)
            if hours >= 1:
                return f"{hours}h"
            return "<1h"
        except (ValueError, TypeError):
            return "?"

    async def _proposal_board_section(self, *, depth: str = "deep") -> str:
        """Operational focus + pending ops proposals + approved."""
        from genesis.db.crud import ego as ego_crud

        lines: list[str] = []

        # ---- Fetch all pending proposals ----
        try:
            all_pending = await ego_crud.get_pending_queue(self._db, ego_source='genesis_ego_cycle')
        except Exception:
            logger.error("Failed to query pending proposals", exc_info=True)
            lines.append("## Operational Focus\n")
            lines.append("*Could not query pending proposals.*\n")
            return "\n".join(lines)

        board = [p for p in all_pending if p.get("rank") is not None]
        queue = [p for p in all_pending if p.get("rank") is None]

        # ---- Operational focus (ranked) ----
        lines.append("## Operational Focus\n")
        if board:
            lines.append(
                f"**{len(board)} proposal{'s' if len(board) != 1 else ''}** "
                f"on your board (ranked focus):\n"
            )
            for p in board:
                age = self._proposal_age(p.get("created_at"))
                content = (p.get("content") or "")[:150].replace("\n", " ")
                lines.append(
                    f"- [#{p['rank']}] **{p.get('action_type', '?')}** "
                    f"(id:{p['id']}) [{age}]: {content}"
                )
        else:
            lines.append("*No ranked operational proposals.*\n")

        # ---- Pending ops proposals (unranked) ----
        if queue:
            lines.append("\n## Pending Ops Proposals\n")
            lines.append(
                f"**{len(queue)} more proposal{'s' if len(queue) != 1 else ''}** "
                f"awaiting user decision (unranked):\n"
            )
            for p in queue:
                age = self._proposal_age(p.get("created_at"))
                content = (p.get("content") or "")[:150].replace("\n", " ")
                lines.append(
                    f"- (id:{p['id']}) [{age}] "
                    f"**{p.get('action_type', '?')}**: {content}"
                )

        # ---- Approved proposals ready for execution ----
        try:
            approved = await ego_crud.list_proposals(self._db, status="approved", limit=5, ego_source='genesis_ego_cycle')
        except Exception:
            approved = []

        if approved:
            lines.append(f"\n**{len(approved)} approved proposals** (ready for execution):\n")
            for p in approved:
                content = (p.get("content") or "")[:150].replace("\n", " ")
                lines.append(
                    f"- **{p.get('action_type', '?')}** (id:{p['id']}): {content}"
                )
            lines.append(
                "\nYou can execute these by outputting execution_briefs "
                "with the proposal_id and dispatch instructions.\n"
            )
        else:
            lines.append("\n*No approved proposals awaiting execution.*\n")

        lines.append("")
        return "\n".join(lines)

    async def _execution_outcomes_section(self, *, depth: str = "deep") -> str:
        """Recent outcomes from ego-dispatched background sessions."""
        lines = ["## Recent Execution Outcomes (48h)\n"]

        try:
            cursor = await self._db.execute(
                "SELECT content, priority, created_at FROM observations "
                "WHERE type = 'execution_outcome' AND source = 'ego_dispatch' "
                "AND created_at >= datetime('now', '-48 hours') "
                "ORDER BY created_at DESC LIMIT 10"
            )
            rows = await cursor.fetchall()
        except Exception:
            logger.error("Failed to query execution outcomes", exc_info=True)
            lines.append("*Could not query execution outcomes.*\n")
            return "\n".join(lines)

        if not rows:
            lines.append("*No ego dispatch outcomes in last 48h.*\n")
            return "\n".join(lines)

        lines.append(f"**{len(rows)} outcomes**:\n")
        for content, priority, created_at in rows:
            short = (content or "")[:200].replace("\n", " ")
            ts = (created_at or "?")[:16]
            lines.append(f"- [{ts}] [{priority}] {short}")

        lines.append("")
        return "\n".join(lines)

    async def _confidence_calibration_section(self, *, depth: str = "deep") -> str:
        """The ego's own confidence calibration — so it can self-correct.

        Informational context for the ``confidence`` field (rendered right before
        the output contract), NOT a limiter and NOT a mechanical rescale. Reads
        ``ego_calibration_snapshots`` ONLY (never ``calibration_curves`` — that table
        is auto-injected into the perception context). Genesis ego only for v1 — the
        aggregate calibration is genesis-ego dominated; per-ego split is future work.

        Live flag ``EgoConfig.calibration_injection_enabled`` (default ON) is read
        fresh each cycle, so toggling it takes effect next cycle without a restart.
        Must be ``async`` (calls async ``get_latest``). Not in ``_ALL_SECTIONS``
        (that table is user-ego only); the genesis section_map drives it directly.
        """
        try:
            from genesis.ego.config import load_ego_config

            cfg = load_ego_config()
            # Default ON: disable ONLY on an explicit False (a YAML null / missing
            # key / truthy value all keep it on, so it can't be silently disabled).
            if getattr(cfg, "calibration_injection_enabled", True) is False:
                return ""

            from genesis.db.crud import ego_calibration as cal_crud
            from genesis.feedback.calibration import format_calibration_section

            snapshot = await cal_crud.get_latest(self._db, domain="ego")
            return format_calibration_section(snapshot, depth=depth)
        except aiosqlite.OperationalError:
            # Expected before PR-B is deployed (table absent) — not an error.
            logger.debug("ego_calibration_snapshots not yet available")
            return ""
        except Exception:
            logger.warning("Failed to build confidence calibration section", exc_info=True)
            return "## Confidence Calibration\n\n*Calibration unavailable (query error — see logs).*\n"

    async def _capability_performance_section(self, *, depth: str = "deep") -> str:
        """System capability performance — domain confidence from multiple sources."""
        lines = ["## Capability Performance\n"]

        try:
            from genesis.db.crud import capability_map as cap_crud

            entries = await cap_crud.get_all(self._db)
        except Exception:
            logger.warning("Failed to query capability performance", exc_info=True)
            lines.append(
                "*Capability performance unavailable (query error — see logs).*\n"
            )
            return "\n".join(lines)

        if not entries:
            lines.append("*No performance data yet.*\n")
            return "\n".join(lines)

        _TREND_ICONS = {"improving": "+", "declining": "-", "stable": "="}

        # Focused deficiency: a capability_improvement cycle targets a specific
        # weak domain (self._focus_id). get_all is confidence-DESC and only the
        # top 15 render below, so a low-confidence target is otherwise absent —
        # surface its full row explicitly so the advisory names a deficiency the
        # ego can actually see.
        focus_id = getattr(self, "_focus_id", None)
        focused = (
            next((e for e in entries if e.get("domain") == focus_id), None)
            if focus_id
            else None
        )
        if focused is not None:
            _fc = focused.get("confidence", 0.0)
            _ft = focused.get("trend", "stable")
            _fs = focused.get("sample_size", 0)
            _fe = (focused.get("evidence_summary") or "")[:120].replace("|", "/")
            lines.append(
                f"**Focused deficiency — {focus_id}**: {_fc:.0%} confidence "
                f"({_ft}, {_fs} samples). {_fe}\n"
            )

        lines.append("| Domain | Confidence | Trend | Samples | Evidence |")
        lines.append("|--------|-----------|-------|---------|----------|")
        for e in entries[:15]:
            domain = e.get("domain", "?")
            conf = e.get("confidence", 0.0)
            trend = e.get("trend", "stable")
            samples = e.get("sample_size", 0)
            evidence = (e.get("evidence_summary") or "")[:80].replace("|", "/")
            icon = _TREND_ICONS.get(trend, "=")
            lines.append(
                f"| {domain} | {conf:.0%} | {icon} | {samples} | {evidence} |"
            )

        lines.append(
            "\nDeclining domains may need investigation. Improving domains "
            "indicate effective maintenance patterns.\n"
        )
        return "\n".join(lines)


    async def _own_goals_section(self, *, depth: str = "deep") -> str:
        """The genesis ego's OWN goal lane (origin='genesis_ego').

        Renders active + paused own goals with staleness annotations. This
        section is what makes own-goal review non-blind — the ego only ever
        reviews goals it can see here — and doubles as the dedupe context
        for own_goal_creations. User goals are deliberately absent: they are
        user-ego jurisdiction (user_context renders those, filtered to
        origin='user').
        """
        try:
            cursor = await self._db.execute(
                "SELECT * FROM user_goals WHERE origin = 'genesis_ego' "
                "AND status IN ('active', 'paused') "
                "ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, "
                "updated_at ASC LIMIT 30",
            )
            goals = [dict(r) for r in await cursor.fetchall()]
        except Exception:
            logger.error("Failed to query own goals", exc_info=True)
            return "## Your Own Goals\n\n*Could not query own goals.*\n"

        lines = ["## Your Own Goals\n"]
        if not goals:
            lines.append(
                "*None yet. When an operational objective needs tracking "
                "across cycles, you may create one via `own_goal_creations` "
                "(see Output Contract).*\n"
            )
            return "\n".join(lines)

        n_active = sum(1 for g in goals if g["status"] == "active")
        n_paused = len(goals) - n_active
        lines.append(f"*{n_active} active, {n_paused} paused.*\n")
        if depth == "light":
            return "\n".join(lines)

        # Staleness threshold mirrors the goal-review default: per-goal
        # cadence_days override, else the EgoConfig dataclass default.
        from genesis.ego.types import EgoConfig

        default_days = EgoConfig.goal_review_staleness_days
        now = datetime.now(UTC)
        for g in goals:
            updated_at = g.get("updated_at") or g.get("created_at") or ""
            days: int | str = "?"
            stale_tag = ""
            try:
                updated = datetime.fromisoformat(updated_at)
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=UTC)
                days = (now - updated).days
                per_goal = g.get("cadence_days")
                threshold = (
                    per_goal
                    if isinstance(per_goal, int) and per_goal > 0
                    else default_days
                )
                if g["status"] == "active" and days >= threshold:
                    stale_tag = " — STALE, review due (own_goal_reviews)"
            except (ValueError, TypeError):
                pass
            lines.append(
                f"- [{g['status'].upper()}] "
                f"**{(g.get('title') or '?')[:100]}** "
                f"(id: {g['id']}, {g.get('priority', 'medium')}, "
                f"{g.get('category', '?')}, updated {days}d ago){stale_tag}"
            )
        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _output_contract_section(*, depth: str = "deep") -> str:
        """Remind the Genesis ego of its output format.

        Includes the escalation field for issues beyond its scope.
        """
        return (
            "## Output Contract\n\n"
            "You MUST respond with valid JSON matching this structure:\n"
            "```json\n"
            "{\n"
            '  "proposals": [\n'
            "    {\n"
            '      "action_type": "investigate|maintenance|config",\n'
            '      "action_category": "system_health|infrastructure|performance",\n'
            '      "content": "what you want to do",\n'
            '      "rationale": "why",\n'
            '      "confidence": 0.85,\n'
            '      "urgency": "low|normal|high|critical",\n'
            '      "alternatives": "what else you considered"\n'
            "    }\n"
            "  ],\n"
            '  "notifications": [\n'
            "    {\n"
            '      "content": "what to tell the user (informational, no approval needed)",\n'
            '      "urgency": "low|normal|high"\n'
            "    }\n"
            "  ],\n"
            '  "escalations": [\n'
            "    {\n"
            '      "content": "issue description the user ego should see",\n'
            '      "context": "what you tried, why it needs user ego",\n'
            '      "suggested_action": "what you recommend"\n'
            "    }\n"
            "  ],\n"
            '  "focus_summary": "one-line: what Genesis is focused on",\n'
            '  "resolved_follow_ups": [{"id": "follow_up_id", "resolution": "why resolved"}],\n'
            '  "intentions": {"review": [{"id": "...", "action": "keep|fire|withdraw|renew"}], '
            '"new": [{"content": "...", "trigger_condition": "...", "reasoning": "..."}]},\n'
            '  "communication_decision": "send_digest|urgent_notify|stay_quiet",\n'
            '  "own_goal_creations": [{"title": "...", "description": "...", '
            '"category": "project|learning|other", "priority": "low|medium|high", '
            '"goal_type": "milestone|continuous", "cadence_days": 14}],\n'
            '  "own_goal_reviews": [{"goal_id": "...", '
            '"recommendation": "continue|pause|deprioritize|close", '
            '"assessment": "..."}]\n'
            "}\n"
            "```\n\n"
            "If you cannot resolve an issue, add it to escalations — "
            "the user ego will see it and decide what the user needs to know.\n\n"
            "No morning_report — that belongs to the user ego.\n\n"
            "own_goal_creations / own_goal_reviews touch ONLY your own lane "
            "(origin=genesis_ego, listed under 'Your Own Goals'): max 1 "
            "creation per cycle and a cap on active own goals; reviews of "
            "anything not in that list are ignored. Omit both keys when "
            "nothing changes.\n"
        )
