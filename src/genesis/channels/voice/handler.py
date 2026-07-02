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
from genesis.memory.provenance import is_external, wrap_external_recall

if TYPE_CHECKING:
    from genesis.memory.retrieval import HybridRetriever
    from genesis.routing.router import ModelRouter

logger = logging.getLogger(__name__)

_VOICE_CALL_SITE = "voice_conversation"
_ESSENTIAL_KNOWLEDGE_PATH = Path.home() / ".genesis" / "essential_knowledge.md"

_VOICE_SYSTEM_PROMPT = """\
You are Genesis, a cognitive AI partner. You are responding to a voice query.
You have full memory across ALL channels — Telegram, dashboard, voice, and
background sessions. The recalled memories below come from your complete
history with the user, not just voice conversations.

Answer the user's question using the recalled memories. If memories are
provided, use them — don't say "this is our first conversation" unless
the memories section is truly empty.

Voice rules:
- Respond naturally.
- Never use markdown formatting. Speak naturally.
- If you don't know something, say so briefly.

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

    async def handle(
        self, transcript: str, session_id: str, *, raw_snippets: bool = False,
    ) -> str:
        """Process a voice transcript and return a spoken response.

        Args:
            transcript: The user's spoken text.
            session_id: Voice session identifier.
            raw_snippets: If True, return formatted memory snippets directly
                without calling the LLM router. Used by S2S pipeline where
                GPT-Realtime handles synthesis, saving ~2s of Groq LLM latency.

        Steps (full path):
        1. Get/create session
        2. Recall relevant memories
        3. Assemble context (essential knowledge + memories + buffer)
        4. Call router with voice-optimized system prompt
        5. Store turn in buffer
        6. Return sanitized response

        Steps (raw_snippets path):
        1. Recall relevant memories
        2. Return formatted snippets (skip LLM, session, context assembly)
        """
        if not transcript.strip():
            return "I didn't catch that. Could you say that again?"

        # Memory recall (best-effort — don't fail the whole request).
        # Two renderings of the SAME recalled set:
        #   memories_text     — SPOKEN path: soft `[external-world knowledge]`
        #                       label (can't speak XML boundary markers) (D12).
        #   llm_memories_text — FULL LLM path: external-world content wrapped in
        #                       <external-content> so a payload in recalled KB is
        #                       treated as data in the system prompt, not an
        #                       instruction (PR2 injection defense).
        memories_text = ""
        llm_memories_text = ""
        try:
            results = await self._retriever.recall(transcript, limit=5, rerank=False)
            if results:
                spoken_snippets = []
                llm_snippets = []
                for r in results[:5]:
                    content = getattr(r, "content", str(r))
                    if isinstance(content, str) and content.strip():
                        external = is_external(getattr(r, "collection", ""))
                        source_pipeline = getattr(r, "source_pipeline", None)
                        prefix = "[external-world knowledge] " if external else ""
                        spoken_snippets.append(f"- {prefix}{content[:300]}")
                        if external:
                            llm_snippets.append(
                                "- " + wrap_external_recall(
                                    content[:300], source_pipeline=source_pipeline,
                                )
                            )
                        else:
                            llm_snippets.append(f"- {content[:300]}")
                if spoken_snippets:
                    memories_text = "Recalled memories:\n" + "\n".join(spoken_snippets)
                    llm_memories_text = "Recalled memories:\n" + "\n".join(llm_snippets)
        except Exception:
            logger.warning(
                "Memory recall failed for voice session %s",
                session_id[:12], exc_info=True,
            )

        # Raw snippets path — return memories directly for S2S synthesis
        if raw_snippets:
            return memories_text or "No relevant memories found for this query."

        # Full path: session + context assembly + LLM call

        # 1. Session — touch to reset sustain timer
        await self._sessions.get_or_create(session_id)

        # 2. Assemble context
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
        if llm_memories_text:
            # Full LLM path uses the wrapped rendering (injection defense).
            context_parts.append(llm_memories_text)

        context_block = "\n\n".join(context_parts) if context_parts else ""
        system_prompt = _VOICE_SYSTEM_PROMPT.format(context=context_block)

        # 3. Build messages (system + buffer + current transcript)
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
        ]
        buffer = self._sessions.get_buffer(session_id)
        if buffer:
            messages.extend(buffer)
        messages.append({"role": "user", "content": transcript})

        # 4. Call router
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

        # 5. Store turns in buffer
        await self._sessions.add_turn(session_id, "user", transcript)
        await self._sessions.add_turn(session_id, "assistant", response)

        # 6. Sanitize for speech (strip any markdown the LLM sneaks in)
        return sanitize_for_speech(response)
