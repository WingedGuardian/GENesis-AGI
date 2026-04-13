"""Pipeline orchestrator — ties collection, triage, and storage together."""

from __future__ import annotations

import logging
from typing import Any

from genesis.pipeline.collectors import CollectorRegistry
from genesis.pipeline.profiles import ProfileLoader
from genesis.pipeline.triage import TriageFilter
from genesis.pipeline.types import PipelineRunResult, ResearchSignal

logger = logging.getLogger(__name__)


class PipelineOrchestrator:
    """Orchestrates the tiered research pipeline."""

    def __init__(
        self,
        *,
        profile_loader: ProfileLoader,
        collector_registry: CollectorRegistry,
        triage_filter: TriageFilter,
        memory_store: Any = None,
        router: Any = None,
        event_bus: Any = None,
        module_dispatcher: Any = None,
    ):
        self._profiles = profile_loader
        self._collectors = collector_registry
        self._triage = triage_filter
        self._memory_store = memory_store
        self._router = router
        self._event_bus = event_bus
        self._module_dispatcher = module_dispatcher

    async def run_collection(self, profile_name: str) -> PipelineRunResult:
        """Run Tier 0 collection for a specific profile."""
        profile = self._profiles.get(profile_name)
        if profile is None:
            return PipelineRunResult(
                profile_name=profile_name,
                errors=[f"Profile not found: {profile_name}"],
            )

        result = PipelineRunResult(profile_name=profile_name)
        all_signals: list[ResearchSignal] = []

        for source_cfg in profile.sources:
            try:
                collector = self._collectors.create(source_cfg.type, profile.name)
                collected = await collector.collect(source_cfg.queries, max_results=20)
                all_signals.extend(collected.signals)
                result.errors.extend(collected.errors)
            except Exception as e:
                result.errors.append(f"Collector {source_cfg.name} failed: {e}")

        result.tier0_collected = len(all_signals)
        return result

    async def run_triage(
        self, signals: list[ResearchSignal], profile_name: str
    ) -> list[ResearchSignal]:
        """Run Tier 1 triage on collected signals."""
        profile = self._profiles.get(profile_name)
        if profile is None:
            return []
        return await self._triage.triage(signals, profile, router=self._router)

    async def run_cycle(self, profile_name: str) -> PipelineRunResult:
        """Run a full Tier 0 + Tier 1 cycle for a profile."""
        profile = self._profiles.get(profile_name)
        if profile is None:
            return PipelineRunResult(
                profile_name=profile_name,
                errors=[f"Profile not found: {profile_name}"],
            )

        result = PipelineRunResult(profile_name=profile_name)

        # Tier 0: Collect
        all_signals: list[ResearchSignal] = []
        for source_cfg in profile.sources:
            try:
                collector = self._collectors.create(source_cfg.type, profile.name)
                collected = await collector.collect(source_cfg.queries, max_results=20)
                all_signals.extend(collected.signals)
                result.errors.extend(collected.errors)
            except Exception as e:
                result.errors.append(f"Collector {source_cfg.name} failed: {e}")
        result.tier0_collected = len(all_signals)

        # Tier 1: Triage
        surviving = await self._triage.triage(all_signals, profile, router=self._router)
        result.tier1_survived = len(surviving)
        result.discarded = result.tier0_collected - result.tier1_survived

        # Store surviving signals
        if self._memory_store and surviving:
            for signal in surviving:
                try:
                    await self._memory_store.store(
                        signal.content,
                        f"pipeline:{profile.name}",
                        memory_type="knowledge",
                        collection="knowledge_base",  # Explicit — module signal data
                        tags=signal.tags + [f"tier:{signal.tier.name}", f"profile:{profile.name}"],
                        confidence=signal.relevance_score * 0.7,
                        source_pipeline="recon",
                    )
                except Exception:
                    logger.warning("Failed to store signal %s", signal.id, exc_info=True)

        # Dispatch surviving signals to matching capability module
        if self._module_dispatcher and surviving:
            try:
                proposal = await self._module_dispatcher.dispatch(
                    profile_name, surviving, router=self._router,
                )
                if proposal:
                    logger.info(
                        "Module proposal from %s: %s",
                        profile_name,
                        proposal.get("type", "unknown"),
                    )
                    if self._event_bus:
                        await self._event_bus.emit(
                            subsystem="modules",
                            event_type="action_proposal",
                            message=f"Module proposal: {proposal.get('type', 'unknown')}",
                            data=proposal,
                        )
            except Exception:
                logger.warning("Module dispatch failed for %s", profile_name, exc_info=True)

        return result
