"""Voice channel adapter — HA TTS with pre-announce chime for outbound speech.

Implements ``ChannelAdapter`` for voice output through Home Assistant.
Inbound voice (HA → Genesis) arrives via the Flask endpoint in
``dashboard/routes/voice_api.py``, not through this adapter.  This
adapter handles outbound only: Genesis speaking to the user proactively.

Two-step delivery:
1. ``assist_satellite.announce`` with empty message — plays the built-in
   pre-announce chime (gentle audio cue before Genesis speaks).
2. ``tts.speak`` — renders and plays the actual message via Piper.

The announce service's own TTS rendering is unreliable (depends on
pipeline TTS config), so we split chime and speech into separate calls.

Registered with the outreach pipeline (time-gated to voice hours) for
proactive alerts, and held by the standalone server for shutdown
notifications.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

import httpx

from genesis.channels.base import ChannelAdapter

logger = logging.getLogger(__name__)


class VoiceChannelAdapter(ChannelAdapter):
    """Outbound voice via Home Assistant with pre-announce chime.

    Two-step: ``assist_satellite.announce`` (chime only) then
    ``tts.speak`` (actual message).  Falls back to ``tts.speak``
    alone when no satellite entity is configured.
    """

    # Delay between chime and TTS — long enough for the chime to play,
    # short enough to feel responsive.
    CHIME_DELAY_S = 1.5

    def __init__(
        self,
        *,
        ha_url: str | None = None,
        ha_token: str | None = None,
        satellite_entity: str = "assist_satellite.home_assistant_voice_0a2841_assist_satellite",
        tts_entity: str = "tts.piper",
        media_player_entity: str = "media_player.home_assistant_voice_0a2841",
    ) -> None:
        self._ha_url = ha_url.rstrip("/") if ha_url else None
        self._ha_token = ha_token
        self._satellite_entity = satellite_entity
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
        preannounce: bool = True,
        **kwargs,
    ) -> str:
        """Speak text through HA with an optional pre-announce chime.

        Two-step delivery:
        1. ``assist_satellite.announce`` with empty message (chime only)
        2. Brief delay for chime to play
        3. ``tts.speak`` for the actual message

        The announce service's built-in TTS rendering is unreliable
        (depends on pipeline config), so we only use it for the chime.

        Pass ``preannounce=False`` to skip the chime (e.g. for rapid
        follow-up messages where a chime would be redundant).
        """
        if not self._ha_url or not self._ha_token:
            logger.warning("Voice adapter: HA not configured, skipping TTS")
            return ""

        delivery_id = str(uuid.uuid4())
        headers = {
            "Authorization": f"Bearer {self._ha_token}",
            "Content-Type": "application/json",
        }

        # Step 1: Play pre-announce chime (if configured and requested)
        if preannounce and self._satellite_entity:
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    await client.post(
                        f"{self._ha_url}/api/services/assist_satellite/announce",
                        headers=headers,
                        json={
                            "entity_id": self._satellite_entity,
                            "message": "",
                        },
                    )
                    # Wait for chime to play before speaking
                    await asyncio.sleep(self.CHIME_DELAY_S)
            except Exception:
                logger.warning("Pre-announce chime failed", exc_info=True)

        # Step 2: Speak the actual message via tts.speak
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                await self._tts_speak(client, headers, text)
        except Exception:
            logger.error("Voice TTS delivery failed", exc_info=True)

        return delivery_id

    async def _tts_speak(
        self,
        client: httpx.AsyncClient,
        headers: dict,
        text: str,
    ) -> None:
        """Direct TTS via ``tts.speak`` — no pre-announce chime."""
        response = await client.post(
            f"{self._ha_url}/api/services/tts/speak",
            headers=headers,
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
