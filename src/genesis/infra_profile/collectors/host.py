"""Host/hypervisor-plane collection via the guardian SSH gateway.

``GuardianRemote.host_profile()`` (gateway verb ``host-profile``) returns one
raw JSON blob gathered host-side by ``guardian/host_profile.py``; this module
owns the facts/metrics split — stable identity/topology goes in ``facts``
(hashed → drift detection + annotation pinning), volatile readings go in
``metrics`` (rendered, never hashed).

Degradation contract (stable API since PR1): with no guardian configured, a
pre-redeploy gateway (``denied``), an SSH failure, or a host-side error, every
host section is ``unavailable`` with a reason and ``available`` is False —
consumers (renderer, sentinel digest) never special-case the rollout.
"""

from __future__ import annotations

import logging
from typing import Any

from genesis.infra_profile.types import PLANE_HOST, SectionResult

logger = logging.getLogger(__name__)

HOST_SECTIONS = ("host_system", "host_storage_pool", "host_virt")


def _unavailable(reason: str) -> tuple[bool, str, list[SectionResult]]:
    return (
        False,
        reason,
        [SectionResult.unavailable(name, reason, plane=PLANE_HOST) for name in HOST_SECTIONS],
    )


def _split(name: str, raw: dict[str, Any], fact_keys: frozenset[str]) -> SectionResult:
    """Split one raw host sub-dict into a SectionResult by fact-key membership.

    A host-side per-section failure arrives as ``{"error": ...}`` — surfaced
    as an error section (prior facts/hash are preserved by the merge in
    ``service.py``, same contract as a failed container collector).
    """
    if not isinstance(raw, dict):
        return SectionResult.failed(name, f"unexpected payload: {raw!r}", plane=PLANE_HOST)
    # "error" is a RESERVED key at this boundary — its presence marks a
    # host-side section failure even alongside partial data (a partial dict
    # silently filed under metrics would render a failure as a healthy
    # reading — review 2026-07-13).
    if "error" in raw:
        return SectionResult.failed(name, str(raw["error"]), plane=PLANE_HOST)
    facts = {k: v for k, v in raw.items() if k in fact_keys}
    metrics = {k: v for k, v in raw.items() if k not in fact_keys}
    return SectionResult(name=name, plane=PLANE_HOST, facts=facts, metrics=metrics)


# Facts = slow-changing configuration/topology; everything else in the blob is
# a volatile reading. Deliberately allowlists (not blocklists) so a NEW field
# added host-side lands in metrics — never silently hash-churning facts across
# a version skew between host and container.
_SYSTEM_FACTS = frozenset(
    {
        "mem_total_kb",
        # Host swap total: topology (the container's pressure-relief valve —
        # its disappearance is exactly the 2026-07 wedge precondition, worth
        # a drift observation). swap_free_kb stays a metric.
        "swap_total_kb",
        "nproc",
        "kernel_release",
        "architecture",
        "hostname",
        "os_pretty_name",
    }
)
# Pool percentages/free-space are volatile. `detected` flipping IS a topology
# signal (measurement path broke or backend changed) — worth a drift
# observation, so it lives in facts. `detail` looks identity-ish but embeds
# the live data%/meta% numbers (verified live: "lvm vg0 data=61.19 meta=42.6")
# — hashing it would churn every refresh, so it is a metric.
_STORAGE_FACTS = frozenset({"detected", "pool_name"})
_VIRT_FACTS = frozenset(
    {
        "incus_client_version",
        "incus_server_version",
        "container_name",
        "container_limits",
        "detect_virt",
        "pve_version",
        "smartctl_present",
    }
)

_FACT_KEYS = {
    "host_system": _SYSTEM_FACTS,
    "host_storage_pool": _STORAGE_FACTS,
    "host_virt": _VIRT_FACTS,
}


async def collect_host(guardian_remote=None) -> tuple[bool, str | None, list[SectionResult]]:
    """Collect host-plane sections.

    Returns ``(available, reason, sections)``.
    """
    if guardian_remote is None:
        return _unavailable("no guardian configured on this install")

    try:
        blob = await guardian_remote.host_profile()
    except Exception as exc:  # noqa: BLE001 — plane degradation, never a raise
        logger.warning("infra_profile: host_profile call raised", exc_info=True)
        return _unavailable(f"host-profile call failed: {exc!r}")

    if not isinstance(blob, dict) or not blob.get("ok"):
        error = "no response"
        if isinstance(blob, dict):
            error = str(blob.get("error") or blob.get("raw") or "no response")
        # A pre-redeploy gateway answers its unknown-verb default — the JSON
        # sentinel {"ok": false, "error": "denied"}, which _as_json embeds RAW
        # in the error field. Match the QUOTED form only: an SSH auth failure
        # ('Permission denied (publickey)') also contains bare 'denied' and
        # must NOT be masked as a benign not-deployed state (review 2026-07-13).
        if '"denied"' in error:
            error = "host-profile gateway verb not deployed on host yet"
        return _unavailable(error[:200])

    sections = [_split(name, blob.get(name, {}), _FACT_KEYS[name]) for name in HOST_SECTIONS]
    return (True, None, sections)
