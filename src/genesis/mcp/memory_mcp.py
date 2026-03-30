"""memory-mcp — backward-compatibility re-export shim.

The real implementation lives in genesis.mcp.memory.
This module IS the memory module via sys.modules forwarding.
"""

from __future__ import annotations

import sys

# Import the memory package
from genesis.mcp import memory as _memory

# Make this module's identity the same as memory
# This allows tests that do memory_mcp._store = mock to affect memory._store
sys.modules["genesis.mcp.memory_mcp"] = _memory
sys.modules[__name__] = _memory

# Re-export for static analysis tools
__all__ = _memory.__all__
