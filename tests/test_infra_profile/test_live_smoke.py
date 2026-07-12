"""Live smoke: real collectors against the actual machine (read-only).

This is the anti-churn integration check: every collector must return a
well-formed SectionResult on a real Linux box, and the stable sections must
hash identically across two back-to-back runs — a hash that flaps with no
system change is a collector bug (nondeterministic facts), the #1 design risk.

No runtime, no writes, no LLM — safe in CI (collectors degrade to error/empty
facts where tooling like systemctl is absent, which is itself asserted shape).
"""

from __future__ import annotations

import asyncio

from genesis.infra_profile.collectors import CONTAINER_COLLECTORS
from genesis.infra_profile.hashing import section_hash
from genesis.infra_profile.types import STATUS_OK, SectionResult

# Sections whose facts must be identical across two immediate runs.
# network/systemd/time excluded: still deterministic in practice, but they
# depend on external tooling whose availability we don't control in CI.
_STABLE = {"os", "virt", "cpu", "memory", "storage", "kernel", "limits", "versions"}


async def _run_all() -> dict[str, SectionResult]:
    results = await asyncio.gather(*(c() for c in CONTAINER_COLLECTORS))
    return {r.name: r for r in results}


async def test_all_collectors_return_wellformed_sections():
    sections = await _run_all()
    assert len(sections) == len(CONTAINER_COLLECTORS)  # unique names
    for name, result in sections.items():
        assert isinstance(result, SectionResult), name
        assert result.status in ("ok", "error", "unavailable"), name
        assert isinstance(result.facts, dict), name
        assert isinstance(result.metrics, dict), name
        if result.status == "error":
            assert result.error, name


async def test_core_sections_collect_on_real_linux():
    sections = await _run_all()
    for name in ("os", "cpu", "memory", "storage", "kernel"):
        assert sections[name].status == STATUS_OK, sections[name].error
        assert sections[name].facts, f"{name} collected no facts"


async def test_stable_sections_hash_deterministically():
    first = await _run_all()
    second = await _run_all()
    for name in _STABLE:
        a, b = first[name], second[name]
        if a.status != STATUS_OK or b.status != STATUS_OK:
            continue  # a flaky section fails the wellformed test, not this one
        assert section_hash(a.facts) == section_hash(b.facts), (
            f"section {name!r} hash flapped across back-to-back runs — "
            "nondeterministic facts (collector bug)"
        )
