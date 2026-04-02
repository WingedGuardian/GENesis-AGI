"""CCReflectionBridge — reflection dispatch to CC background sessions.

Package split from reflection_bridge.py. All public names re-exported
for backward compatibility.
"""

from genesis.cc.reflection_bridge._bridge import CCReflectionBridge
from genesis.cc.reflection_bridge._prompts import _light_focus_area

__all__ = ["CCReflectionBridge", "_light_focus_area"]
