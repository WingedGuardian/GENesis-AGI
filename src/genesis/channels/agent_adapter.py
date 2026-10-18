"""Local durable pull channel. No listener, sender, or peer network client."""

from genesis.channels.base import ChannelAdapter, ChannelNotConfiguredError
from genesis.peers.conversation import ConversationRefusal
from genesis.peers.outbox import Outbox, PeerEnvelope, registry_for_db


class AgentChannelAdapter(ChannelAdapter):
    def __init__(self, db):
        self.db = db

    async def start(self):
        pass

    async def stop(self):
        pass

    async def send_typing(self, channel_id):
        pass

    def get_capabilities(self):
        return {
            "markdown": False,
            "buttons": False,
            "reactions": False,
            "voice": False,
            "max_length": 256 * 1024,
            "pull": True,
        }

    async def get_engagement_signals(self, message_id):
        return {}  # collection is not owner judgment or earned authority

    async def send_message(self, channel_id, text, *, message_thread_id=None, **kwargs):
        envelope = kwargs.get("peer_envelope")
        if not isinstance(envelope, PeerEnvelope) or envelope.peer_id != channel_id:
            raise ChannelNotConfiguredError("Peer envelope required")
        try:
            return await Outbox(await registry_for_db(self.db)).put(envelope)
        except ConversationRefusal:
            raise ChannelNotConfiguredError("Peer message refused") from None
