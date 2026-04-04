"""Backward-compat shim — Genesis UI overlay blueprint.

The overlay blueprint and static assets have moved to:
    genesis.hosting.agent_zero.overlay

The data endpoints (/api/genesis/ui/*) have moved to:
    genesis.dashboard.routes.ui_data (on the genesis_dashboard blueprint)

This shim re-exports the overlay blueprint and register_injection so
existing callers (e.g., _00_genesis_bootstrap.py) continue to work.
"""

from genesis.hosting.agent_zero.overlay import blueprint, register_injection

__all__ = ["blueprint", "register_injection"]
