"""Voice channel adapter — HA TTS with pre-announce chime for outbound speech.

Implements ``ChannelAdapter`` for voice output through Home Assistant.
Inbound voice (HA → Genesis) arrives via the Flask endpoint in
``dashboard/routes/voice_api.py``, not through this adapter.  This
adapter handles outbound only: Genesis speaking to the user proactively.

Two-step delivery:
1. ``media_player.play_media`` — plays a gentle chime audio file as an
   audio cue before Genesis speaks.
2. ``tts.speak`` — renders and plays the actual message via Piper.

The chime uses ``media_player.play_media`` (not ``assist_satellite.announce``)
because the announce service engages the voice assistant C++ state machine,
which can unpredictably reopen the microphone via its internal
``continue_conversation_`` flag.  The media player path stays entirely
in the media layer — no voice assistant involvement.

Registered with the outreach pipeline (time-gated to voice hours) for
proactive alerts, and held by the standalone server for shutdown
notifications.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid

import httpx

from genesis.channels.base import ChannelAdapter

logger = logging.getLogger(__name__)


class VoiceChannelAdapter(ChannelAdapter):
    """Outbound voice via Home Assistant with pre-announce chime.

    Two-step: ``media_player.play_media`` (chime audio file) then
    ``tts.speak`` (actual message).  Falls back to ``tts.speak``
    alone when no chime media ID is configured.
    """

    # Delay between chime and TTS — long enough for chime to finish,
    # short enough to feel responsive.  The default chime is ~0.45s.
    CHIME_DELAY_S = 1.0
    # Default chime audio file (uploaded to HA /config/www/)
    DEFAULT_CHIME_MEDIA_ID = "media-source://media_source/local/genesis_chime.wav"

    def __init__(
        self,
        *,
        ha_url: str | None = None,
        ha_token: str | None = None,
        chime_media_id: str = "",
        tts_entity: str = "",
        media_player_entity: str = "",
    ) -> None:
        self._ha_url = ha_url.rstrip("/") if ha_url else None
        self._ha_token = ha_token
        self._chime_media_id = chime_media_id or self.DEFAULT_CHIME_MEDIA_ID
        self._tts_entity = tts_entity or os.environ.get(
            "HA_TTS_ENTITY", "tts.piper",
        )
        self._media_player = media_player_entity or os.environ.get(
            "HA_MEDIA_PLAYER_ENTITY",
            "media_player.home_assistant_voice_0a2841",
        )

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

        Plays a chime audio file via ``media_player.play_media``, waits
        briefly, then speaks via ``tts.speak``.  The chime uses the
        media player directly (not ``assist_satellite.announce``) to
        avoid engaging the voice assistant state machine, which can
        unpredictably reopen the microphone.

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
                # Play pre-announce chime via media player (not voice assistant)
                if preannounce and self._chime_media_id:
                    try:
                        await client.post(
                            f"{self._ha_url}/api/services/media_player/play_media",
                            headers=headers,
                            json={
                                "entity_id": self._media_player,
                                "media_content_id": self._chime_media_id,
                                "media_content_type": "music",
                                "announce": True,
                            },
                        )
                        await asyncio.sleep(self.CHIME_DELAY_S)
                    except Exception:
                        logger.warning("Pre-announce chime failed", exc_info=True)

                # Speak the actual message
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
