"""health-mcp backward-compatibility shim.

The real implementation lives in ``genesis.mcp.health`` and still includes
the "dashboard" heartbeat configuration.
"""

from __future__ import annotations

import sys

from genesis.mcp import health as _health

sys.modules["genesis.mcp.health_mcp"] = _health
sys.modules[__name__] = _health

__all__ = _health.__all__
