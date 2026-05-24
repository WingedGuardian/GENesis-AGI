"""Voice channel — reactive voice interface via Home Assistant.

Re-exports ``VoiceDeliveryHelper`` from the original ``voice`` module
(now at ``_delivery.py``) for backward compatibility.  Existing code
that does ``from genesis.channels.voice import VoiceDeliveryHelper``
continues to work.
"""

# Backward compatibility — VoiceDeliveryHelper lived in channels/voice.py
# before this became a package.  The original module is now _delivery.py.
from genesis.channels.voice._delivery import VoiceDeliveryHelper

__all__ = ["VoiceDeliveryHelper"]
