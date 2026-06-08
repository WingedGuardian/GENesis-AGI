"""ReplyHandler — dispatches DirectSessions for autonomous email replies.

When the ReplyPoller detects a reply to a registered thread, this handler
creates a background CC session that:
1. Reads the thread history (original message + reply)
2. Drafts a contextual response
3. Sends it via outreach_send
4. Escalates to ego via directive if human judgment is needed

Uses the 'campaign' profile (allows outreach_send, doesn't force Opus).
"""

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.cc.direct_session import DirectSessionRunner
    from genesis.mail.reply_poller import ParsedReply
    from genesis.mail.threads import ThreadTracker

logger = logging.getLogger(__name__)

_IDENTITY_DIR = Path(__file__).resolve().parents[1] / "identity"
_MAIL_REPLY_PROMPT = _IDENTITY_DIR / "MAIL_REPLY.md"


def _build_reply_prompt(thread: dict, reply: ParsedReply, history: list[dict]) -> str:
    """Build the user prompt for the reply session."""
    parts = [
        "## Email Reply — Thread Context\n",
        f"**Thread ID:** {thread['id']}",
        f"**Recipient:** {thread['recipient']}",
        f"**Original subject:** {thread.get('subject', 'N/A')}",
        f"**Thread owner:** {thread['owner']}",
    ]

    if thread.get("owner_ref"):
        parts.append(f"**Owner ref:** {thread['owner_ref']}")

    context = thread.get("context")
    if context:
        if isinstance(context, str):
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                context = json.loads(context)
        parts.append(f"\n**Context:** {json.dumps(context, indent=2) if isinstance(context, dict) else context}")

    parts.append("\n## Thread History\n")
    for msg in history:
        direction = "SENT" if msg["direction"] == "sent" else "RECEIVED"
        parts.append(f"### [{direction}] {msg.get('subject', 'N/A')}")
        parts.append(f"**From:** {msg.get('sender', 'N/A')}")
        parts.append(f"**Date:** {msg.get('received_at', 'N/A')}")
        if msg.get("body_preview"):
            parts.append(f"\n{msg['body_preview']}\n")

    parts.append("## New Reply (respond to this)\n")
    parts.append(f"**From:** {reply.sender}")
    parts.append(f"**Subject:** {reply.subject}")
    parts.append(f"\n{reply.body_preview}\n")

    return "\n".join(parts)


def _build_follow_up_prompt(thread: dict, history: list[dict]) -> str:
    """Build the user prompt for a follow-up session (no reply received)."""
    parts = [
        "## Follow-Up Email — No Reply Received\n",
        f"**Thread ID:** {thread['id']}",
        f"**Recipient:** {thread['recipient']}",
        f"**Original subject:** {thread.get('subject', 'N/A')}",
        "**Days since sent:** 4+",
        f"**Thread owner:** {thread['owner']}",
    ]

    if thread.get("owner_ref"):
        parts.append(f"**Owner ref:** {thread['owner_ref']}")

    context = thread.get("context")
    if context:
        if isinstance(context, dict):
            parts.append(f"\n**Context:** {json.dumps(context, indent=2)}")
        else:
            parts.append(f"\n**Context:** {context}")

    parts.append("\n## Original Thread\n")
    for msg in history:
        parts.append(f"### [{msg['direction'].upper()}] {msg.get('subject', 'N/A')}")
        parts.append(f"**From:** {msg.get('sender', 'N/A')}")
        parts.append(f"**Date:** {msg.get('received_at', 'N/A')}")
        if msg.get("body_preview"):
            parts.append(f"\n{msg['body_preview']}\n")

    parts.append(
        "\nDraft and send a brief, friendly follow-up to the recipient. "
        "Reference the original email naturally. Keep it short (2-3 sentences). "
        "Do NOT be pushy. If the original pitch was cold outreach, a single "
        "follow-up is all that's appropriate."
    )

    return "\n".join(parts)


class ReplyHandler:
    """Handles email replies by dispatching autonomous CC sessions."""

    def __init__(
        self,
        *,
        session_runner: DirectSessionRunner,
        thread_tracker: ThreadTracker,
    ) -> None:
        self._runner = session_runner
        self._tracker = thread_tracker
        self._system_prompt: str | None = None

    def _load_system_prompt(self) -> str:
        """Load MAIL_REPLY.md system prompt (lazy, cached)."""
        if self._system_prompt is None:
            if _MAIL_REPLY_PROMPT.exists():
                self._system_prompt = _MAIL_REPLY_PROMPT.read_text()
            else:
                logger.warning("MAIL_REPLY.md not found, using minimal prompt")
                self._system_prompt = (
                    "You are Genesis, responding to an email reply on your own email address. "
                    "Be direct, professional, and helpful. Send your response via outreach_send "
                    "with channel='email'. If the reply requires human judgment (commitments, "
                    "scheduling, unclear intent), create an ego_directive to escalate."
                )
        return self._system_prompt

    async def handle_reply(self, thread: dict, reply: ParsedReply) -> str | None:
        """Handle an incoming reply by dispatching a DirectSession.

        Returns:
            Session ID if dispatched, None if skipped.
        """
        from genesis.cc.direct_session import DirectSessionRequest
        from genesis.cc.types import CCModel, EffortLevel

        history = await self._tracker.get_thread_history(thread["id"])
        prompt = _build_reply_prompt(thread, reply, history)
        system_prompt = self._load_system_prompt()

        request = DirectSessionRequest(
            prompt=prompt,
            profile="campaign",
            model=CCModel.SONNET,
            effort=EffortLevel.HIGH,
            system_prompt=system_prompt,
            timeout_s=600,  # 10 min — email replies should be quick
            notify=False,
            source_tag="mail_reply",
            caller_context=f"email_thread:{thread['id']}",
        )

        try:
            session_id = await self._runner.spawn(request)
            logger.info(
                "Dispatched reply session %s for thread %s",
                session_id, thread["id"],
            )
            return session_id
        except Exception:
            logger.error(
                "Failed to dispatch reply session for thread %s",
                thread["id"], exc_info=True,
            )
            return None

    async def handle_follow_up(self, thread: dict, _reply: None) -> str | None:
        """Handle a stale thread by dispatching a follow-up session.

        Returns:
            Session ID if dispatched, None if skipped.
        """
        from genesis.cc.direct_session import DirectSessionRequest
        from genesis.cc.types import CCModel, EffortLevel

        history = await self._tracker.get_thread_history(thread["id"])
        prompt = _build_follow_up_prompt(thread, history)
        system_prompt = self._load_system_prompt()

        request = DirectSessionRequest(
            prompt=prompt,
            profile="campaign",
            model=CCModel.SONNET,
            effort=EffortLevel.MEDIUM,
            system_prompt=system_prompt,
            timeout_s=600,
            notify=False,
            source_tag="mail_follow_up",
            caller_context=f"email_thread:{thread['id']}",
        )

        try:
            session_id = await self._runner.spawn(request)
            # Mark thread as follow_up_sent BEFORE the session runs
            # (the session will send the actual email)
            await self._tracker.mark_follow_up_sent(thread["id"])
            logger.info(
                "Dispatched follow-up session %s for thread %s",
                session_id, thread["id"],
            )
            return session_id
        except Exception:
            logger.error(
                "Failed to dispatch follow-up for thread %s",
                thread["id"], exc_info=True,
            )
            return None
