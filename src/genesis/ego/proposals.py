"""Ego proposal workflow — lifecycle from creation to Telegram approval.

Handles: proposal creation, batch digest formatting, Telegram delivery
via TopicManager, and status resolution.
"""

from __future__ import annotations

import html
import json
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from genesis.db.crud import ego as ego_crud
from genesis.ego.types import partition_informational
from genesis.util.approval_words import (
    APPROVE_PHRASES as _SHARED_BARE_APPROVE,
)
from genesis.util.approval_words import (
    APPROVE_TOKENS as _SHARED_APPROVE_TOKENS,
)
from genesis.util.approval_words import (
    REJECT_PHRASES as _SHARED_BARE_REJECT,
)
from genesis.util.approval_words import (
    REJECT_TOKENS as _SHARED_REJECT_TOKENS,
)

if TYPE_CHECKING:
    import aiosqlite

    from genesis.channels.telegram.topics import TopicManager
    from genesis.memory.store import MemoryStore

logger = logging.getLogger(__name__)

TOPIC_CATEGORY = "ego_proposals"


def _serialize_expected_outputs(raw: dict | str | None) -> str | None:
    """Serialize expected_outputs from ego LLM output for DB storage.

    Accepts a dict (from parsed LLM JSON) or a pre-serialized string.
    Returns a JSON string or None.
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        return json.dumps(raw)
    if isinstance(raw, str):
        return raw  # already serialized
    return None


# ---------------------------------------------------------------------------
# Digest formatting
# ---------------------------------------------------------------------------

_ESC = html.escape

_URGENCY_ORDER = {"critical": 0, "high": 1, "normal": 2, "low": 3}
_URGENCY_TAGS = {"critical": "[CRITICAL] ", "high": "[HIGH] "}

_EGO_LABELS = {
    "user_ego_cycle": "User Ego",
    "genesis_ego_cycle": "Genesis Ego",
    # Informational eval sources — named so they never render as a bare "Ego".
    "j9_eval": "Eval",
    "gauntlet": "Gauntlet",
}


def _sort_proposals(proposals: list[dict], *, enforce_calibration: bool = False) -> list[dict]:
    """Sort proposals by urgency (critical first) then confidence (desc).

    With ``enforce_calibration`` the arbitration discount's
    ``_calibrated_confidence`` (domain track record) outranks the stated
    confidence — WS-2 P4 ``arbitration: enforce`` only. In shadow the stated
    value always drives the sort (annotate-only).
    """

    def _conf(p: dict) -> float:
        if enforce_calibration and p.get("_calibrated_confidence") is not None:
            return float(p["_calibrated_confidence"])
        return float(p.get("confidence", 0))

    return sorted(
        proposals,
        key=lambda p: (
            _URGENCY_ORDER.get(p.get("urgency", "normal"), 2),
            -_conf(p),
        ),
    )


def _truncate(text: str, limit: int) -> str:
    """Truncate text to limit, adding ellipsis if needed."""
    return text[:limit] + "\u2026" if len(text) > limit else text


def _format_digest(
    proposals: list[dict],
    batch_id: str,
    ego_source: str | None = None,
) -> str:
    """Format proposals as a structured WHAT/WHY/HOW digest for Telegram.

    Proposals are sorted by urgency x confidence before numbering.
    Fields map to: content → WHAT, rationale → WHY, execution_plan → HOW.

    The calibrated track-record confidence drives the sort only under
    ``arbitration: enforce`` (see :func:`annotate_calibration`).
    """
    try:
        from genesis.ledger.ws2_ledger_config import arbitration_mode

        enforce = arbitration_mode() == "enforce"
    except Exception:  # noqa: BLE001 — formatting must never fail on config
        enforce = False
    sorted_proposals = _sort_proposals(proposals, enforce_calibration=enforce)
    label = _EGO_LABELS.get(ego_source or "", "Ego")
    lines = [f"<b>{_ESC(label)}</b> \u2014 {_ESC(batch_id[:8])}\n"]

    for i, p in enumerate(sorted_proposals, 1):
        urgency = p.get("urgency", "normal")
        urgency_tag = _URGENCY_TAGS.get(urgency, "")
        action_type = p.get("action_type", "?")

        # Header: number + urgency + action type
        lines.append(f"<b>{i}.</b> {_ESC(urgency_tag)}{_ESC(action_type)}")

        # WHAT (content)
        content = _truncate(p.get("content", ""), 400)
        lines.append(f"\n<b>WHAT:</b> {_ESC(content)}")

        # WHY (rationale)
        rationale = p.get("rationale", "")
        if rationale:
            rationale = _truncate(rationale, 300)
            lines.append(f"\n<b>WHY:</b> {_ESC(rationale)}")

        # HOW (execution_plan)
        plan = p.get("execution_plan", "")
        if plan:
            plan = _truncate(plan, 200)
            lines.append(f"\n<b>HOW:</b> {_ESC(plan)}")

        # Confidence badge
        confidence = p.get("confidence", 0.0)
        lines.append(f"\n[{confidence:.2f} confidence]")

        # WS-2 P4 arbitration annotations (annotate-only; never suppresses).
        badge = p.get("_calibration_badge")
        if badge:
            lines.append(_ESC(badge))
        note = p.get("_calibration_note")
        if note:
            lines.append(f"<i>{_ESC(note)}</i>")
        lines.append("")

    # Separator + compact reply instructions
    lines.append('<i>ok \u2022 no \u2022 "approve 1, reject 2" \u2022 or talk</i>')
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# WS-2 P4 arbitration discount (design \u00a75.1)
# ---------------------------------------------------------------------------

# Confidence-vs-track-record gap on ok stated cells that triggers the
# discount (same floor as the calibration_status MCP ranking).
_CALIBRATION_GAP_FLOOR = 0.15

# Process-global count of calibration-lookup failures \u2014 surfaced as the
# `ledger:arbitration_failed` WARNING by mcp/health/errors.py while nonzero
# (writers.py counter idiom). A lookup failure never blocks proposal creation.
_arbitration_failures: dict[str, int] = {}


def arbitration_failure_counts() -> dict[str, int]:
    """Snapshot of arbitration-lookup failure counts (read by health alerts)."""
    return dict(_arbitration_failures)


def _reset_arbitration_failures_for_tests() -> None:
    _arbitration_failures.clear()


async def annotate_calibration(db: aiosqlite.Connection, proposal: dict) -> None:
    """Annotate ONE proposal dict (in place) with its domain's calibration.

    Reads the stated-lane 90d ``calibration_cells`` row for
    ``(ego.<action_type>, ego_proposal, approved_and_executes)`` \u2014 the exact
    cell the ledger grades this proposal class into (writers.py stamps
    ``domain=f"ego.{action_type}"``):

    - no cell \u2192 nothing (no-data ego domains are the norm until rows grade);
    - ``thin``/``unknown`` \u2192 ``_calibration_note`` escalation phrasing ONLY \u2014
      never a discount on ignorance, never a bare percentage (design \u00a73.4);
    - ``ok`` with overconfidence gap > 0.15 (cell mean stated \u2212 track record)
      \u2192 ``_calibrated_confidence`` = shrunk track record + a digest badge
      showing THIS proposal's stated confidence vs the domain track record.

    Annotate-only: sort consumes ``_calibrated_confidence`` exclusively under
    ``arbitration: enforce`` (see ``_sort_proposals``); delivery is never
    gated. Raises never escape \u2014 failures count into
    ``_arbitration_failures`` and the proposal ships un-annotated.
    """
    action_type = proposal.get("action_type", "unknown")
    try:
        from genesis.db.crud import calibration_cells as cc_crud

        domain = f"ego.{action_type}"
        cells = await cc_crud.list_cells(db, domain=domain, provenance="stated", window_days=90)
        cell = next(
            (
                c
                for c in cells
                if c["domain"] == domain
                and c["action_class"] == "ego_proposal"
                and c["metric"] == "approved_and_executes"
            ),
            None,
        )
        if cell is None:
            return
        if cell["status"] in ("thin", "unknown"):
            proposal["_calibration_note"] = (
                f"calibration: {cell['status']} (n={cell['n']}) \u2014 escalate; "
                "track record not yet trustworthy"
            )
            return
        mean_conf = cell["mean_confidence"]
        shrunk = cell["shrunk_estimate"]
        if mean_conf is None or shrunk is None:
            return
        if (mean_conf - shrunk) > _CALIBRATION_GAP_FLOOR:
            proposal["_calibrated_confidence"] = float(shrunk)
            stated = float(proposal.get("confidence", 0.0))
            proposal["_calibration_badge"] = (
                f"\u2696 stated {stated:.2f} \u2192 track record {float(shrunk):.2f} (n={cell['n']})"
            )
    except Exception:  # noqa: BLE001 \u2014 annotation is best-effort, never blocking
        # Sanitize before keying: action_type is free-form ego-LLM text and
        # this key flows into the durable alert-id namespace
        # (ledger:arbitration_failed:<key>) \u2014 unlike the schema-CHECKed
        # action_class of the sibling alarms.
        key = re.sub(r"[^a-z0-9_.-]", "_", str(action_type).lower())[:48] or "unknown"
        _arbitration_failures[key] = _arbitration_failures.get(key, 0) + 1
        logger.debug("arbitration calibration lookup failed", exc_info=True)


# ---------------------------------------------------------------------------
# ProposalWorkflow
# ---------------------------------------------------------------------------


class ProposalWorkflow:
    """Manages the ego proposal lifecycle.

    Parameters
    ----------
    db:
        Open aiosqlite connection.
    topic_manager:
        TopicManager for sending to Telegram supergroup topics.
        None if Telegram is not available (proposals created but not sent).
    memory_store:
        MemoryStore for storing corrections on proposal rejection.
        None if memory is not available (corrections silently skipped).
    """

    def __init__(
        self,
        *,
        db: aiosqlite.Connection,
        topic_manager: TopicManager | None = None,
        memory_store: MemoryStore | None = None,
        autonomy_manager: object | None = None,
    ) -> None:
        self._db = db
        self._topic_manager = topic_manager
        self._memory_store = memory_store
        # AutonomyManager (duck-typed) for the earn-back promote hook in
        # resolve_proposals. None disables earn-back on the Telegram path.
        self._autonomy_manager = autonomy_manager

    # -- Late-binding setters (wired in standalone.py after Telegram init) --

    def set_topic_manager(self, topic_manager: TopicManager) -> None:
        """Attach a TopicManager for Telegram proposal delivery."""
        self._topic_manager = topic_manager

    # -- Creation ----------------------------------------------------------

    async def create_batch(
        self,
        proposals: list[dict],
        *,
        cycle_id: str | None = None,
        ego_source: str | None = None,
    ) -> tuple[str, list[str], list[dict]]:
        """Create a batch of proposals from ego output dicts.

        Returns ``(batch_id, [proposal_ids], [created_proposals])``.
        The third element mirrors the created IDs so callers can pair
        them correctly even when dedup skips mid-batch proposals.
        """
        batch_id = uuid.uuid4().hex[:16]
        created_at = datetime.now(UTC).isoformat()

        # Pre-fetch valid goal IDs for validation (cheap SELECT, prevents
        # hallucinated goal_ids from persisting).
        valid_goal_ids: set[str] | None = None
        if any(p.get("goal_id") for p in proposals):
            try:
                from genesis.db.crud import user_goals

                active_goals = await user_goals.list_active(self._db, limit=200)
                valid_goal_ids = {g["id"] for g in active_goals}
            except Exception:
                logger.warning(
                    "Goal ID validation degraded — cannot verify goal_ids",
                    exc_info=True,
                )

        from genesis.ego.integrity import content_hash as _content_hash
        from genesis.ego.integrity import content_size as _content_size

        ids: list[str] = []
        created_proposals: list[dict] = []
        for p in proposals:
            pid = uuid.uuid4().hex[:16]
            rank_val = p.get("rank")
            if rank_val is not None:
                try:
                    rank_val = int(rank_val)
                except (ValueError, TypeError):
                    rank_val = None

            # Validate goal_id — drop if not a real active goal (guards against
            # hallucinated LLM goal_ids). If validation is degraded
            # (valid_goal_ids is None), pass through.
            # EXCEPT goal_status_change: its goal_id is code-generated for a
            # specific (possibly just-paused, hence non-active) goal, never
            # hallucinated — dropping it would silently no-op the approval.
            raw_goal_id = p.get("goal_id") or None
            if (
                raw_goal_id
                and valid_goal_ids is not None
                and p.get("action_type") != "goal_status_change"
            ):
                goal_id = raw_goal_id if raw_goal_id in valid_goal_ids else None
                if not goal_id:
                    logger.warning(
                        "Proposal %s: dropping invalid goal_id %r",
                        pid,
                        raw_goal_id,
                    )
            else:
                goal_id = raw_goal_id

            proposal_content = p.get("content", "")
            hash_val = _content_hash(proposal_content) if proposal_content else None

            # Dedup: skip if identical content already pending/approved.
            # Skip check for empty content to avoid false collisions on
            # the fixed SHA-256 of the empty string.
            if hash_val:
                try:
                    if await ego_crud.has_pending_proposal_with_hash(
                        self._db,
                        hash_val,
                    ):
                        logger.info(
                            "Proposal dedup: skipping exact duplicate (hash=%s)",
                            hash_val[:12],
                        )
                        continue
                except Exception:
                    logger.warning("Proposal dedup check failed, proceeding", exc_info=True)

            await ego_crud.create_proposal(
                self._db,
                id=pid,
                action_type=p.get("action_type", "unknown"),
                action_category=p.get("action_category", ""),
                content=proposal_content,
                rationale=p.get("rationale", ""),
                confidence=float(p.get("confidence", 0.0)),
                urgency=p.get("urgency", "normal"),
                alternatives=p.get("alternatives", ""),
                cycle_id=cycle_id,
                batch_id=batch_id,
                created_at=created_at,
                rank=rank_val,
                execution_plan=p.get("execution_plan"),
                recurring=bool(p.get("recurring", False)),
                memory_basis=p.get("memory_basis", ""),
                realist_verdict=p.get("_realist_verdict"),
                realist_reasoning=p.get("_realist_reasoning"),
                ego_source=ego_source,
                goal_id=goal_id,
                content_hash=hash_val,
                content_size=_content_size(proposal_content),
                original_content=p.get("_original_content"),
                expected_outputs=_serialize_expected_outputs(
                    p.get("expected_outputs"),
                ),
            )
            ids.append(pid)
            created_proposals.append(p)

            # WS-2 P1b: approved_and_executes prediction per created proposal
            # (dedup-skipped proposals above never reach here). Wrapped so
            # even an import failure can never break the batch.
            try:
                from genesis.ledger.writers import on_ego_proposal

                await on_ego_proposal(
                    self._db,
                    proposal_id=pid,
                    action_type=p.get("action_type", "unknown"),
                    confidence=float(p.get("confidence", 0.0)),
                )
            except Exception:  # noqa: BLE001 — ledger is best-effort
                logger.debug("ledger prediction hook failed", exc_info=True)

        logger.info(
            "Created ego proposal batch %s with %d proposals",
            batch_id,
            len(ids),
        )
        return batch_id, ids, created_proposals

    # -- Formatting --------------------------------------------------------

    async def validate_batch(self, proposals: list[dict]) -> list[str]:
        """Structural sanity checks on proposals before Telegram delivery."""
        issues: list[str] = []
        contents: list[str] = [p.get("content", "") for p in proposals]
        for p in proposals:
            title = (p.get("content") or "?")[:40]

            # 1. Low confidence + high-impact action
            conf = float(p.get("confidence", 0))
            action = p.get("action_type", "")
            if action in ("execute", "deploy", "modify", "dispatch") and conf < 0.6:
                issues.append(f"'{title}': low confidence ({conf:.0%}) for {action}")

            # 2. Empty rationale (why does this proposal exist?)
            rationale = (p.get("rationale") or "").strip()
            if len(rationale) < 20:
                issues.append(f"'{title}': missing or thin rationale")

            # 3. Duplicate content within the same batch
            if contents.count(p.get("content", "")) > 1:
                issues.append(f"'{title}': duplicate within batch")

        return issues

    def format_digest(
        self,
        proposals: list[dict],
        batch_id: str,
        ego_source: str | None = None,
    ) -> str:
        """Format proposals as an HTML numbered digest for Telegram."""
        return _format_digest(proposals, batch_id, ego_source=ego_source)

    # -- Delivery ----------------------------------------------------------

    # GROUNDWORK(digest-rate-limit): 6h minimum between Telegram deliveries
    # per ego source. Proposals are stored regardless; only delivery is
    # gated. Set to True when ready to enforce.
    _DIGEST_RATE_LIMIT_ENABLED = False
    _DIGEST_RATE_LIMIT_HOURS = 6

    async def send_digest(
        self,
        batch_id: str,
        *,
        validation_warnings: list[str] | None = None,
        ego_source: str | None = None,
    ) -> str | None:
        """Send the batch digest to Telegram. Returns delivery_id or None.

        Rate-limited: at most one delivery per ego source every 6 hours.
        Proposals are still stored; only Telegram delivery is gated.

        TODO(batch-4): Register ReplyWaiter BEFORE sending to close the
        race window where a fast reply arrives before wait_for_reply is
        called.  Currently the window is milliseconds (negligible for a
        human replying), but architecturally it should be pre-registered.
        """
        if self._topic_manager is None:
            logger.warning("No topic_manager — cannot send ego digest")
            return None

        # GROUNDWORK(digest-rate-limit): enforce minimum interval between
        # Telegram deliveries per ego source. Proposals are already stored
        # in the DB regardless; this only gates the notification.
        if self._DIGEST_RATE_LIMIT_ENABLED and ego_source:
            from datetime import UTC, datetime, timedelta

            state_key = f"last_digest_delivery:{ego_source}"
            last_ts = await ego_crud.get_state(self._db, state_key)
            if last_ts:
                try:
                    last_dt = datetime.fromisoformat(last_ts)
                    cutoff = datetime.now(UTC) - timedelta(
                        hours=self._DIGEST_RATE_LIMIT_HOURS,
                    )
                    if last_dt > cutoff:
                        logger.info(
                            "Digest rate-limited for %s (last: %s, next eligible: %s)",
                            ego_source,
                            last_ts,
                            (last_dt + timedelta(hours=self._DIGEST_RATE_LIMIT_HOURS)).isoformat(),
                        )
                        return None
                except (ValueError, TypeError):
                    pass  # Malformed timestamp — proceed with delivery

        proposals = await ego_crud.list_proposals_by_batch(self._db, batch_id)
        if not proposals:
            logger.warning("No proposals found for batch %s", batch_id)
            return None

        # WS-2 P4: annotate at the RENDER boundary — the reload above returns
        # bare DB rows (the transient _calibration_* keys are never persisted),
        # so annotating any earlier could not reach the delivered digest.
        # Delivery-time annotation also renders the freshest twice-daily cells.
        try:
            from genesis.ledger.ws2_ledger_config import arbitration_mode

            annotate = arbitration_mode() != "off"
        except Exception:  # noqa: BLE001 — config must never block delivery
            annotate = True
        if annotate:
            for p in proposals:
                await annotate_calibration(self._db, p)

        digest_html = self.format_digest(proposals, batch_id, ego_source=ego_source)

        # Prepend validation warnings if any
        if validation_warnings:
            warn_lines = "\n".join(f"  - {w}" for w in validation_warnings)
            digest_html = f"\u26a0\ufe0f <b>Validation:</b>\n{warn_lines}\n\n{digest_html}"

        # Show pending backlog from other batches (same ego only). Exclude
        # acknowledge-only eval rows (j9/gauntlet) — they are notifications, not
        # approval work, and "approve all pending" never touches them.
        try:
            all_pending = await ego_crud.list_pending_proposals(
                self._db,
                ego_source=ego_source,
            )
            approval_pending, _informational = partition_informational(all_pending)
            other_pending = [p for p in approval_pending if p.get("batch_id") != batch_id]
            if other_pending:
                summary_lines = []
                for op in other_pending[:5]:
                    preview = op.get("content", "")[:60]
                    if len(op.get("content", "")) > 60:
                        preview += "\u2026"
                    action = op.get("action_type", "?")
                    summary_lines.append(f"  \u2022 [{_ESC(action)}] {_ESC(preview)}")
                backlog_text = "\n".join(summary_lines)
                digest_html = (
                    f"<i>\U0001f4cb {len(other_pending)} older proposal(s) still "
                    f"awaiting response:</i>\n{backlog_text}\n"
                    f"<i>Reply 'approve all pending' to resolve all.</i>\n\n" + digest_html
                )
        except Exception:
            pass  # Non-critical; skip header on error

        delivery_id = await self._topic_manager.send_to_category(
            TOPIC_CATEGORY,
            digest_html,
        )

        if delivery_id is None:
            logger.error("Failed to send ego digest for batch %s", batch_id)
            return None

        # Store bidirectional mapping for reply resolution.
        await ego_crud.set_state(
            self._db,
            key=f"delivery_batch:{delivery_id}",
            value=batch_id,
        )
        await ego_crud.set_state(
            self._db,
            key=f"batch_delivery:{batch_id}",
            value=delivery_id,
        )

        # GROUNDWORK(digest-rate-limit): record delivery timestamp for
        # rate limiting. Written even when gate is disabled so the
        # timestamp is ready when the gate is flipped on.
        try:
            if ego_source:
                from datetime import UTC, datetime

                await ego_crud.set_state(
                    self._db,
                    key=f"last_digest_delivery:{ego_source}",
                    value=datetime.now(UTC).isoformat(),
                )
        except Exception:
            pass  # Non-critical — don't let timestamp failure block delivery

        logger.info(
            "Sent ego digest for batch %s (delivery_id=%s, %d proposals)",
            batch_id,
            delivery_id,
            len(proposals),
        )
        return str(delivery_id)

    # -- Approval processing -----------------------------------------------

    async def resolve_proposals(
        self,
        batch_id: str,
        decisions: dict[int, tuple[str, str | None]],
    ) -> dict[str, str]:
        """Apply parsed decisions to proposals in the batch.

        ``decisions`` maps 1-based index → (status, optional reason).
        Returns ``{proposal_id: final_status}``.

        On rejection with a reason, automatically stores a correction
        memory so the ego learns to avoid similar proposals.
        """
        proposals = await ego_crud.list_proposals_by_batch(self._db, batch_id)
        results: dict[str, str] = {}

        for idx, (status, reason) in decisions.items():
            if idx < 1 or idx > len(proposals):
                continue
            prop = proposals[idx - 1]
            updated = await ego_crud.resolve_proposal(
                self._db,
                prop["id"],
                status=status,
                user_response=reason,
            )
            if updated:
                results[prop["id"]] = status
                logger.info(
                    "Proposal %s → %s%s",
                    prop["id"],
                    status,
                    f" ({reason})" if reason else "",
                )
                # Shared post-resolution hook: J-9, journal, decision
                # capture, correction memory, and all action hooks — the
                # SAME artifact set as the dashboard and MCP paths.
                from genesis.ego.resolution import handle_proposal_resolution

                await handle_proposal_resolution(
                    self._db,
                    prop,
                    status,
                    reason=reason,
                    source="telegram",
                    memory_store=self._memory_store,
                    autonomy_manager=self._autonomy_manager,
                )
            else:
                logger.warning(
                    "Proposal %s not updated (already resolved?)",
                    prop["id"],
                )

        return results

    # -- Cross-batch approval ------------------------------------------------

    async def resolve_all_pending_proposals(
        self,
        status: str,
        reason: str | None = None,
    ) -> dict[str, str]:
        """Resolve ALL pending proposals across all batches.

        Loops per-batch to preserve intervention_journal, J-9 eval,
        and correction memory lifecycle.  Returns {proposal_id: status}.

        Uses full batch lists for correct 1-based indexing — resolve_proposals
        indexes into list_proposals_by_batch (ALL proposals, not just pending).
        """
        pending = await ego_crud.list_pending_proposals(self._db)
        # Informational eval rows (j9/gauntlet) are acknowledge-only — a bulk
        # "approve all pending" must never sweep them into a resolution. (Today
        # they also carry a NULL batch_id and would fall out of the per-batch
        # grouping below, but that is incidental; this makes the exclusion
        # explicit so the no-approval invariant can't regress.)
        pending, _informational = partition_informational(pending)
        if not pending:
            return {}

        # Group by batch_id
        batch_ids: set[str] = set()
        for p in pending:
            bid = p.get("batch_id") or "unknown"
            batch_ids.add(bid)

        all_results: dict[str, str] = {}
        for batch_id in batch_ids:
            # Get full batch to find correct 1-based positions
            full_batch = await ego_crud.list_proposals_by_batch(
                self._db,
                batch_id,
            )
            # Create decisions for ALL positions — resolve_proposal's
            # WHERE status='pending' clause skips already-resolved ones.
            decisions = {i + 1: (status, reason) for i in range(len(full_batch))}
            results = await self.resolve_proposals(batch_id, decisions)
            all_results.update(results)

        logger.info(
            "Cross-batch resolve: %d proposals → %s across %d batch(es)",
            len(all_results),
            status,
            len(batch_ids),
        )
        return all_results

    # -- Revoke (cancel approved proposals) -----------------------------------

    async def revoke_approved_proposals(
        self,
        batch_id: str,
        proposal_indices: list[int] | None = None,
        reason: str | None = None,
    ) -> int:
        """Revoke approved proposals in a batch (approved → rejected).

        If proposal_indices is None, revokes all approved in the batch.
        Returns count of revoked proposals.
        """
        proposals = await ego_crud.list_proposals_by_batch(self._db, batch_id)
        revoked = 0
        for i, prop in enumerate(proposals, 1):
            if proposal_indices is not None and i not in proposal_indices:
                continue
            if prop["status"] != "approved":
                continue
            ok = await ego_crud.revoke_proposal(
                self._db,
                prop["id"],
                user_response=reason or "revoked by user",
            )
            if ok:
                revoked += 1
                # Update intervention journal
                try:
                    from genesis.db.crud import intervention_journal as journal_crud

                    await journal_crud.resolve(
                        self._db,
                        prop["id"],
                        outcome_status="rejected",
                        actual_outcome="User revoked approval" + (f": {reason}" if reason else ""),
                        user_response=reason,
                    )
                except Exception:
                    logger.warning("Failed to update journal for revoked proposal %s", prop["id"])
        if revoked:
            logger.info("Revoked %d approved proposal(s) in batch %s", revoked, batch_id)
        return revoked

    # -- Late-binding for memory store (wired in init/ego.py) ----------------

    def set_memory_store(self, store: MemoryStore) -> None:
        """Attach a MemoryStore for storing correction memories on rejection."""
        self._memory_store = store


# ---------------------------------------------------------------------------
# Reply parser — converts user Telegram text into proposal decisions
# ---------------------------------------------------------------------------

# Token + phrase vocabulary is CANONICAL in genesis.util.approval_words
# (imported at the top of this module) — shared with the CLI approval gate
# and the Telegram bare-text path so the three decision surfaces can never
# drift again. Only the cancel verbs are proposal-specific (grace-period
# revoke has no equivalent elsewhere).
_APPROVE_WORDS = _SHARED_APPROVE_TOKENS
_REJECT_WORDS = _SHARED_REJECT_TOKENS
_CANCEL_WORDS = {"cancel", "cancelled", "revoke", "revoked", "stop", "undo"}

# Bare affirmative / negative — standalone short replies in the
# ego_proposals topic. The shared phrase sets are punctuation-free (the
# shared normalize() strips trailing punctuation before matching, but this
# parser's caller only lowercases/strips), so the historical dotted
# variants are added back locally for byte-compatible matching.
_BARE_APPROVE = _SHARED_BARE_APPROVE | frozenset(
    {
        "ok.",
        "okay.",
        "yes.",
        "yep.",
        "sure.",
    }
)
_BARE_REJECT = _SHARED_BARE_REJECT | frozenset(
    {
        "no.",
    }
)

# Pattern: "1 approve" or "approve 1" or "1 yes" or "reject 2: reason"
_NUMBERED_PATTERN = re.compile(
    r"(?:(\d+)\s*[.:)?\-]?\s*(\w+)|(\w+)\s+(\d+))"
    r"(?:\s*[:\-]\s*(.+))?",
    re.IGNORECASE,
)


def parse_proposal_decisions(text: str) -> dict[int, tuple[str, str | None]]:
    """Parse user reply into proposal decisions.

    Supported formats (case-insensitive, comma/newline separated):
      "1 approve, 2 reject: reason"
      "approve 1, reject 2: reason"
      "approve all" / "reject all"

    Returns {1-based-index: (status, optional_reason)} or empty dict
    if unparseable.  Empty dict means fall through to correction store.

    Bare short replies ("ok", "yes", "sure", "no", "nope") are recognized
    as approve-all / reject-all for the most recent batch.  This is safe
    because the parser only fires in the ego_proposals topic.
    """
    stripped = text.strip().lower()

    # Cross-batch bulk operations (all pending across batches)
    if stripped in (
        "approve all pending",
        "approved all pending",
        "accept all pending",
        "yes all pending",
    ):
        return {-1: ("approved", None)}  # -1 = sentinel for cross-batch
    if stripped in (
        "reject all pending",
        "rejected all pending",
        "deny all pending",
        "no all pending",
    ):
        return {-1: ("rejected", None)}

    # Cancel/revoke (works on approved proposals during grace period)
    if stripped in ("cancel all", "revoke all", "stop all", "undo all"):
        return {0: ("cancelled", None)}  # 0 = sentinel, "cancelled" triggers revoke path

    # Batch-scoped bulk operations (explicit "all" qualifier)
    if stripped in ("approve all", "approved all", "accept all", "yes all", "go ahead"):
        return {0: ("approved", None)}  # 0 = sentinel for "all in batch"
    if stripped in ("reject all", "rejected all", "deny all", "no all"):
        return {0: ("rejected", None)}

    # Bare short replies — natural approval/rejection without numbers
    if stripped in _BARE_APPROVE:
        return {0: ("approved", None)}
    if stripped in _BARE_REJECT:
        return {0: ("rejected", None)}

    # Try numbered decisions (comma or newline separated)
    decisions: dict[int, tuple[str, str | None]] = {}
    parts = re.split(r"[,\n]+", text.strip())

    for part in parts:
        part = part.strip()
        if not part:
            continue
        m = _NUMBERED_PATTERN.match(part)
        if not m:
            continue

        # Two capture groups: "1 approve" or "approve 1"
        if m.group(1) and m.group(2):
            num_str, word = m.group(1), m.group(2).lower()
            reason = m.group(5)
        elif m.group(3) and m.group(4):
            word, num_str = m.group(3).lower(), m.group(4)
            reason = m.group(5)
        else:
            continue

        try:
            idx = int(num_str)
        except ValueError:
            continue

        if idx < 1:
            continue

        if word in _APPROVE_WORDS:
            decisions[idx] = ("approved", reason.strip() if reason else None)
        elif word in _REJECT_WORDS:
            decisions[idx] = ("rejected", reason.strip() if reason else None)
        elif word in _CANCEL_WORDS:
            decisions[idx] = ("cancelled", reason.strip() if reason else None)
        # Unknown word → skip this part (don't fail the whole parse)

    return decisions
