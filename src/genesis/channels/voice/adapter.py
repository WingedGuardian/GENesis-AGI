"""Voice channel adapter — HA satellite announce for outbound speech.

Implements ``ChannelAdapter`` for voice output through Home Assistant.
Inbound voice (HA → Genesis) arrives via the Flask endpoint in
``dashboard/routes/voice_api.py``, not through this adapter.  This
adapter handles outbound only: Genesis speaking to the user proactively.

Uses ``assist_satellite.announce`` for delivery — this plays a short
pre-announce chime before the TTS message, giving the user a gentle
audio cue that Genesis is about to speak.  Falls back to ``tts.speak``
if the satellite entity is not configured.

Registered with the outreach pipeline (time-gated to voice hours) for
proactive alerts, and held by the standalone server for shutdown
notifications.
"""

from __future__ import annotations

import logging
import uuid

import httpx

from genesis.channels.base import ChannelAdapter

logger = logging.getLogger(__name__)


class VoiceChannelAdapter(ChannelAdapter):
    """Outbound voice via Home Assistant satellite announce.

    Primary path: ``assist_satellite.announce`` — plays a pre-announce
    chime then speaks TTS.  Falls back to ``tts.speak`` when no
    satellite entity is configured.
    """

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
        """Speak text through HA with a pre-announce chime.

        Uses ``assist_satellite.announce`` which plays a gentle chime
        before TTS.  Falls back to ``tts.speak`` if announce fails or
        the satellite entity is not configured.

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

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                if self._satellite_entity:
                    # Primary: assist_satellite.announce (chime + TTS)
                    payload: dict = {
                        "entity_id": self._satellite_entity,
                        "message": text,
                        "preannounce": preannounce,
                    }
                    response = await client.post(
                        f"{self._ha_url}/api/services/assist_satellite/announce",
                        headers=headers,
                        json=payload,
                    )
                    response.raise_for_status()
                    logger.info(
                        "Voice announce delivered: %d chars → %s (chime=%s)",
                        len(text), self._satellite_entity, preannounce,
                    )
                else:
                    # Fallback: tts.speak (no chime)
                    await self._tts_speak(client, headers, text)
        except Exception:
            logger.warning("Voice announce failed, trying tts.speak fallback", exc_info=True)
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    await self._tts_speak(client, headers, text)
            except Exception:
                logger.error("Voice TTS fallback also failed", exc_info=True)

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
            "Voice TTS (fallback) delivered: %d chars → %s",
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
