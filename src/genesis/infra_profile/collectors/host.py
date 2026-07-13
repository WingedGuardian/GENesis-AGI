"""Host/hypervisor-plane collection via the guardian SSH gateway.

PR1 ships the plane-unavailable contract only; the real gateway verb
(``host-profile``) and its client land in PR2. The section names and the
"not visible from this vantage" degradation are stable API from day one so
consumers (renderer, sentinel digest) never special-case the rollout.
"""

from __future__ import annotations

import logging

from genesis.infra_profile.types import PLANE_HOST, SectionResult

logger = logging.getLogger(__name__)

HOST_SECTIONS = ("host_system", "host_storage_pool", "host_virt")


async def collect_host(guardian_remote=None) -> tuple[bool, str | None, list[SectionResult]]:
    """Collect host-plane sections.

    Returns ``(available, reason, sections)``. With no guardian configured —
    or (PR1) the gateway verb not yet deployed — every host section is
    ``unavailable`` and ``available`` is False.
    """
    if guardian_remote is None:
        reason = "no guardian configured on this install"
        return (
            False,
            reason,
            [SectionResult.unavailable(name, reason, plane=PLANE_HOST) for name in HOST_SECTIONS],
        )

    # GROUNDWORK(infra-host-plane): PR2 wires GuardianRemote.host_profile()
    # (gateway verb `host-profile`) and splits its JSON blob into the
    # HOST_SECTIONS facts/metrics here.
    reason = "host-profile gateway verb not yet available"
    return (
        False,
        reason,
        [SectionResult.unavailable(name, reason, plane=PLANE_HOST) for name in HOST_SECTIONS],
    )
