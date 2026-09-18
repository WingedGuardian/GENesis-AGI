"""Channel adapter base class — unified interface for all messaging platforms."""

from abc import ABC, abstractmethod


class ChannelNotConfiguredError(ValueError):
    """A channel was named that this install has no configuration to reach.

    PERMANENT, not transient — which is the whole reason it is its own type.
    `pipeline._deliver` catches a bare `Exception` from a send and DEFERS it
    (`pipeline.py`), and `resilience/outreach_recovery.py` then retries the
    deferred row 5 times over ~2.35h (`_BACKOFF_SCHEDULE` 60/300/900/3600/3600)
    rebuilding the SAME channel and target each time, before filing a
    `priority="high"` delivery-exhausted observation whose content embeds
    `deferred_id` — so `skip_if_duplicate` does not collapse them across rows.
    Meanwhile the drain treats FAILED as "transient, retried next cycle"
    (`outreach/scheduler.py`) until the 24h age-out.

    None of that can help: no amount of retrying creates a webhook. Raising a
    distinguishable type lets `_deliver` return a TERMINAL result instead, so a
    misconfiguration is reported once rather than becoming a day of retries and
    repeated high-priority alerts.

    Subclasses ValueError so any caller already handling ValueError from an
    adapter keeps working unchanged.
    """


class ChannelAdapter(ABC):
    """Base interface for all messaging channel adapters.

    Each adapter (Telegram, WhatsApp, Discord, etc.) implements this interface.
    The bridge service loads one or more adapters and manages their lifecycle.
    """

    @abstractmethod
    async def start(self) -> None:
        """Start the adapter (connect, begin polling/listening)."""

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully shut down the adapter."""

    @abstractmethod
    async def send_message(
        self,
        channel_id: str,
        text: str,
        *,
        message_thread_id: int | None = None,
        **kwargs,
    ) -> str:
        """Send a text message to a channel/user. Returns a delivery ID.

        Args:
            message_thread_id: Optional forum topic thread ID. Adapters that
                support forum topics (e.g. Telegram) will route the message
                to the specified topic. Others silently ignore it.
        """

    @abstractmethod
    async def send_typing(self, channel_id: str) -> None:
        """Show a typing/activity indicator."""

    @abstractmethod
    def get_capabilities(self) -> dict:
        """Return adapter capabilities.

        Expected keys:
            markdown (bool), buttons (bool), reactions (bool),
            voice (bool), max_length (int)
        """

    async def send_voice(
        self,
        channel_id: str,
        audio_bytes: bytes,
        reply_to_message_id: str | None = None,
    ) -> str:
        """Send a voice/audio message. Returns a delivery ID.

        Default raises NotImplementedError — channels that support voice
        override this. Check get_capabilities()["voice"] before calling.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support send_voice"
        )

    async def send_document(
        self,
        channel_id: str,
        document: bytes | str,
        *,
        caption: str | None = None,
        filename: str | None = None,
        message_thread_id: int | None = None,
    ) -> str:
        """Send a document/file. Returns a delivery ID.

        Default raises NotImplementedError — channels that support documents
        override this. Check get_capabilities().get("documents") before calling.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support send_document"
        )

    # GROUNDWORK(outreach-pipeline): Phase 8 engagement tracking.
    @abstractmethod
    async def get_engagement_signals(self, delivery_id: str) -> dict:
        """Check engagement signals for a sent message.

        Returns:
            {signal: "engaged"|"ignored"|"neutral", details: {...}}
        """
