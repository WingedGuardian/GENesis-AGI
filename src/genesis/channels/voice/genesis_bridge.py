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
            "Ask the Genesis backend for memory recall, knowledge lookup, "
            "task dispatch, or any reasoning that requires the user's personal "
            "context. Genesis will figure out what tools and capabilities to "
            "use internally."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to ask Genesis",
                },
            },
            "required": ["query"],
        },
    },
    {
        "type": "function",
        "name": "web_search",
        "description": (
            "Quick web search for current facts like weather, news, scores, "
            "stock prices. Use for simple factual lookups that don't need "
            "Genesis's memory or knowledge base."
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
You remember the user's history, projects, and preferences through your tools.

Rules:
- Be concise. Spoken responses should be 1-3 sentences unless asked for detail.
- Never use markdown. Speak naturally, like a knowledgeable colleague.
- When the user asks about their past work, memories, or personal context: \
call ask_genesis.
- When the user asks about current events, weather, or real-time facts: \
call web_search.
- For general knowledge: answer directly, no tool call needed.
- If a request would take time, say you'll look into it.

{essential_knowledge}
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
        self, name: str, arguments: str,
    ) -> str:
        """Dispatch a tool call and return the result as JSON string."""
        try:
            args = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError:
            return json.dumps({"error": f"Invalid arguments: {arguments}"})

        if name == "ask_genesis":
            return await self._ask_genesis(args.get("query", ""))
        if name == "web_search":
            return await self._web_search(args.get("query", ""))

        return json.dumps({"error": f"Unknown tool: {name}"})

    async def _ask_genesis(self, query: str) -> str:
        """Delegate to VoiceConversationHandler — same cognitive path as Phase 1."""
        if not self._voice_handler:
            return json.dumps({"answer": "Genesis voice handler not available."})

        try:
            # Use a stable session ID for S2S tool calls so context accumulates
            response = await self._voice_handler.handle(
                transcript=query,
                session_id="s2s-tool-bridge",
            )
            return json.dumps({"answer": response})
        except Exception:
            logger.exception("ask_genesis tool call failed")
            return json.dumps({"error": "Genesis processing failed"})

    async def _web_search(self, query: str) -> str:
        """Handle web_search tool call — quick factual lookup."""
        try:
            from genesis.mcp.health.web_tools import web_search
            result = await web_search(query, max_results=3)
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
        """Build the system prompt with essential knowledge."""
        ek = ""
        if _ESSENTIAL_KNOWLEDGE_PATH.exists():
            ek = _ESSENTIAL_KNOWLEDGE_PATH.read_text()[:2000]

        return SYSTEM_INSTRUCTIONS.format(
            essential_knowledge=f"\nCurrent context:\n{ek}" if ek else "",
        )
