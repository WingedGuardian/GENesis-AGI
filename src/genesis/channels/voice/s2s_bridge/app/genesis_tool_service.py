"""Genesis tool dispatch — HTTP client for the S2S voice bridge addon.

Replaces the HA MCP integration from the upstream project.  The addon
calls Genesis's Flask endpoints to dispatch tool calls triggered by
the OpenAI Realtime model during a voice conversation.

Endpoints called:
- ``POST /v1/voice/tool_call`` — dispatch ask_genesis, web_search, approve_pending
- ``GET /v1/voice/system_prompt`` — fetch Genesis persona + context
- ``GET /v1/voice/tool_declarations`` — fetch tool schemas for session config
- ``POST /api/t/memory_store`` — persist conversation transcripts (Phase 3D)
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class GenesisToolService:
    """HTTP client for Genesis voice tool dispatch.

    Called by the Pipecat pipeline when the OpenAI Realtime model
    triggers a function call (ask_genesis, web_search, approve_pending).
    """

    def __init__(self, genesis_url: str, token: str = "") -> None:
        self._url = genesis_url.rstrip("/")
        self._headers: dict[str, str] = {"Content-Type": "application/json"}
        if token:
            self._headers["Authorization"] = f"Bearer {token}"

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict:
        """Dispatch a tool call to Genesis and return the result."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self._url}/v1/voice/tool_call",
                headers=self._headers,
                json={"tool_name": name, "arguments": arguments},
            )
            resp.raise_for_status()
            return resp.json()

    async def get_system_prompt(self) -> str:
        """Fetch the Genesis voice system prompt."""
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{self._url}/v1/voice/system_prompt",
                headers=self._headers,
            )
            resp.raise_for_status()
            return resp.json().get("prompt", "")

    async def get_tool_declarations(self) -> list[dict]:
        """Fetch Genesis tool declarations for OpenAI session config."""
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{self._url}/v1/voice/tool_declarations",
                headers=self._headers,
            )
            resp.raise_for_status()
            return resp.json().get("tools", [])

    async def store_conversation(
        self, messages: list, satellite_id: str = "s2s-default",
    ) -> dict | None:
        """Persist a voice conversation transcript to Genesis memory.

        Matches the format used by ``S2SSessionManager.close()``
        (s2s_session.py) — same source, tags, wing, room — so all voice
        conversations appear in the same memory namespace regardless of
        which pipeline produced them.
        """
        turns: list[str] = []
        for msg in messages:
            role = msg.get("role", "") if isinstance(msg, dict) else getattr(msg, "role", "")
            content = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")
            if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                label = "User" if role == "user" else "Genesis"
                turns.append(f"{label}: {content}")

        if not turns:
            logger.debug("No user/assistant turns to persist")
            return None

        transcript = f"Voice conversation [{satellite_id}]:\n" + "\n".join(turns)
        logger.info(
            "Persisting voice conversation (%d turns, %d chars)",
            len(turns), len(transcript),
        )

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{self._url}/api/t/memory_store",
                headers=self._headers,
                json={
                    "content": transcript,
                    "source": "voice_s2s",
                    "memory_type": "episodic",
                    "tags": ["voice", "s2s", "conversation"],
                    "confidence": 0.5,
                    "wing": "channels",
                    "room": "voice",
                },
            )
            resp.raise_for_status()
            return resp.json()
