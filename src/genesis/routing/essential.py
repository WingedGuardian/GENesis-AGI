"""Essential cloud-dependent call sites — the shared definition of which call
sites, if left with no working provider, constitute a genuine system
degradation.

This set is the single source of truth used by BOTH:
  * the degradation trigger (`circuit_breaker.compute_degradation_level`), and
  * the API-key alert severity (`observability.snapshots.api_keys`),
so "what counts as critical" is consistent across the health surfaces.

Only CLOUD-dependent essentials belong here. CC-native sites (``dispatch=cli``,
e.g. 5/6/7 reflections) and the embedding path (21) live on SEPARATE resilience
axes (``CCStatus`` / ``EmbeddingStatus`` in ``resilience.state``) and must NOT
be folded into the cloud-coverage computation.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.routing.types import RoutingConfig

logger = logging.getLogger(__name__)

# Cloud-dependent essential call sites. Each has a cross-vendor free chain
# today (verified 2026-06-19), so a paid-provider outage alone never uncovers
# them — which is exactly why a paid outage should NOT alarm.
ESSENTIAL_CLOUD_SITES: frozenset[str] = frozenset(
    {
        "3_micro_reflection",
        "4_light_reflection",
        "9_fact_extraction",
        "40_ego_focus_selection",
        # 8_ego_compaction removed 2026-07-13 — the ego went ephemeral (#26), so
        # CompactionEngine never routes; leaving it here mapped a dead chain whose
        # "uncovered" state could trigger a FALSE ESSENTIAL degradation for a site
        # that never runs. Its model_routing.yaml entry was removed in the same PR.
    }
)


def build_essential_provider_map(config: RoutingConfig) -> dict[str, list[str]] | None:
    """Map each present essential cloud site → its provider chain.

    Logs a warning (rather than silently skipping) if an expected essential id
    is absent from the routing config — a rename/removal must not quietly drop
    a site from the coverage check.

    Returns None when NO essential site is present at all, which puts the
    registry on the legacy provider-count check. When sites are present but
    every one is blocked, the map is empty but NOT None: coverage mode stays
    on, with nothing uncovered, rather than letting the legacy heuristic raise
    a degradation over sites the configuration itself turned off.
    """
    result: dict[str, list[str]] = {}
    present = 0
    call_sites = getattr(config, "call_sites", {}) or {}
    for site in ESSENTIAL_CLOUD_SITES:
        cs = call_sites.get(site)
        present += cs is not None
        # A site with NO providers is BLOCKED by configuration (an API-only
        # site whose providers are all disabled, or one a rejected overlay
        # blocked), not uncovered by an outage. Counting it as uncovered would
        # raise system-wide ESSENTIAL degradation and shed every other routed
        # site over one config line, so it is skipped the way a missing site is.
        if cs is None or not cs.chain:
            logger.warning(
                "Essential cloud site %r %s — degradation coverage check will skip it",
                site,
                "not found in routing config" if cs is None else "has no providers (blocked)",
            )
            continue
        result[site] = list(cs.chain)
    if not present:
        # All essential ids absent → the registry would silently fall back to
        # legacy provider-count degradation, re-introducing the false-alarm this
        # set exists to prevent. Make that loud, not silent.
        logger.error(
            "build_essential_provider_map: NONE of the %d expected essential "
            "sites were found in routing config — coverage-based degradation is "
            "DISABLED (legacy provider-count fallback). Essential site ids were "
            "likely renamed; update ESSENTIAL_CLOUD_SITES.",
            len(ESSENTIAL_CLOUD_SITES),
        )
        return None
    return result
