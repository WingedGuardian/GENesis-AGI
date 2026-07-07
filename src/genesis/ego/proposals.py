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
from genesis.ego.types import ProposalStatus
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
}


def _sort_proposals(proposals: list[dict]) -> list[dict]:
    """Sort proposals by urgency (critical first) then confidence (desc)."""
    return sorted(
        proposals,
        key=lambda p: (
            _URGENCY_ORDER.get(p.get("urgency", "normal"), 2),
            -float(p.get("confidence", 0)),
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
    """
    sorted_proposals = _sort_proposals(proposals)
    label = _EGO_LABELS.get(ego_source or "", "Ego")
    lines = [f"<b>{_ESC(label)}</b> \u2014 {_ESC(batch_id[:8])}\n"]

    for i, p in enumerate(sorted_proposals, 1):
        urgency = p.get("urgency", "normal")
        urgency_tag = _URGENCY_TAGS.get(urgency, "")
        action_type = p.get("action_type", "?")

        # Header: number + urgency + action type
        lines.append(
            f"<b>{i}.</b> {_ESC(urgency_tag)}{_ESC(action_type)}"
        )

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
        lines.append("")

    # Separator + compact reply instructions
    lines.append(
        "<i>ok \u2022 no \u2022 \"approve 1, reject 2\" \u2022 or talk</i>"
    )
    return "\n".join(lines)


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
                        pid, raw_goal_id,
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
                        self._db, hash_val,
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
                            ego_source, last_ts,
                            (last_dt + timedelta(hours=self._DIGEST_RATE_LIMIT_HOURS)).isoformat(),
                        )
                        return None
                except (ValueError, TypeError):
                    pass  # Malformed timestamp — proceed with delivery

        proposals = await ego_crud.list_proposals_by_batch(self._db, batch_id)
        if not proposals:
            logger.warning("No proposals found for batch %s", batch_id)
            return None

        digest_html = self.format_digest(proposals, batch_id, ego_source=ego_source)

        # Prepend validation warnings if any
        if validation_warnings:
            warn_lines = "\n".join(f"  - {w}" for w in validation_warnings)
            digest_html = f"\u26a0\ufe0f <b>Validation:</b>\n{warn_lines}\n\n{digest_html}"

        # Show pending backlog from other batches (same ego only)
        try:
            all_pending = await ego_crud.list_pending_proposals(
                self._db, ego_source=ego_source,
            )
            other_pending = [
                p for p in all_pending
                if p.get("batch_id") != batch_id
            ]
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
                    f"<i>Reply 'approve all pending' to resolve all.</i>\n\n"
                    + digest_html
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
                # J-9 eval: log proposal resolution for ego quality tracking
                from genesis.eval.j9_hooks import emit_proposal_resolved

                await emit_proposal_resolved(
                    self._db,
                    proposal_id=prop["id"],
                    status=status,
                    confidence=prop.get("confidence"),
                    action_type=prop.get("action_type"),
                )
                # Intervention journal: record resolution
                try:
                    from genesis.db.crud import intervention_journal as journal_crud

                    await journal_crud.resolve(
                        self._db,
                        prop["id"],
                        outcome_status=status,
                        actual_outcome=f"User {status}" + (f": {reason}" if reason else ""),
                        user_response=reason,
                    )
                except Exception:
                    logger.warning("Failed to update intervention journal for %s", prop["id"])
                # Auto-store correction memory on rejection with reason
                if status == ProposalStatus.REJECTED and reason and self._memory_store:
                    await self._store_correction(prop, reason)
                # Autonomy earn-back: promote on approval / cooldown on reject.
                # No-op unless this is an autonomy_earnback proposal. Wrapped so a
                # failure here never aborts resolution of sibling proposals.
                try:
                    from genesis.ego.earnback import handle_earnback_resolution

                    await handle_earnback_resolution(
                        self._db, prop, status, self._autonomy_manager,
                    )
                except Exception:
                    logger.warning(
                        "earnback resolution hook failed for %s",
                        prop.get("id"), exc_info=True,
                    )
                # Goal status change: apply pause/deprioritize on approval.
                try:
                    from genesis.ego.goal_actions import (
                        handle_goal_status_change_resolution,
                    )

                    await handle_goal_status_change_resolution(
                        self._db, prop, status,
                    )
                except Exception:
                    logger.warning(
                        "goal status-change hook failed for %s",
                        prop.get("id"), exc_info=True,
                    )
                # Cell promotion (WS-8 PR-D): promote on approval / cooldown on reject.
                try:
                    from genesis.ego.cell_promotion import (
                        handle_cell_promotion_resolution,
                    )

                    await handle_cell_promotion_resolution(self._db, prop, status)
                except Exception:
                    logger.warning(
                        "cell promotion hook failed for %s",
                        prop.get("id"), exc_info=True,
                    )
                # Cognitive variant promotion (Evo PR-B): apply the reflection
                # prompt winner to the overlay on approval.
                try:
                    from genesis.ego.cognitive_variant import (
                        handle_cognitive_variant_resolution,
                    )

                    await handle_cognitive_variant_resolution(self._db, prop, status)
                except Exception:
                    logger.warning(
                        "cognitive-variant hook failed for %s",
                        prop.get("id"), exc_info=True,
                    )
                # J-9 regression (informational): mark executed on approval, no
                # side-effect. Never dispatched (blocklist + NOTIFY_USER gate).
                try:
                    from genesis.ego.j9_regression_actions import (
                        handle_j9_regression_resolution,
                    )

                    await handle_j9_regression_resolution(self._db, prop, status)
                except Exception:
                    logger.warning(
                        "j9 regression hook failed for %s",
                        prop.get("id"), exc_info=True,
                    )
                # Gauntlet regression (informational): mark executed on approval,
                # no side-effect. Never dispatched (blocklist + NOTIFY_USER gate).
                try:
                    from genesis.ego.gauntlet_regression_actions import (
                        handle_gauntlet_regression_resolution,
                    )

                    await handle_gauntlet_regression_resolution(self._db, prop, status)
                except Exception:
                    logger.warning(
                        "gauntlet regression hook failed for %s",
                        prop.get("id"), exc_info=True,
                    )
            else:
                logger.warning(
                    "Proposal %s not updated (already resolved?)",
                    prop["id"],
                )

        return results

    async def _store_correction(
        self,
        proposal: dict,
        reason: str,
    ) -> None:
        """Store a correction memory when a proposal is rejected with a reason."""
        action_type = proposal.get("action_type", "unknown")
        action_category = proposal.get("action_category", "")
        content_snippet = proposal.get("content", "")[:200]
        correction_text = (
            f"User rejected [{action_type}]: {content_snippet}. Reason: {reason}. Do not repeat."
        )
        tags = ["ego_correction"]
        if action_category:
            tags.append(action_category)
        try:
            await self._memory_store.store(
                content=correction_text,
                source="ego_correction",
                tags=tags,
                wing="autonomy",
                room="ego_corrections",
                source_subsystem="ego",
            )
            logger.info(
                "Stored ego correction for rejected proposal %s",
                proposal.get("id", "?"),
            )
        except Exception:
            logger.warning(
                "Failed to store ego correction — continuing",
                exc_info=True,
            )

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
_BARE_APPROVE = _SHARED_BARE_APPROVE | frozenset({
    "ok.", "okay.", "yes.", "yep.", "sure.",
})
_BARE_REJECT = _SHARED_BARE_REJECT | frozenset({
    "no.",
})

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
