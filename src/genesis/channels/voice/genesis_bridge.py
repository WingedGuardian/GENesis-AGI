"""Genesis bridge — handles tool calls from the S2S voice model.

The S2S model (GPT-Realtime or Gemini Live) acts as a conversational
front-end.  When it needs Genesis capabilities, it calls one of two
tools:

- ``ask_genesis(query)`` — Genesis decides internally what to do
  (memory recall, knowledge lookup, task dispatch, web search, etc.)
- ``web_search(query)`` — quick factual web lookup without Genesis

This module dispatches those tool calls to the appropriate Genesis
services and returns structured text results.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.channels.voice.handler import VoiceConversationHandler

logger = logging.getLogger(__name__)

_ESSENTIAL_KNOWLEDGE_PATH = Path.home() / ".genesis" / "essential_knowledge.md"

# Tool declarations for the S2S model session config
TOOL_DECLARATIONS = [
    {
        "type": "function",
        "name": "ask_genesis",
        "description": (
            "REQUIRED for any question about: conversations, past events, "
            "what we discussed, what we worked on, memories, personal context, "
            "projects, tasks, or anything the user has told you before. "
            "You do NOT have this information yourself — you MUST call this "
            "tool to access the user's history. Genesis has full memory."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The user's question, rephrased as a query",
                },
            },
            "required": ["query"],
        },
    },
    {
        "type": "function",
        "name": "web_search",
        "description": (
            "REQUIRED for any question about: current events, weather, news, "
            "scores, stock prices, real-time facts, or anything that changes "
            "over time. Also use when the user explicitly asks you to search. "
            "You do NOT have current information yourself — call this tool."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Web search query",
                },
            },
            "required": ["query"],
        },
    },
]

# System instructions for the S2S model
SYSTEM_INSTRUCTIONS = """\
You are Genesis, a cognitive AI partner, speaking through a voice interface.
You have two tools. Use them.

TOOL RULES (important — follow these strictly):
- "what did we do / work on / discuss" → ALWAYS call ask_genesis. Never guess.
- "search / look up / what's the weather / news" → ALWAYS call web_search.
- "can you search the web" or similar capability questions → call web_search \
with a relevant query to demonstrate the capability.
- Questions about the user's personal context, projects, history → call ask_genesis.
- General knowledge you're confident about → answer directly, no tool call.
- When in doubt between answering directly and calling a tool → call the tool. \
Better to be thorough than to guess wrong.

VOICE RULES:
- Keep responses to 1-3 sentences for most questions. Only go longer when \
the topic genuinely requires detail or you're reporting search/memory results.
- Never restate or paraphrase the question back. Never say "Great question" \
or similar filler. Never end with "let me know if you need anything" or \
"feel free to ask." Start with the answer, stop when you've given it.
- Never use markdown, bullet points, or formatting. Speak naturally.
- Don't narrate what you're doing ("Let me search..."). Just do it silently \
and report the result.

{voice_context}
"""


class GenesisBridge:
    """Dispatches S2S model tool calls to Genesis services.

    Delegates ``ask_genesis`` to the existing ``VoiceConversationHandler``
    (same cognitive logic as Phase 1 fallback — no DRY violation).
    Handles ``web_search`` independently since it's a new capability.
    """

    def __init__(
        self,
        *,
        voice_handler: VoiceConversationHandler | None = None,
    ) -> None:
        self._voice_handler = voice_handler

    async def handle_tool_call(
        self, name: str, arguments: str, *, satellite_id: str = "s2s-default",
    ) -> str:
        """Dispatch a tool call and return the result as JSON string."""
        try:
            args = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError:
            return json.dumps({"error": f"Invalid arguments: {arguments}"})

        if name == "ask_genesis":
            return await self._ask_genesis(
                args.get("query", ""), satellite_id=satellite_id,
            )
        if name == "web_search":
            return await self._web_search(args.get("query", ""))

        return json.dumps({"error": f"Unknown tool: {name}"})

    async def _ask_genesis(
        self, query: str, *, satellite_id: str = "s2s-default",
    ) -> str:
        """Recall memories and return raw snippets for S2S synthesis.

        Uses raw_snippets=True to skip the Groq LLM call — GPT-Realtime
        handles synthesis from the raw memory snippets, saving ~2s latency.
        Falls back to the full LLM path if raw recall fails.
        """
        if not self._voice_handler:
            return json.dumps({"answer": "Genesis voice handler not available."})

        session_id = f"s2s-{satellite_id}"
        try:
            response = await self._voice_handler.handle(
                transcript=query,
                session_id=session_id,
                raw_snippets=True,
            )
            return json.dumps({"answer": response})
        except Exception:
            logger.exception("ask_genesis raw recall failed, trying full path")

        # Fallback: full LLM path (Groq synthesis)
        try:
            response = await self._voice_handler.handle(
                transcript=query,
                session_id=session_id,
            )
            return json.dumps({"answer": response})
        except Exception:
            logger.exception("ask_genesis full path also failed")
            return json.dumps({"error": "Genesis processing failed"})

    async def _web_search(self, query: str) -> str:
        """Handle web_search tool call — quick factual lookup."""
        try:
            from genesis.mcp.health.web_tools import _impl_web_search
            result = await _impl_web_search(query, backend="brave", max_results=3)
            search_results = result.get("results", [])
            if search_results:
                snippets = [
                    f"{r.get('title', '')}: {r.get('snippet', '')}"
                    for r in search_results[:3]
                ]
                return json.dumps({"results": snippets})
            return json.dumps({"results": [], "note": "No results found"})
        except ImportError:
            logger.warning("Web search provider not available")
        except Exception:
            logger.exception("Web search failed for voice query")

        return json.dumps({"error": "Web search unavailable"})

    def get_system_prompt(self) -> str:
        """Build the system prompt with curated voice context.

        Extracts only the Active Context section from essential knowledge —
        the rest (wing counts, conversation pivots, ego proposals) is system
        telemetry that wastes tokens and confuses the voice model.
        """
        voice_ctx = ""
        if _ESSENTIAL_KNOWLEDGE_PATH.exists():
            voice_ctx = _extract_voice_context(
                _ESSENTIAL_KNOWLEDGE_PATH.read_text(),
            )

        return SYSTEM_INSTRUCTIONS.format(
            voice_context=f"\nWhat the user has been working on recently:\n{voice_ctx}"
            if voice_ctx else "",
        )


def _extract_voice_context(ek_text: str, max_chars: int = 500) -> str:
    """Extract the Active Context section from essential knowledge.

    Strips system telemetry (wing counts, conversation pivots, ego
    proposals, observation IDs) — keep only plain-language project context.
    """
    lines = ek_text.split("\n")
    in_active_context = False
    context_lines = []

    for line in lines:
        # Start collecting at "Active Context"
        if "### Active Context" in line:
            in_active_context = True
            continue
        # Stop at the next section header
        if in_active_context and line.startswith("### "):
            break
        if in_active_context and line.strip():
            # Strip leading "- " for cleaner voice context
            clean = line.strip().lstrip("- ")
            if clean:
                context_lines.append(clean)

    result = ". ".join(context_lines)
    return result[:max_chars] if result else ""
