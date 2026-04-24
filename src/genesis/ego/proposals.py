"""Ego proposal workflow — lifecycle from creation to Telegram approval.

Handles: proposal creation, batch digest formatting, Telegram delivery
via TopicManager, reply parsing, status resolution, and expiry sweeps.
"""

from __future__ import annotations

import html
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from genesis.db.crud import ego as ego_crud
from genesis.ego.types import ProposalStatus

if TYPE_CHECKING:
    import aiosqlite

    from genesis.channels.telegram.topics import TopicManager
    from genesis.outreach.reply_waiter import ReplyWaiter

logger = logging.getLogger(__name__)

TOPIC_CATEGORY = "ego_proposals"

# ---------------------------------------------------------------------------
# Reply parsing
# ---------------------------------------------------------------------------

_APPROVE_WORDS = frozenset({
    "approve", "approved", "ok", "lgtm", "yes", "go", "ship",
})
_REJECT_WORDS = frozenset({
    "reject", "rejected", "no", "nope", "deny", "denied",
})
_ALL_WORDS = frozenset({"all", "everything"})
_NUM_RE = re.compile(r"\d+")
_REASON_RE = re.compile(r"[\u2014\-:]\s*")  # em-dash, hyphen, colon


def _extract_reason(text: str) -> str | None:
    """Extract rejection reason after separator (—, -, :)."""
    parts = _REASON_RE.split(text, maxsplit=1)
    if len(parts) > 1:
        reason = parts[1].strip()
        return reason if reason else None
    return None


def _parse_line(
    line: str,
    count: int,
    out: dict[int, tuple[str, str | None]],
) -> None:
    """Parse one line/clause for approve/reject + numbers."""
    lower = line.strip().lower()
    if not lower:
        return

    # Determine verb
    words = lower.split()
    verb = words[0] if words else ""
    status: str | None = None
    if verb in _APPROVE_WORDS:
        status = ProposalStatus.APPROVED
    elif verb in _REJECT_WORDS:
        status = ProposalStatus.REJECTED
    else:
        return  # not a recognized command line

    rest = line.strip()[len(verb):].strip()

    # Check for "all"
    rest_words = rest.lower().split()
    if not rest_words or (rest_words[0] in _ALL_WORDS):
        reason = _extract_reason(rest) if status == ProposalStatus.REJECTED else None
        for i in range(1, count + 1):
            out[i] = (status, reason)
        return

    # Extract numbers from text BEFORE the reason separator only.
    # Without this, "reject 2 — first one is low-priority" would match "1"
    # from the reason text.
    reason: str | None = None
    num_text = rest
    if status == ProposalStatus.REJECTED:
        parts = _REASON_RE.split(rest, maxsplit=1)
        num_text = parts[0]
        reason = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None

    nums = [int(n) for n in _NUM_RE.findall(num_text) if 1 <= int(n) <= count]
    for n in nums:
        out[n] = (status, reason)


def parse_reply(
    reply_text: str,
    proposal_count: int,
) -> dict[int, tuple[str, str | None]]:
    """Parse user reply into {1-based index: (status, reason|None)}.

    Supports:
    - ``"approve all"`` / ``"ok"`` / ``"lgtm"`` / ``"yes"``
    - ``"reject all"`` / ``"reject all — bad idea"``
    - ``"approve 1,3"``
    - ``"reject 2 — not worth it"``
    - ``"approve 1,3 reject 2"``  (mixed on one line)
    - ``"1,3"``  (bare numbers → approve)
    - Empty / unparseable → empty dict (safe default)
    """
    text = reply_text.strip()
    if not text:
        return {}

    lower = text.lower()
    words = lower.split()
    first = words[0] if words else ""
    result: dict[int, tuple[str, str | None]] = {}

    # Global approve shortcuts
    if first in _APPROVE_WORDS:
        rest = lower[len(first):].strip()
        rest_words = rest.split()
        if not rest_words or rest_words[0] in _ALL_WORDS:
            return {i: (ProposalStatus.APPROVED, None) for i in range(1, proposal_count + 1)}

    # Global reject shortcuts
    if first in _REJECT_WORDS:
        rest = text[len(first):].strip()
        rest_lower = rest.lower().split()
        if not rest_lower or rest_lower[0] in _ALL_WORDS:
            reason = _extract_reason(rest)
            return {i: (ProposalStatus.REJECTED, reason) for i in range(1, proposal_count + 1)}

    # Split on newlines and semicolons, also split "approve X reject Y" on keyword boundaries
    chunks = re.split(r"[\n;]+", text)
    expanded: list[str] = []
    for chunk in chunks:
        # Split "approve 1,3 reject 2" into two lines
        parts = re.split(r"\b(?=(?:approve|reject|deny)\b)", chunk, flags=re.IGNORECASE)
        expanded.extend(parts)

    for line in expanded:
        _parse_line(line, proposal_count, result)

    # Bare numbers with no verb → approve
    if not result:
        nums = [int(n) for n in _NUM_RE.findall(text) if 1 <= int(n) <= proposal_count]
        for n in nums:
            result[n] = (ProposalStatus.APPROVED, None)

    return result


# ---------------------------------------------------------------------------
# Digest formatting
# ---------------------------------------------------------------------------

_ESC = html.escape


def _format_digest(
    proposals: list[dict],
    batch_id: str,
    expiry_minutes: int,
) -> str:
    """Format proposals as an HTML numbered digest for Telegram."""
    lines = [f"<b>Ego Proposals</b> \u2014 Batch {_ESC(batch_id[:8])}\n"]

    for i, p in enumerate(proposals, 1):
        content = p.get("content", "")
        if len(content) > 200:
            content = content[:200] + "\u2026"
        rationale = p.get("rationale", "")
        if len(rationale) > 150:
            rationale = rationale[:150] + "\u2026"

        lines.append(
            f"<b>{i}.</b> <b>[{_ESC(p.get('action_type', '?'))}]</b> "
            f"{_ESC(content)}"
        )
        if rationale:
            lines.append(f"<i>Rationale:</i> {_ESC(rationale)}")
        confidence = p.get("confidence", 0.0)
        urgency = p.get("urgency", "normal")
        lines.append(
            f"<i>Confidence:</i> {confidence:.2f} | "
            f"<i>Urgency:</i> {urgency}"
        )
        alts = p.get("alternatives", "")
        if alts:
            if len(alts) > 150:
                alts = alts[:150] + "\u2026"
            lines.append(f"<i>Alternatives:</i> {_ESC(alts)}")
        lines.append("")

    lines.append("<i>Reply to this message to approve/reject. Examples:</i>")
    lines.append("<code>approve all</code>")
    lines.append("<code>approve 1,3</code>")
    lines.append("<code>reject 2 \u2014 not worth it</code>")
    hours = max(1, expiry_minutes // 60)
    lines.append(f"\n<i>Expires in {hours} hours.</i>")

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
    reply_waiter:
        ReplyWaiter for detecting quote-replies to proposal digests.
        None if reply detection is not available.
    expiry_minutes:
        Default time-to-live for proposals (minutes).
    """

    def __init__(
        self,
        *,
        db: aiosqlite.Connection,
        topic_manager: TopicManager | None = None,
        reply_waiter: ReplyWaiter | None = None,
        expiry_minutes: int = 240,
    ) -> None:
        self._db = db
        self._topic_manager = topic_manager
        self._reply_waiter = reply_waiter
        self._expiry_minutes = expiry_minutes

    # -- Late-binding setters (wired in standalone.py after Telegram init) --

    def set_topic_manager(self, topic_manager: TopicManager) -> None:
        """Attach a TopicManager for Telegram proposal delivery."""
        self._topic_manager = topic_manager

    def set_reply_waiter(self, waiter: ReplyWaiter) -> None:
        """Attach a ReplyWaiter for detecting user responses to proposals."""
        self._reply_waiter = waiter

    # -- Creation ----------------------------------------------------------

    async def create_batch(
        self,
        proposals: list[dict],
        *,
        cycle_id: str | None = None,
        expiry_minutes: int | None = None,
    ) -> tuple[str, list[str]]:
        """Create a batch of proposals from ego output dicts.

        Returns ``(batch_id, [proposal_ids])``.
        """
        batch_id = uuid.uuid4().hex[:16]
        ttl = expiry_minutes if expiry_minutes is not None else self._expiry_minutes
        now = datetime.now(UTC)
        expires_at = (now + timedelta(minutes=ttl)).isoformat()
        created_at = now.isoformat()

        ids: list[str] = []
        for p in proposals:
            pid = uuid.uuid4().hex[:16]
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
                expires_at=expires_at,
            )
            ids.append(pid)

        logger.info(
            "Created ego proposal batch %s with %d proposals (expires %s)",
            batch_id, len(ids), expires_at,
        )
        return batch_id, ids

    # -- Formatting --------------------------------------------------------

    def format_digest(
        self,
        proposals: list[dict],
        batch_id: str,
    ) -> str:
        """Format proposals as an HTML numbered digest for Telegram."""
        return _format_digest(proposals, batch_id, self._expiry_minutes)

    # -- Delivery ----------------------------------------------------------

    async def send_digest(self, batch_id: str) -> str | None:
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

    async def wait_and_process_reply(
        self,
        batch_id: str,
        delivery_id: str,
        *,
        timeout_s: float | None = None,
    ) -> dict[str, str]:
        """Wait for user reply and process it.

        Returns ``{proposal_id: new_status}`` for resolved proposals.
        Empty dict if timeout or no reply waiter.
        """
        if self._reply_waiter is None:
            logger.warning("No reply_waiter — cannot await approval")
            return {}

        if timeout_s is None:
            timeout_s = self._expiry_minutes * 60.0

        reply_text = await self._reply_waiter.wait_for_reply(
            delivery_id, timeout_s=timeout_s,
        )

        if reply_text is None:
            logger.info("No reply for batch %s (timed out)", batch_id)
            return {}

        proposals = await ego_crud.list_proposals_by_batch(self._db, batch_id)
        decisions = parse_reply(reply_text, len(proposals))

        if not decisions:
            logger.warning(
                "Could not parse reply for batch %s: %r",
                batch_id, reply_text[:200],
            )
            return {}

        return await self.resolve_proposals(batch_id, decisions)

    async def resolve_proposals(
        self,
        batch_id: str,
        decisions: dict[int, tuple[str, str | None]],
    ) -> dict[str, str]:
        """Apply parsed decisions to proposals in the batch.

        ``decisions`` maps 1-based index → (status, optional reason).
        Returns ``{proposal_id: final_status}``.
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
            else:
                logger.warning(
                    "Proposal %s not updated (already resolved?)", prop["id"],
                )

        return results

    # -- Expiry ------------------------------------------------------------

    async def expire_stale(self) -> int:
        """Expire all pending proposals past their expires_at.

        TODO(batch-6): Also clean up stale ego_state entries
        (delivery_batch:* and batch_delivery:*) for expired batches
        to prevent unbounded KV accumulation.
        """
        now = datetime.now(UTC).isoformat()
        count = await ego_crud.expire_proposals(self._db, now=now)
        if count:
            logger.info("Expired %d stale ego proposals", count)
        return count
