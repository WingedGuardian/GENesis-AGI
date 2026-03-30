"""Dashboard route modules — all routes are registered on the blueprint defined in _blueprint."""

from __future__ import annotations

from genesis.dashboard.routes import (
    activity,
    budget,
    config,
    ego,
    errors,
    events,
    health,
    modules,
    outreach,
    providers,
    recon,
    resolution,
    routing,
    services,
    state,
    vitals,
)

__all__ = [
    "activity",
    "budget",
    "config",
    "ego",
    "errors",
    "events",
    "health",
    "modules",
    "outreach",
    "providers",
    "recon",
    "resolution",
    "routing",
    "services",
    "state",
    "vitals",
]
