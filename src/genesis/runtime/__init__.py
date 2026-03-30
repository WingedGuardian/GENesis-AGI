"""Runtime package — GenesisRuntime singleton and all init functions.

Backward compatibility: ``from genesis.runtime import GenesisRuntime`` continues
to work via re-export from _core.
"""

from genesis.runtime._core import GenesisRuntime

__all__ = ["GenesisRuntime"]
