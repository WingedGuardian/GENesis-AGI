"""Bootstrap step: infrastructure body-schema boot refresh.

Spawns a delayed background refresh (non-blocking — the init step returns
immediately) so every boot leaves a current profile, annotations, and rendered
INFRASTRUCTURE.md under ~/.genesis/infrastructure/. The delay lets boot IO
quiet down before the collectors run.

Runs AFTER guardian_monitoring so ``rt._guardian_remote`` is wired when the
host plane lands (PR2); with no guardian the host sections degrade to
"not visible from this vantage".
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

_BOOT_DELAY_SECONDS = 120.0


async def init_infra_profile(rt) -> None:
    """Register the delayed boot refresh task."""
    from genesis.util.tasks import tracked_task

    async def _delayed_refresh() -> None:
        await asyncio.sleep(_BOOT_DELAY_SECONDS)
        # Function-scope import: runtime/init/__init__.py imports every step
        # eagerly, and the infra_profile graph should load only when used.
        from genesis.infra_profile.service import refresh_from_runtime

        try:
            profile = await refresh_from_runtime("boot")
            sections = profile.get("sections", {})
            ok = sum(1 for s in sections.values() if s.get("status") == "ok")
            logger.info(
                "infra_profile boot refresh complete: %d/%d sections ok",
                ok,
                len(sections),
            )
        except Exception:
            # refresh() contains its stages, but never let a surprise here
            # produce an unobserved task failure either.
            logger.error("infra_profile boot refresh failed", exc_info=True)

    tracked_task(_delayed_refresh(), name="infra-profile-boot-refresh")
    logger.info(
        "infra_profile boot refresh scheduled (+%ds)",
        int(_BOOT_DELAY_SECONDS),
    )
