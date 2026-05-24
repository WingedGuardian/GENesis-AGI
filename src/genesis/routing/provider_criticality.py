"""Dynamic provider criticality — derived from routing config chains.

Replaces the static PROVIDER_TIERS registry. Criticality is computed at the
provider_type level (one API key covers all providers of that type) by
reverse-indexing which call sites reference each provider.

Tiers:
  sole     — provider type is the ONLY option in ≥1 call site chain
  systemic — provider type appears in 10+ call site chains
  active   — provider type appears in 1-9 call site chains
  dormant  — provider type not referenced by any call site chain
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.routing.types import RoutingConfig

logger = logging.getLogger(__name__)

_SYSTEMIC_THRESHOLD = 10


def call_sites_for_provider_type(
    config: RoutingConfig, provider_type: str,
) -> list[str]:
    """Return call site IDs that include any provider of this type.

    CC-dispatched call sites (dispatch="cli") are excluded — CC availability
    is a different monitoring domain from API key health.
    """
    type_providers = {
        name for name, p in config.providers.items()
        if p.provider_type == provider_type
    }
    if not type_providers:
        return []
    return [
        cs_id
        for cs_id, cs in config.call_sites.items()
        if (cs.dispatch or "dual") != "cli"
        and any(p in type_providers for p in cs.chain)
    ]


def derive_criticality(config: RoutingConfig) -> dict[str, dict]:
    """Compute criticality per provider_type from chain usage.

    Returns ``{provider_type: info}`` where *info* is::

        {
            "chain_count": int,
            "chain_usage": list[str],   # call site IDs
            "criticality": str,         # sole | systemic | active | dormant
            "is_free": bool,
            "sole_sites": list[str],    # sites where this type is the only provider
        }
    """
    # Group providers by type
    types: dict[str, dict] = {}
    for name, p in config.providers.items():
        info = types.setdefault(p.provider_type, {"names": set(), "is_free": True})
        info["names"].add(name)
        if not p.is_free:
            info["is_free"] = False

    # Include disabled providers so dormant keys still show up
    for name, ptype in getattr(config, "disabled_providers", {}).items():
        info = types.setdefault(ptype, {"names": set(), "is_free": True})
        info["names"].add(name)

    result: dict[str, dict] = {}
    for ptype, info in types.items():
        sites = call_sites_for_provider_type(config, ptype)

        # Detect sole-provider chains: call sites where the ONLY providers
        # in the chain all belong to this type (no alternatives)
        sole_sites: list[str] = []
        for cs_id in sites:
            cs = config.call_sites.get(cs_id)
            if not cs:
                continue
            active_chain = [p for p in cs.chain if p in config.providers]
            if active_chain and all(
                config.providers[p].provider_type == ptype
                for p in active_chain
            ):
                sole_sites.append(cs_id)

        count = len(sites)
        if sole_sites:
            crit = "sole"
        elif count >= _SYSTEMIC_THRESHOLD:
            crit = "systemic"
        elif count >= 1:
            crit = "active"
        else:
            crit = "dormant"

        result[ptype] = {
            "chain_count": count,
            "chain_usage": sites,
            "criticality": crit,
            "is_free": info["is_free"],
            "sole_sites": sole_sites,
        }

    return result
