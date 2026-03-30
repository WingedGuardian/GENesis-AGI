"""External program adapter — wraps arbitrary programs as Genesis modules."""

from genesis.modules.external.adapter import ExternalProgramAdapter
from genesis.modules.external.config import HealthCheckConfig, IPCConfig, ProgramConfig

__all__ = [
    "ExternalProgramAdapter",
    "HealthCheckConfig",
    "IPCConfig",
    "ProgramConfig",
]
