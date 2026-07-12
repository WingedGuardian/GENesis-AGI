"""Infrastructure body-schema refresh job (daily 06:20 local).

Delegates to ``genesis.infra_profile.service.refresh_from_runtime`` — a full
collect → drift → annotate → render cycle. Function-scope imports are
intentional (import-cycle breaker + test patch seam); do not hoist them.
"""

from __future__ import annotations

import logging

from genesis.surplus.jobs._guard import record_failure, record_success

logger = logging.getLogger(__name__)

JOB_ID = "infra_profile_refresh"


async def run_infra_profile_refresh() -> None:
    """Refresh the infrastructure profile (facts + annotations + doc)."""
    try:
        from genesis.runtime import GenesisRuntime

        if GenesisRuntime.instance().paused:
            logger.debug("infra_profile refresh skipped (Genesis paused)")
            return
    except Exception:
        logger.warning(
            "Pause check failed — skipping infra_profile refresh",
            exc_info=True,
        )
        return

    try:
        from genesis.infra_profile.service import refresh_from_runtime

        profile = await refresh_from_runtime("scheduled")
        sections = profile.get("sections", {})
        ok = sum(1 for s in sections.values() if s.get("status") == "ok")
        logger.info(
            "infra_profile scheduled refresh: %d/%d sections ok",
            ok,
            len(sections),
        )
        record_success(JOB_ID)
    except Exception:
        logger.exception("infra_profile scheduled refresh failed")
        record_failure(JOB_ID)
