"""Voice conversation handler — fast path bypassing CC.

Provides <5s latency by doing memory recall + direct router call instead
of spawning a CC subprocess.  For complex queries that need tool access,
the LLM response can suggest follow-up via Telegram.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from genesis.channels.tts_config import sanitize_for_speech
from genesis.channels.voice.sessions import VoiceSessionManager

if TYPE_CHECKING:
    from genesis.memory.retrieval import HybridRetriever
    from genesis.routing.router import ModelRouter

logger = logging.getLogger(__name__)

_VOICE_CALL_SITE = "voice_conversation"
_ESSENTIAL_KNOWLEDGE_PATH = Path.home() / ".genesis" / "essential_knowledge.md"

_VOICE_SYSTEM_PROMPT = """\
You are Genesis, a cognitive AI partner, speaking through a voice interface.
You have access to recalled memories and knowledge provided below as context.

Voice rules:
- Be concise. Spoken responses should be 1-3 sentences unless the user asks for detail.
- Never use markdown formatting — no bullets, headers, code blocks, or URLs.
- Speak naturally, as a knowledgeable colleague would.
- If you don't know something, say so briefly.
- For complex requests that would take time, say you'll look into it.

{context}
"""


class VoiceConversationHandler:
    """Handles voice transcripts: recall context, call LLM, return response."""

    def __init__(
        self,
        *,
        retriever: HybridRetriever,
        router: ModelRouter,
        session_manager: VoiceSessionManager | None = None,
    ) -> None:
        self._retriever = retriever
        self._router = router
        self._sessions = session_manager or VoiceSessionManager()

    @property
    def session_manager(self) -> VoiceSessionManager:
        return self._sessions

    async def handle(self, transcript: str, session_id: str) -> str:
        """Process a voice transcript and return a spoken response.

        1. Get/create session
        2. Recall relevant memories
        3. Assemble context (essential knowledge + memories + buffer)
        4. Call router with voice-optimized system prompt
        5. Store turn in buffer
        6. Return sanitized response
        """
        if not transcript.strip():
            return "I didn't catch that. Could you say that again?"

        # 1. Session — touch to reset sustain timer
        await self._sessions.get_or_create(session_id)

        # 2. Memory recall (best-effort — don't fail the whole request)
        memories_text = ""
        try:
            results = await self._retriever.recall(transcript, limit=5, rerank=False)
            if results:
                snippets = []
                for r in results[:5]:
                    content = getattr(r, "content", str(r))
                    if isinstance(content, str) and content.strip():
                        snippets.append(f"- {content[:300]}")
                if snippets:
                    memories_text = "Recalled memories:\n" + "\n".join(snippets)
        except Exception:
            logger.warning(
                "Memory recall failed for voice session %s",
                session_id[:12], exc_info=True,
            )

        # 3. Assemble context
        essential = ""
        try:
            if _ESSENTIAL_KNOWLEDGE_PATH.exists():
                essential = _ESSENTIAL_KNOWLEDGE_PATH.read_text(
                    encoding="utf-8",
                )[:2000]
        except Exception:
            pass

        context_parts = []
        if essential:
            context_parts.append(f"Essential knowledge:\n{essential}")
        if memories_text:
            context_parts.append(memories_text)

        context_block = "\n\n".join(context_parts) if context_parts else ""
        system_prompt = _VOICE_SYSTEM_PROMPT.format(context=context_block)

        # 4. Build messages (system + buffer + current transcript)
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
        ]
        buffer = self._sessions.get_buffer(session_id)
        if buffer:
            messages.extend(buffer)
        messages.append({"role": "user", "content": transcript})

        # 5. Call router
        try:
            result = await self._router.route_call(
                call_site_id=_VOICE_CALL_SITE,
                messages=messages,
            )
            if not result.success:
                logger.error(
                    "Voice router call failed: %s", result.error,
                )
                return "I'm having trouble thinking right now. Try again in a moment."
            response = result.content or "I processed your request but don't have a response."
        except Exception:
            logger.error(
                "Voice handler exception for session %s",
                session_id[:12], exc_info=True,
            )
            return "Something went wrong on my end. Try again."

        # 6. Store turns in buffer
        await self._sessions.add_turn(session_id, "user", transcript)
        await self._sessions.add_turn(session_id, "assistant", response)

        # 7. Sanitize for speech (strip any markdown the LLM sneaks in)
        return sanitize_for_speech(response)
