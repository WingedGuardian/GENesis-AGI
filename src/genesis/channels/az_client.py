"""Backward-compat shim — AZClient has moved to genesis.hosting.agent_zero.client."""

from genesis.hosting.agent_zero.client import AZClient

__all__ = ["AZClient"]
