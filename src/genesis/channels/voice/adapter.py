"""Voice channel adapter — HA TTS for outbound speech.

Implements ``ChannelAdapter`` for voice output through Home Assistant.
Inbound voice (HA → Genesis) arrives via the Flask endpoint in
``dashboard/routes/voice_api.py``, not through this adapter.  This
adapter handles outbound only: Genesis speaking to the user proactively.

NOT registered with the outreach pipeline via ``register_channel`` —
outreach delivery at arbitrary times (3am morning reports) would be
disruptive.  Instead, held directly by the standalone server for
explicit voice output when needed.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

import httpx

from genesis.channels.base import ChannelAdapter

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class VoiceChannelAdapter(ChannelAdapter):
    """Outbound voice via Home Assistant TTS service."""

    def __init__(
        self,
        *,
        ha_url: str | None = None,
        ha_token: str | None = None,
        tts_entity: str = "tts.piper",
        media_player_entity: str = "media_player.home_assistant_voice_0a2841",
    ) -> None:
        self._ha_url = ha_url.rstrip("/") if ha_url else None
        self._ha_token = ha_token
        self._tts_entity = tts_entity
        self._media_player = media_player_entity

    async def start(self) -> None:
        """No-op — adapter is stateless, HA handles transport."""
        logger.info("Voice channel adapter started (outbound only)")

    async def stop(self) -> None:
        """No-op — no persistent connections."""

    async def send_message(
        self,
        channel_id: str,
        text: str,
        *,
        message_thread_id: int | None = None,
        **kwargs,
    ) -> str:
        """Speak text through HA TTS.

        Uses HA's ``tts.speak`` service to synthesize and play audio on
        the target media player (Voice PE satellite).
        """
        if not self._ha_url or not self._ha_token:
            logger.warning("Voice adapter: HA not configured, skipping TTS")
            return ""

        delivery_id = str(uuid.uuid4())

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(
                    f"{self._ha_url}/api/services/tts/speak",
                    headers={
                        "Authorization": f"Bearer {self._ha_token}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "entity_id": self._tts_entity,
                        "media_player_entity_id": self._media_player,
                        "message": text,
                    },
                )
                response.raise_for_status()
                logger.info(
                    "Voice TTS delivered: %d chars → %s",
                    len(text), self._media_player,
                )
        except Exception:
            logger.error("Voice TTS delivery failed", exc_info=True)

        return delivery_id

    async def send_typing(self, channel_id: str) -> None:
        """No typing indicator for voice."""

    def get_capabilities(self) -> dict:
        return {
            "markdown": False,
            "buttons": False,
            "reactions": False,
            "voice": True,
            "documents": False,
            "max_length": 2000,
        }

    async def get_engagement_signals(self, delivery_id: str) -> dict:
        """Voice has no engagement tracking."""
        return {"signal": "neutral", "details": {}}
