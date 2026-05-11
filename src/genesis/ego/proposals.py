"""Ego proposal workflow — lifecycle from creation to Telegram approval.

Handles: proposal creation, batch digest formatting, Telegram delivery
via TopicManager, and status resolution.
"""

from __future__ import annotations

import html
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from genesis.db.crud import ego as ego_crud
from genesis.ego.types import ProposalStatus

if TYPE_CHECKING:
    import aiosqlite

    from genesis.channels.telegram.topics import TopicManager
    from genesis.memory.store import MemoryStore

logger = logging.getLogger(__name__)

TOPIC_CATEGORY = "ego_proposals"

# ---------------------------------------------------------------------------
# Digest formatting
# ---------------------------------------------------------------------------

_ESC = html.escape


def _format_digest(
    proposals: list[dict],
    batch_id: str,
) -> str:
    """Format proposals as an HTML numbered digest for Telegram."""
    lines = [f"<b>Ego Proposals</b> \u2014 Batch {_ESC(batch_id[:8])}\n"]

    for i, p in enumerate(proposals, 1):
        content = p.get("content", "")
        if len(content) > 800:
            content = content[:800] + "\u2026"
        rationale = p.get("rationale", "")
        if len(rationale) > 500:
            rationale = rationale[:500] + "\u2026"

        lines.append(
            f"<b>{i}.</b> <b>[{_ESC(p.get('action_type', '?'))}]</b> "
            f"{_ESC(content)}"
        )
        if rationale:
            lines.append(f"<i>Rationale:</i> {_ESC(rationale)}")
        memory_basis = p.get("memory_basis", "")
        if memory_basis:
            if len(memory_basis) > 500:
                memory_basis = memory_basis[:500] + "\u2026"
            lines.append(f"<i>{_ESC(memory_basis)}</i>")
        confidence = p.get("confidence", 0.0)
        urgency = p.get("urgency", "normal")
        lines.append(
            f"<i>Confidence:</i> {confidence:.2f} | "
            f"<i>Urgency:</i> {urgency}"
        )
        alts = p.get("alternatives", "")
        if alts:
            if len(alts) > 300:
                alts = alts[:300] + "\u2026"
            lines.append(f"<i>Alternatives:</i> {_ESC(alts)}")
        lines.append("")


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
    ) -> None:
        self._db = db
        self._topic_manager = topic_manager
        self._memory_store = memory_store

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
    ) -> tuple[str, list[str]]:
        """Create a batch of proposals from ego output dicts.

        Returns ``(batch_id, [proposal_ids])``.
        """
        batch_id = uuid.uuid4().hex[:16]
        created_at = datetime.now(UTC).isoformat()

        ids: list[str] = []
        for p in proposals:
            pid = uuid.uuid4().hex[:16]
            rank_val = p.get("rank")
            if rank_val is not None:
                try:
                    rank_val = int(rank_val)
                except (ValueError, TypeError):
                    rank_val = None
            await ego_crud.create_proposal(
                self._db,
                id=pid,
                action_type=p.get("action_type", "unknown"),
                action_category=p.get("action_category", ""),
                content=p.get("content", ""),
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
            )
            ids.append(pid)

        logger.info(
            "Created ego proposal batch %s with %d proposals",
            batch_id, len(ids),
        )
        return batch_id, ids

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
    ) -> str:
        """Format proposals as an HTML numbered digest for Telegram."""
        return _format_digest(proposals, batch_id)

    # -- Delivery ----------------------------------------------------------

    async def send_digest(
        self,
        batch_id: str,
        *,
        validation_warnings: list[str] | None = None,
    ) -> str | None:
        """Send the batch digest to Telegram. Returns delivery_id or None.

        TODO(batch-4): Register ReplyWaiter BEFORE sending to close the
        race window where a fast reply arrives before wait_for_reply is
        called.  Currently the window is milliseconds (negligible for a
        human replying), but architecturally it should be pre-registered.
        """
        if self._topic_manager is None:
            logger.warning("No topic_manager — cannot send ego digest")
            return None

        proposals = await ego_crud.list_proposals_by_batch(self._db, batch_id)
        if not proposals:
            logger.warning("No proposals found for batch %s", batch_id)
            return None

        digest_html = self.format_digest(proposals, batch_id)

        # Prepend validation warnings if any
        if validation_warnings:
            warn_lines = "\n".join(f"  - {w}" for w in validation_warnings)
            digest_html = f"\u26a0\ufe0f <b>Validation:</b>\n{warn_lines}\n\n{digest_html}"

        delivery_id = await self._topic_manager.send_to_category(
            TOPIC_CATEGORY, digest_html,
        )

        if delivery_id is None:
            logger.error("Failed to send ego digest for batch %s", batch_id)
            return None

        # Store bidirectional mapping for reply resolution.
        await ego_crud.set_state(
            self._db, key=f"delivery_batch:{delivery_id}", value=batch_id,
        )
        await ego_crud.set_state(
            self._db, key=f"batch_delivery:{batch_id}", value=delivery_id,
        )

        logger.info(
            "Sent ego digest for batch %s (delivery_id=%s, %d proposals)",
            batch_id, delivery_id, len(proposals),
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
                self._db, prop["id"], status=status, user_response=reason,
            )
            if updated:
                results[prop["id"]] = status
                logger.info(
                    "Proposal %s → %s%s",
                    prop["id"], status,
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
                        self._db, prop["id"],
                        outcome_status=status,
                        actual_outcome=f"User {status}" + (f": {reason}" if reason else ""),
                        user_response=reason,
                    )
                except Exception:
                    logger.warning("Failed to update intervention journal for %s", prop["id"])
                # Auto-store correction memory on rejection with reason
                if (
                    status == ProposalStatus.REJECTED
                    and reason
                    and self._memory_store
                ):
                    await self._store_correction(prop, reason)
            else:
                logger.warning(
                    "Proposal %s not updated (already resolved?)", prop["id"],
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
            f"User rejected [{action_type}]: {content_snippet}. "
            f"Reason: {reason}. Do not repeat."
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

    # -- Late-binding for memory store (wired in init/ego.py) ----------------

    def set_memory_store(self, store: MemoryStore) -> None:
        """Attach a MemoryStore for storing correction memories on rejection."""
        self._memory_store = store


# ---------------------------------------------------------------------------
# Reply parser — converts user Telegram text into proposal decisions
# ---------------------------------------------------------------------------

_APPROVE_WORDS = {"approve", "approved", "yes", "accept", "go", "ok", "okay"}
_REJECT_WORDS = {"reject", "rejected", "no", "deny", "denied", "skip", "nope"}

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

    IMPORTANT: bare "approve" without "all" or a number does NOT match —
    it falls through to avoid false positives on conversational replies.
    """
    stripped = text.strip().lower()

    # Bulk operations
    if stripped in ("approve all", "approved all", "accept all", "yes all", "go ahead"):
        return {0: ("approved", None)}  # 0 = sentinel for "all"
    if stripped in ("reject all", "rejected all", "deny all", "no all"):
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
        # Unknown word → skip this part (don't fail the whole parse)

    return decisions
