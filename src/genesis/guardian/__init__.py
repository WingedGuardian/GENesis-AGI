"""genesis.guardian — External host VM health monitor and recovery engine.

The Guardian runs on the host VM as a systemd timer, outside the Genesis
container's blast radius. It detects container failures via 5 independent
probes, diagnoses root cause via CC, and recovers with user approval.

Design doc: docs/architecture/genesis-v3-survivable-architecture.md
"""

from genesis.guardian.config import GuardianConfig, load_config

__all__ = [
    "GuardianConfig",
    "load_config",
]
