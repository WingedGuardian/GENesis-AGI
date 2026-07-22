"""Morning report generator — daily system state synthesis."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from genesis.content.drafter import ContentDrafter
from genesis.content.types import DraftRequest, FormatTarget
from genesis.db.crud.observations import INTERNAL_OBS_TYPES as _INTERNAL_OBS_TYPES_SET
from genesis.outreach.types import OutreachCategory, OutreachRequest

logger = logging.getLogger(__name__)

# Convert to tuple for db.execute() compatibility (requires sequence, not frozenset)
_INTERNAL_OBS_TYPES = tuple(_INTERNAL_OBS_TYPES_SET)

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "identity" / "MORNING_REPORT.md"


def _relative_age(iso_ts: str) -> str:
    """Convert an ISO timestamp to a human-readable relative age string."""
    if not iso_ts:
        return "unknown age"
    try:
        ts = datetime.fromisoformat(iso_ts)
        delta = datetime.now(UTC) - ts
        total_s = delta.total_seconds()
        if total_s < 0:
            return "just now"
        if total_s < 3600:
            return f"{int(total_s / 60)}m ago"
        if total_s < 86400:
            return f"{total_s / 3600:.0f}h ago"
        return f"{total_s / 86400:.0f}d ago"
    except (ValueError, TypeError):
        return "unknown age"


def _age_seconds(iso_ts: str) -> float:
    """Return seconds since the given ISO timestamp, or 0 on error."""
    try:
        return (datetime.now(UTC) - datetime.fromisoformat(iso_ts)).total_seconds()
    except (ValueError, TypeError):
        return 0


# Observations older than this in "What I Noticed" are historical signals, not
# current conditions: their DISPLAYED severity is demoted one level and tagged
# so a days-old write (e.g. a stale quality_drift) doesn't read as a fresh
# critical alarm. Stored priority is never changed.
_STALE_OBS_SECONDS = 3 * 86400
_STALE_PRIORITY_DEMOTION = {"critical": "high", "high": "medium", "medium": "medium"}


# ── Capability-build lane report helpers ──────────────────────────────────

_BUILD_PR_CI_TIMEOUT_S = 20  # per-PR gh call; PRs are checked concurrently
_MAX_BUILD_PR_CHECKS = 10    # cap concurrent gh calls (rate-limit safety)


async def _pr_ci_status(pr_url: str) -> str | None:
    """One-shot CI rollup for a draft build PR via
    ``gh pr view <url> --json statusCheckRollup``. Returns a compact word
    ('passing'/'failing'/'pending'/'no checks'), or None on error/timeout.

    Uses the shared ``run_gh`` helper, which kills+reaps the child process on
    timeout (the process-kill sequence is easy to get subtly wrong, so it is
    not re-implemented here).
    """
    import json

    from genesis.recon.gh_cli import run_gh

    if not pr_url:
        return None
    raw = await run_gh(
        "gh", "pr", "view", pr_url, "--json", "statusCheckRollup",
        timeout=_BUILD_PR_CI_TIMEOUT_S,
    )
    if not raw:
        return None
    try:
        checks = json.loads(raw).get("statusCheckRollup") or []
    except (ValueError, TypeError):
        return None
    return _summarize_ci_rollup(checks)


def _summarize_ci_rollup(checks: list) -> str:
    """Collapse a heterogeneous statusCheckRollup (CheckRun + StatusContext)
    into one word: failing > pending > passing."""
    if not checks:
        return "no checks"
    _FAIL = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED",
             "STARTUP_FAILURE", "ERROR"}
    _OK = {"SUCCESS", "NEUTRAL", "SKIPPED"}
    saw_fail = saw_pending = False
    for c in checks:
        conclusion = (c.get("conclusion") or "").upper()
        state = (c.get("state") or "").upper()  # StatusContext form
        if conclusion in _FAIL or state in {"FAILURE", "ERROR"}:
            saw_fail = True
        elif conclusion in _OK or state == "SUCCESS":
            continue
        else:
            # COMPLETED-without-conclusion, QUEUED, IN_PROGRESS, PENDING, ...
            saw_pending = True
    if saw_fail:
        return "failing"
    if saw_pending:
        return "pending"
    return "passing"


def _format_build_calibration(counts: list[dict]) -> list[str]:
    """Per-verdict agreement lines from verdict×user_decision counts.
    dont_build is reported as 'uncontested' (never carded), never as agreement.
    """
    agg: dict[str, dict[str, int]] = {}
    for row in counts:
        verdict = row.get("verdict") or "?"
        decision = row.get("user_decision") or "pending"
        agg.setdefault(verdict, {})[decision] = row.get("count", 0)
    lines: list[str] = []
    if "build" in agg:
        b = agg["build"]
        approved = b.get("approved", 0)
        rejected = b.get("rejected", 0)
        pending = b.get("pending", 0)
        decided = approved + rejected
        rate = f"{approved}/{decided} approved" if decided else "none decided yet"
        extra = f", {pending} pending your tap" if pending else ""
        lines.append(f"- build verdicts: {rate}{extra}")
    if "dont_build" in agg:
        total = sum(agg["dont_build"].values())
        lines.append(
            f"- dont_build verdicts: {total} "
            "(uncontested — reported, never carded)"
        )
    if "needs_discussion" in agg:
        total = sum(agg["needs_discussion"].values())
        lines.append(f"- needs_discussion verdicts: {total}")
    return lines


class MorningReportGenerator:
    """Synthesizes system state into a daily morning report."""

    def __init__(
        self,
        health_data: object,
        db: aiosqlite.Connection,
        drafter: ContentDrafter,
        *,
        event_bus: object | None = None,
    ) -> None:
        self._health = health_data
        self._db = db
        self._drafter = drafter
        self._event_bus = event_bus
        self._pending_surface_ids: list[str] = []
        self._pending_mq_ids: list[str] = []

    async def generate(self) -> OutreachRequest:
        context = await self._assemble_context()
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        topic = f"Morning Report — {today}"

        system_prompt = self._load_system_prompt()

        draft = await self._drafter.draft(
            DraftRequest(
                topic=topic,
                context=context,
                target=FormatTarget.GENERIC,
                tone="concise and informative",
                max_length=None,
                system_prompt=system_prompt,
            ),
            # 13_morning_report — daily morning report generation. Free-chain.
            call_site_id="13_morning_report",
        )

        return OutreachRequest(
            category=OutreachCategory.DIGEST,
            topic=topic,
            context=draft.content.text,
            salience_score=0.0,
            signal_type="morning_report",
            # The draft above ran with the MORNING_REPORT.md system prompt
            # against grounded context. Without verbatim, the pipeline
            # re-drafts it through the generic drafter (system_prompt=None),
            # un-grounding every number. Formatter still applies.
            verbatim=True,
        )

    async def confirm_delivery(self) -> None:
        """Mark surfaced items as consumed after successful delivery.

        Called by the scheduler after the pipeline confirms delivery.
        Items collected during generate() are only closed here — if
        delivery fails, they re-appear in the next report.

        Observations: only calls mark_surfaced — retrieved/influenced
        tracking is handled by _get_activity_summary to avoid
        double-counting.

        Message-queue rows rendered into the report are marked responded;
        nothing else ever closes them (the 7-day expiry job is the only
        other exit, which conflates "seen in a delivered report" with
        "never seen at all").
        """
        now = datetime.now(UTC).isoformat()

        ids = self._pending_surface_ids
        if ids:
            from genesis.db.crud.observations import mark_surfaced

            await mark_surfaced(self._db, ids, now)
            self._pending_surface_ids = []

        mq_ids = self._pending_mq_ids
        if mq_ids:
            from genesis.db.crud import message_queue as mq_crud

            for mq_id in mq_ids:
                await mq_crud.set_response(
                    self._db,
                    mq_id,
                    response="surfaced_in_morning_report",
                    responded_at=now,
                )
            self._pending_mq_ids = []

    @staticmethod
    def _load_system_prompt() -> str | None:
        try:
            return _PROMPT_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.warning("MORNING_REPORT.md not found at %s", _PROMPT_PATH)
            return None

    async def _assemble_context(self) -> str:
        sections: list[str] = []

        # 0. Ground truth — deterministic totals, assembled before any list
        # section. List sections below are TRUNCATED SAMPLES; these numbers
        # are the authoritative counts the report must restate exactly.
        try:
            ground = await self._ground_truth_section()
            sections.append(ground)
        except Exception:
            logger.warning("Morning report: ground truth unavailable", exc_info=True)
            await self._emit_warning("ground_truth", "Ground-truth totals unavailable")

        # 1. System Health
        try:
            health = await self._health.snapshot()
            sections.append(self._format_health(health))
        except Exception as exc:
            sections.append(f"## System Health\nData unavailable: {exc}")
            await self._emit_warning("health_snapshot", "Health snapshot unavailable")

        # 2. Activity (sessions, user-relevant observations)
        try:
            activity = await self._get_activity_summary()
            sections.append(f"## Activity (last 24h)\n{activity}")
        except Exception:
            logger.warning("Morning report: activity summary unavailable", exc_info=True)
            sections.append("## Activity (last 24h)\nNo data")

        # 3. Cognitive State (with audience context for the LLM)
        try:
            cog = await self._get_cognitive_state()
            sections.append(f"## Cognitive State\n{cog}")
        except Exception:
            logger.warning("Morning report: cognitive state unavailable", exc_info=True)
            await self._emit_warning("cognitive_state", "Cognitive state section unavailable")

        # 3b. Cognitive subsystem quality grades (weekly J9 grades; neutral
        # observability — the regression alarm is a separate deterministic path)
        try:
            eval_quality = await self._get_eval_quality_section()
            if eval_quality:
                sections.append(f"## Cognitive Subsystem Grades\n{eval_quality}")
        except Exception:
            logger.warning("Morning report: eval quality section unavailable", exc_info=True)
            await self._emit_warning("eval_quality", "Cognitive subsystem grades section unavailable")

        # 4. Pending Items (user-actionable only)
        try:
            pending = await self._get_pending_items()
            sections.append(f"## Pending Items\n{pending}")
        except Exception:
            logger.warning("Morning report: pending items unavailable", exc_info=True)
            await self._emit_warning("pending_items", "Pending items section unavailable")

        # 5. Follow-ups (user-actionable + blocked + recently completed)
        try:
            followups = await self._get_follow_ups_summary()
            if followups:
                sections.append(f"## Follow-ups\n{followups}")
        except Exception:
            logger.warning("Morning report: follow-ups unavailable", exc_info=True)

        # 5b. Inbox follow-ups resolved by ego (weekly safety net)
        try:
            inbox_resolved = await self._get_inbox_resolved_digest()
            if inbox_resolved:
                sections.append(f"## Inbox Follow-ups (Ego-resolved)\n{inbox_resolved}")
        except Exception:
            logger.warning("Morning report: inbox resolved digest unavailable", exc_info=True)

        # 5c. Capability-build lane (open draft build PRs + calibration)
        try:
            build_lane = await self._get_build_lane_section()
            if build_lane:
                sections.append(f"## Capability Build Lane\n{build_lane}")
        except Exception:
            logger.warning("Morning report: build lane section unavailable", exc_info=True)
            await self._emit_warning("build_lane", "Capability build lane section unavailable")

        # 6. Outreach summary (just total count, no self-analysis)
        try:
            engagement = await self._get_engagement_summary()
            sections.append(f"## Outreach (7 days)\n{engagement}")
        except Exception:
            logger.warning("Morning report: engagement summary unavailable", exc_info=True)
            await self._emit_warning("engagement_summary", "Engagement summary section unavailable")

        # 7. Critical Issues (only if WARNING+ alerts are active)
        try:
            critical_issues = await self._get_critical_issues()
            if critical_issues:
                sections.append(f"## Critical Issues\n{critical_issues}")
        except Exception as exc:
            logger.warning("Morning report: critical issues check failed", exc_info=True)
            sections.append(f"## Critical Issues\nFailed to query health alerts: {exc}")

        # 8. What I Noticed (unsurfaced observations worth user attention)
        try:
            noticed = await self._get_observation_insights()
            if noticed:
                sections.append(f"## What I Noticed\n{noticed}")
        except Exception:
            logger.warning("Morning report: observation insights unavailable", exc_info=True)

        # 9. Standing Items (observations surfaced 3+ times, still unresolved)
        try:
            standing = await self._get_standing_items()
            if standing:
                sections.append(f"## Standing Items\n{standing}")
        except Exception:
            logger.warning("Morning report: standing items unavailable", exc_info=True)

        return "\n\n".join(sections)

    async def _ground_truth_section(self) -> str:
        """Deterministic SQL totals — the report's authoritative numbers.

        Every count here is a full COUNT/len over the un-truncated query,
        never the length of a display slice. The system prompt instructs
        the drafter to restate these exactly and never derive totals from
        the (truncated) list sections.
        """
        from genesis.db.crud import approval_requests as approval_crud
        from genesis.db.crud import ego as ego_crud
        from genesis.db.crud import follow_ups
        from genesis.db.crud.observations import count_unsurfaced

        lines = [
            "## Ground Truth (authoritative totals — restate these EXACTLY; "
            "the list sections below are truncated samples)",
        ]

        # Same queries the list sections run (un-truncated lens), so these
        # totals can never disagree with the samples below.
        try:
            needs_input = len(await follow_ups.get_pending(
                self._db, strategy="user_input_needed", domain="user_world",
            ))
            pending_all = len(await follow_ups.get_pending(
                self._db, domain="user_world",
            ))
            blocked = len(await follow_ups.get_by_status(
                self._db, "blocked", domain="user_world",
            ))
            failed = len(await follow_ups.get_by_status(
                self._db, "failed", domain="user_world",
            ))
            lines.append(
                f"- Follow-ups (user-world): {needs_input} awaiting your "
                f"input, {pending_all} pending total, {blocked} blocked, "
                f"{failed} failed"
            )
        except Exception:
            logger.warning("Ground truth: follow-up counts failed", exc_info=True)

        try:
            from genesis.ego.types import partition_informational

            raw_props = await ego_crud.list_pending_proposals(self._db)
            # Informational eval rows (j9/gauntlet) aren't approval work.
            proposals, _informational = partition_informational(raw_props)
            lines.append(f"- Pending ego proposals: {len(proposals)}")
        except Exception:
            logger.warning("Ground truth: proposal count failed", exc_info=True)

        try:
            approvals = await approval_crud.list_pending(self._db)
            lines.append(f"- Pending approval requests: {len(approvals)}")
        except Exception:
            logger.warning("Ground truth: approval count failed", exc_info=True)

        try:
            unsurfaced = await count_unsurfaced(
                self._db,
                priority_filter=("critical", "high", "medium"),
                exclude_types=_INTERNAL_OBS_TYPES,
            )
            lines.append(f"- Unsurfaced observations (worth attention): {unsurfaced}")
        except Exception:
            logger.warning("Ground truth: observation count failed", exc_info=True)

        return "\n".join(lines)

    async def _emit_warning(self, section: str, message: str) -> None:
        if not self._event_bus:
            return
        try:
            from genesis.observability.types import Severity, Subsystem

            await self._event_bus.emit(
                Subsystem.OUTREACH, Severity.WARNING,
                "morning_report.section_failed",
                message,
                section=section,
            )
        except Exception:
            pass  # Don't let observability failures break the report

    def _format_health(self, health: dict) -> str:
        cost = health.get("cost", {})
        queues = health.get("queues", {})
        infra = health.get("infrastructure", {})
        surplus = health.get("surplus", {})
        awareness = health.get("awareness", {})
        cc = health.get("cc_sessions", {})
        # cc_sessions snapshot nests foreground/background as dicts
        # (e.g. {"foreground": {"active": 2}}). Handle both nested and
        # flat formats defensively for backward-compatible test fixtures.
        fg = cc.get("foreground", 0)
        fg_active = fg.get("active", 0) if isinstance(fg, dict) else fg
        bg = cc.get("background", 0)
        bg_active = bg.get("active", 0) if isinstance(bg, dict) else bg
        lines = [
            "## System Health",
            self._format_cost_line(cost),
            f"- Infrastructure: DB={infra.get('genesis.db', {}).get('status', '?')}, Qdrant={infra.get('qdrant', {}).get('status', '?')}",
            f"- Queues: deferred={queues.get('deferred_work', 0)}, dead_letters={queues.get('dead_letters', 0)}, pending_embeddings={queues.get('pending_embeddings', 0)}",
            f"- Surplus: {surplus.get('status', '?')}, queue_depth={surplus.get('queue_depth', 0)}",
            f"- Awareness: ticks_24h={awareness.get('ticks_24h', '?')}",
            f"- CC Sessions: foreground={fg_active}, background={bg_active}, failed_24h={cc.get('failed_24h', 0)}",
        ]
        pending_embed = queues.get('pending_embeddings', 0)
        if pending_embed and pending_embed > 100:
            lines.append(f"- **Embedding queue elevated**: {pending_embed} pending")
        return "\n".join(lines)

    @staticmethod
    def _format_cost_line(cost: dict) -> str:
        """One neutral, grounded cost line: month-to-date spend against the
        monthly cap. Real numbers only — no projection, no daily figure, no
        spike alarm. Cost is observability, not control; the user decides
        tradeoffs. The budget system (cost_tracker events) owns alarms, not
        this report.
        """
        spend = cost.get("monthly_usd")
        if spend is None:
            return "- Spend: unavailable"
        cap = cost.get("budget_monthly_limit")
        pct = cost.get("budget_pct_used")
        # A falsy cap (None when the budget query failed, or a nonsensical $0)
        # drops the cap clause rather than printing a misleading "$0 cap".
        if cap:
            pct_txt = f"{pct:.0f}% of " if pct is not None else ""
            return f"- Spend: ${spend:.2f} MTD, {pct_txt}${cap:.0f} cap"
        return f"- Spend: ${spend:.2f} MTD"

    async def _get_activity_summary(self) -> str:
        from genesis.db.crud import cc_sessions as sessions_crud
        from genesis.db.crud import inbox_items as inbox_crud
        from genesis.db.crud import observations as obs_crud
        from genesis.db.crud import user_goals as goals_crud

        lines = []

        # CC Sessions in last 24h
        status_counts = await sessions_crud.get_status_counts(self._db, hours=24)
        if status_counts:
            parts = [f"{s}={c}" for s, c in status_counts.items()]
            lines.append(f"- CC sessions: {', '.join(parts)}")
        else:
            lines.append("- CC sessions: none in last 24h")

        # Session topics (foreground only, last 24h) — what was actually worked on
        topics = await sessions_crud.get_recent_topics(self._db, hours=24, limit=15)
        if topics:
            lines.append("- Session topics (foreground):")
            for t in topics:
                lines.append(f"  - {t[:120]}")

        # Active user goals — enables the LLM to note priority alignment/drift.
        # origin="user": ego-owned goals are not user goals (counted below).
        goal_rows = await goals_crud.list_active(self._db, limit=10, origin="user")
        if goal_rows:
            lines.append("- Active user goals:")
            for g in goal_rows:
                lines.append(f"  - [{g['priority']}] ({g['category']}) {g['title']}")

        # Genesis's own goals (origin='genesis_ego', additive ego autonomy) —
        # ambient visibility + the paused-tail watch surface. Compact count
        # only; autonomous actions get their own report lines via the
        # goal_autonomous_action observation surfacing.
        try:
            ego_goal_counts = await goals_crud.count_by_status(
                self._db, origin="genesis_ego",
            )
            n_active = ego_goal_counts.get("active", 0)
            n_paused = ego_goal_counts.get("paused", 0)
            if n_active or n_paused:
                lines.append(
                    f"- Genesis's own goals: {n_active} active, {n_paused} paused"
                )
        except Exception:
            logger.warning("Failed to count Genesis-ego goals", exc_info=True)

        # Inbox items — only report genuinely pending DB items
        pending_inbox = await inbox_crud.count_pending(self._db)
        if pending_inbox > 0:
            lines.append(f"- Inbox: {pending_inbox} items awaiting evaluation")

        # User-relevant observations with content preview (top 5)
        obs_rows = await obs_crud.get_unsurfaced(
            self._db,
            priority_filter=("high", "medium", "low"),
            exclude_types=_INTERNAL_OBS_TYPES,
            limit=5,
        )
        if obs_rows:
            lines.append("- User-relevant observations (unresolved):")
            obs_ids = []
            for r in obs_rows:
                obs_ids.append(r["id"])
                content_preview = (r["content"] or "?")[:120]
                lines.append(f"  - [{r['priority']}] {r['type']}: {content_preview}")

            # Track retrieval and influence
            try:
                await obs_crud.increment_retrieved_batch(self._db, obs_ids)
                await obs_crud.mark_influenced_batch(self._db, obs_ids)
            except Exception:
                logger.warning("Failed to track morning report observation consumption", exc_info=True)

        # Genesis-internal observation count (single summary line)
        internal_count = await obs_crud.count_unresolved_by_types(
            self._db, types=_INTERNAL_OBS_TYPES,
        )
        if internal_count:
            lines.append(
                f"- Genesis internal: {internal_count} items tracked "
                "(reflections, awareness events, etc. — no user action needed)"
            )

        return "\n".join(lines) if lines else "No activity data."

    async def _get_cognitive_state(self) -> str:
        from genesis.db.crud import cognitive_state as cs_crud

        rows = await cs_crud.get_recent_active(self._db, hours=24, limit=10)
        if not rows:
            return "No active cognitive state entries (all >24h old — skipped)."
        header = (
            "Note: These are Genesis's INTERNAL state entries. Do NOT present\n"
            "them as action items or quote them. At most, summarize in one line:\n"
            "'Genesis is tracking N internal items.' Skip entirely if nothing\n"
            "requires user awareness.\n"
        )
        entry_lines = []
        for r in rows:
            age = _relative_age(r["created_at"])
            aging_tag = " [AGING]" if _age_seconds(r["created_at"]) > 43200 else ""  # >12h
            entry_lines.append(f"- [{r['section']}]{aging_tag} {r['content'][:300]} ({age})")
        return header + "\n" + "\n".join(entry_lines)

    async def _get_eval_quality_section(self) -> str | None:
        """Weekly cognitive-subsystem quality grades (A–F) — neutral readout.

        This is OBSERVABILITY, not an alarm channel: a grade *regression* is
        surfaced separately and deterministically at aggregation time, not via
        this LLM-narrated report. Returns None when no graded subsystem exists
        (cold start / insufficient data) so the section is skipped entirely.
        Subsystems with no letter grade (``cognitive_drift`` is dark by design;
        sparse weeks grade to None) are omitted — never shown as a problem.
        """
        from genesis.db.crud import j9_eval

        grades = await j9_eval.get_latest_subsystem_grades(self._db)
        graded = [g for g in grades if g.get("grade")]
        if not graded:
            return None

        lines = [
            "Weekly cognitive-subsystem grades. Mention a subsystem ONLY if it "
            "is at D/F or notably low; otherwise compress to 'cognitive "
            "subsystems nominal'. Weekly cadence — do not repeat unchanged "
            "grades in the daily report.",
        ]
        for g in sorted(graded, key=lambda x: x.get("subsystem", "")):
            sub = g.get("subsystem", "?")
            grade = g.get("grade", "?")
            score = g.get("score")
            score_txt = f" ({score:.0f})" if isinstance(score, (int, float)) else ""
            lines.append(f"- {sub}: {grade}{score_txt}")
        return "\n".join(lines)

    async def _get_build_lane_section(self) -> str | None:
        """Capability-build lane readout: open draft build PRs (+ one-shot CI),
        the wouldn't-build reasons, and per-verdict calibration vs the user's
        actual decisions. Returns None when the lane has no candidates yet
        (cold start) so the section is skipped entirely."""
        import asyncio

        from genesis.db.crud import build_candidates as bc

        open_prs = await bc.list_by_outcome(self._db, "pr_opened")
        dont_build = await bc.list_by_verdict(self._db, "dont_build", limit=10)
        counts = await bc.verdict_decision_counts(self._db)

        if not open_prs and not dont_build and not counts:
            return None

        lines: list[str] = [
            "Autonomous capability-build lane. State facts only. 'Uncontested' "
            "means an item was never carded for a decision, NOT that the user "
            "agreed. Surface open build PRs under Open Items / Next Steps.",
        ]

        if open_prs:
            checked = open_prs[:_MAX_BUILD_PR_CHECKS]
            ci_states = await asyncio.gather(
                *[_pr_ci_status(r.get("pr_url") or "") for r in checked]
            )
            lines.append("")
            lines.append("Open build PRs (draft, awaiting your review/merge):")
            for row, ci in zip(checked, ci_states, strict=True):
                title = row.get("item_title") or "(untitled)"
                pr_url = row.get("pr_url") or "(no url)"
                ci_txt = f" — CI {ci}" if ci else ""
                lines.append(f"- {title}: {pr_url}{ci_txt}")
            if len(open_prs) > _MAX_BUILD_PR_CHECKS:
                lines.append(
                    f"- (+{len(open_prs) - _MAX_BUILD_PR_CHECKS} more open build "
                    "PRs; CI not checked)"
                )

        if dont_build:
            lines.append("")
            lines.append(
                "Wouldn't-build (Genesis declined; reported, never queued):"
            )
            for row in dont_build:
                title = row.get("item_title") or "(untitled)"
                reason = row.get("verdict_reason") or "no reason recorded"
                lines.append(f"- {title}: {reason}")

        calibration = _format_build_calibration(counts)
        if calibration:
            lines.append("")
            lines.append("Calibration (Genesis verdict vs your decision):")
            lines.extend(calibration)

        return "\n".join(lines)

    async def _get_pending_items(self) -> str:
        from genesis.db.crud import approval_requests as approval_crud
        from genesis.db.crud import ego as ego_crud
        from genesis.db.crud import message_queue as mq_crud

        lines: list[str] = []

        # Message queue items. Rendered rows are collected so
        # confirm_delivery() can close them — without that they re-list
        # every morning until the 7-day expiry job silently drops them
        # (observed: 158 expired / 0 ever responded over 4 months).
        self._pending_mq_ids = []
        mq_rows = await mq_crud.query_pending(self._db)
        rendered_mq_ids: list[str] = []
        for r in mq_rows:
            content = r.get("content", "")
            if "Untitled" in content:
                continue
            lines.append(
                f"- [{r.get('message_type', '?')}] "
                f"priority={r.get('priority', '?')}, "
                f"from={r.get('source') or '?'}, "
                f"created={r.get('created_at', '?')}: {content[:200]}"
            )
            # Only fire-and-forget findings are closed on delivery.
            # question/decision/error rows belong to the checkpoint flow
            # (CheckpointManager.deliver_response) — closing them here
            # would swallow a background session's pending question.
            if r.get("id") and r.get("message_type") == "finding":
                rendered_mq_ids.append(r["id"])
        self._pending_mq_ids = rendered_mq_ids

        # Pending ego proposals (user needs to approve/reject on dashboard)
        try:
            from genesis.ego.types import partition_informational

            raw_props = await ego_crud.list_pending_proposals(self._db)
            # Informational eval rows (j9/gauntlet) aren't approve/reject work.
            proposals, _informational = partition_informational(raw_props)
            total_proposals = len(proposals)
            proposals = proposals[:5]
            if proposals:
                shown = (
                    f" (showing {len(proposals)} of {total_proposals})"
                    if total_proposals > len(proposals) else ""
                )
                lines.append(
                    f"- {total_proposals} pending ego proposal(s) "
                    f"(approve/reject on dashboard){shown}:"
                )
                for p in proposals:
                    lines.append(
                        f"  - {(p.get('content') or '?')[:150]} "
                        f"(urgency={p.get('urgency') or 'normal'})"
                    )
        except Exception:
            logger.warning(
                "Morning report: ego proposals query failed", exc_info=True,
            )

        # Pending approval requests
        try:
            approvals = await approval_crud.list_pending(self._db)
            total_approvals = len(approvals)
            approvals = approvals[:5]
            if approvals:
                shown = (
                    f" (showing {len(approvals)} of {total_approvals})"
                    if total_approvals > len(approvals) else ""
                )
                lines.append(
                    f"- {total_approvals} pending approval request(s){shown}:"
                )
                for a in approvals:
                    lines.append(f"  - {(a.get('description') or '?')[:150]}")
        except Exception:
            logger.warning(
                "Morning report: approval requests query failed",
                exc_info=True,
            )

        return "\n".join(lines) if lines else "No pending items."

    async def _get_follow_ups_summary(self) -> str | None:
        """Return follow-ups needing user attention + recently completed."""
        from genesis.db.crud import follow_ups

        lines = []

        # User-input-needed items. Strict user_world throughout: the morning
        # report is the USER's brief — internal-dev items (incl. internal
        # failed/blocked) belong to the genesis ego / health, not here.
        user_items = await follow_ups.get_pending(
            self._db, strategy="user_input_needed", domain="user_world",
        )
        if user_items:
            shown = (
                f" (showing 5 of {len(user_items)})" if len(user_items) > 5 else ""
            )
            lines.append(f"**Needs your input ({len(user_items)} total){shown}:**")
            for fu in user_items[:5]:
                lines.append(
                    f"- {fu['content'][:200]} ({_relative_age(fu.get('created_at', ''))})"
                )

        # Blocked/failed items
        blocked = await follow_ups.get_by_status(self._db, "failed", domain="user_world")
        blocked += await follow_ups.get_by_status(self._db, "blocked", domain="user_world")
        if blocked:
            shown = (
                f" (showing 5 of {len(blocked)})" if len(blocked) > 5 else ""
            )
            lines.append(f"**Blocked/failed ({len(blocked)} total){shown}:**")
            for fu in blocked[:5]:
                reason = fu.get("blocked_reason", "") or "no reason recorded"
                lines.append(
                    f"- {fu['content'][:150]} "
                    f"({_relative_age(fu.get('created_at', ''))}) — {reason[:100]}"
                )

        # Recently completed (last 24h)
        completed = await follow_ups.get_recently_completed(
            self._db, hours=24, limit=5, domain="user_world",
        )
        if completed:
            lines.append("**Completed (24h):**")
            for row in completed:
                lines.append(f"- ✓ {row['content'][:200]}")

        return "\n".join(lines) if lines else None

    async def _get_inbox_resolved_digest(self) -> str | None:
        """Weekly digest of inbox follow-ups resolved by ego.

        Only included when there are ego-resolved inbox follow-ups in the
        last 7 days. Gives the user visibility into what the ego handled
        without requiring them to check the dashboard.
        """
        from genesis.db.crud import follow_ups

        resolved = await follow_ups.get_recently_resolved(
            self._db, source="inbox_evaluation", days=7,
        )
        if not resolved:
            return None

        header_shown = (
            f" (showing 10 of {len(resolved)})" if len(resolved) > 10 else ""
        )
        lines = [
            f"Ego resolved {len(resolved)} inbox follow-up(s) this week"
            f"{header_shown}:"
        ]
        for fu in resolved[:10]:
            content = (fu.get("content") or "?")[:150]
            notes = (fu.get("resolution_notes") or "no notes")[:100]
            lines.append(f"- {content} — {notes}")
        return "\n".join(lines)

    async def _get_critical_issues(self) -> str | None:
        """Return critical issues text ONLY if WARNING+ alerts are active.

        Returns None (not empty string) when all clear — caller skips the
        section entirely. This is not a standing checklist.
        """
        lines: list[str] = []

        # Health alerts (call sites down, queue depth, resilience warnings)
        try:
            from genesis.mcp.health_mcp import _impl_health_alerts

            alerts = await _impl_health_alerts(active_only=True)
            # Filter to genuinely urgent issues only:
            # - CRITICAL/ERROR always included (something is actually down)
            # - WARNING only if NOT a degraded call site (fallback routing
            #   is normal operation, not worth morning-report space)
            for a in alerts:
                severity = a.get("severity", "").upper()
                alert_id = a.get("id", "")
                if (
                    severity in ("ERROR", "CRITICAL")
                    or (severity == "WARNING" and not alert_id.startswith("call_site:"))
                ):
                    lines.append(
                        f"- **{severity}**: {a.get('message', 'Unknown')} "
                        f"(id: {alert_id})"
                    )
        except Exception:
            logger.warning("Failed to query health alerts for morning report", exc_info=True)

        # Subsystem heartbeat staleness (detects silent deaths)
        try:
            from genesis.mcp.health.manifest import _impl_subsystem_heartbeats

            heartbeats = await _impl_subsystem_heartbeats()
            for name, info in heartbeats.items():
                if info.get("status") == "overdue":
                    age = info.get("age_seconds", 0)
                    age_h = age / 3600 if age else 0
                    lines.append(
                        f"- **WARNING**: Subsystem '{name}' heartbeat overdue "
                        f"({age_h:.1f}h since last seen)"
                    )
        except Exception:
            logger.warning("Failed to query heartbeats for morning report", exc_info=True)

        return "\n".join(lines) if lines else None

    async def _get_engagement_summary(self) -> str:
        try:
            from genesis.db.crud.outreach import get_engagement_stats
            stats = await get_engagement_stats(self._db, days=7)
        except Exception:
            # Fallback to simple count
            from genesis.db.crud.outreach import count_recent
            total = await count_recent(self._db, days=7)
            return f"- {total} messages sent in last 7 days." if total else "- No outreach in last 7 days."

        total = stats["total"]
        if not total:
            return "- No outreach in last 7 days."

        pending = stats["pending"]

        # Just total count — no engagement self-analysis per guidelines
        lines = [f"- {total} messages sent in last 7 days."]
        if pending:
            lines.append(f"- {pending} awaiting engagement signal.")

        return "\n".join(lines)

    # Fact-name → note map for conditions that are PROTECTIVE, not risks.
    # Install-agnostic (matched against observation content, lowercase):
    # the drafter has repeatedly inverted these into alarms ("systemd-oomd
    # active" reported as an OOM risk).
    _PROTECTIVE_FACTS: dict[str, str] = {
        "systemd-oomd": "OOM-protection service — its presence is protective",
        "earlyoom": "OOM-protection service — its presence is protective",
        "zram": "compressed-swap protection — its presence is protective",
        "swap enabled": "swap protection — its presence is protective",
    }

    # A protection named alongside any of these cues is being reported as
    # ABSENT/broken — that IS a risk and must never be tagged protective
    # (the tag would instruct the drafter to soften or omit it).
    _NEGATION_CUES: tuple[str, ...] = (
        "not ", "n't", "no ", "never", "disabled", "absent", "missing",
        "unprotected", "inactive", "stopped", "failed", "broken", "off ",
        "lost", "without",
    )

    # Sources that ONLY ever report missing/broken protections — never tag.
    _NEVER_PROTECTIVE_SOURCES: frozenset[str] = frozenset({
        "infra_protection_posture_monitor",
    })

    def _protective_tag(self, content: str, *, source: str = "") -> str:
        """Return ' [protective: …]' when the observation cites a known
        protective mechanism AS PRESENT, else ''.

        Never tags posture-monitor alerts (that source exists to report
        protections that are MISSING) and never tags content carrying a
        negation cue — "systemd-oomd is not enforced" is a live risk, and
        tagging it protective would invert (and, per the prompt rule,
        suppress) exactly the alert class this map exists to protect.
        """
        if source in self._NEVER_PROTECTIVE_SOURCES:
            return ""
        lowered = content.lower()
        for fact, note in self._PROTECTIVE_FACTS.items():
            if fact in lowered:
                if any(cue in lowered for cue in self._NEGATION_CUES):
                    return ""
                return f" [protective: {note}]"
        return ""

    async def _get_observation_insights(self) -> str | None:
        """Surface unsurfaced observations that deserve user attention.

        Returns None if no unsurfaced observations exist. Observation IDs
        are collected in _pending_surface_ids and confirmed via
        confirm_delivery() after successful pipeline delivery.
        """
        from genesis.db.crud.observations import get_unsurfaced

        observations = await get_unsurfaced(
            self._db,
            priority_filter=("critical", "high", "medium"),
            exclude_types=_INTERNAL_OBS_TYPES,
            limit=10,
        )
        if not observations:
            return None

        lines = []
        for obs in observations:
            prio = obs["priority"]
            # Stale grounding: a >3d-old observation is a historical signal, not
            # a current condition — demote its displayed severity one level and
            # tag it so it doesn't surface as a fresh alarm (stored priority
            # unchanged).
            if _age_seconds(obs.get("created_at", "")) > _STALE_OBS_SECONDS:
                prio = _STALE_PRIORITY_DEMOTION.get(prio, prio)
                aged = " [aged]"
            else:
                aged = ""
            badge = {"critical": "🔴", "high": "🟠", "medium": "🟡"}.get(prio, "")
            content = obs["content"][:200].replace("\n", " ")
            age = _relative_age(obs.get("created_at", ""))
            protective = self._protective_tag(
                content, source=obs.get("source") or "",
            )
            lines.append(
                f"- {badge} **{prio}**{aged} ({age}): {content}{protective}"
            )

        # Defer surfacing until delivery is confirmed via confirm_delivery().
        # If delivery fails, these observations re-appear in the next report.
        self._pending_surface_ids = [obs["id"] for obs in observations]

        return "\n".join(lines)

    async def _get_standing_items(self) -> str | None:
        """Return observations surfaced 3+ times but still unresolved.

        These are known conditions — demoted to end of report.
        Includes last_surfaced_at so the LLM can judge staleness.
        """
        from genesis.db.crud.observations import get_standing

        items = await get_standing(
            self._db,
            priority_filter=("critical", "high", "medium"),
            exclude_types=_INTERNAL_OBS_TYPES,
            threshold=3,
            limit=5,
        )
        if not items:
            return None

        # Provide last report timestamp so LLM can compress unchanged items
        from genesis.db.crud.outreach import get_previous_report_time
        last_report_at = await get_previous_report_time(self._db)

        # Parse last_report_at to datetime for correct comparison
        # (outreach_history uses space-separated format, surfaced_at uses ISO T-format)
        last_report_dt = None
        if last_report_at:
            try:
                last_report_dt = datetime.fromisoformat(last_report_at)
                if last_report_dt.tzinfo is None:
                    last_report_dt = last_report_dt.replace(tzinfo=UTC)
            except (ValueError, TypeError):
                pass

        lines = []
        if last_report_at:
            lines.append(f"(Last report: {last_report_at[:16]})")
        for obs in items:
            content = obs["content"][:150].replace("\n", " ")
            count = obs.get("surfaced_count", 0)
            age = _relative_age(obs.get("created_at", ""))
            last_surfaced = obs.get("surfaced_at", "")
            changed_since_last = True
            if last_report_dt and last_surfaced:
                try:
                    surfaced_dt = datetime.fromisoformat(last_surfaced)
                    if surfaced_dt.tzinfo is None:
                        surfaced_dt = surfaced_dt.replace(tzinfo=UTC)
                    changed_since_last = surfaced_dt > last_report_dt
                except (ValueError, TypeError):
                    pass
            status = "" if changed_since_last else " [unchanged]"
            lines.append(f"- {content} (surfaced {count}x, {age}){status}")
        return "\n".join(lines)
