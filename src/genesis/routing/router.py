"""Core router — walks fallback chains with circuit breakers, budgets, and degradation."""

from __future__ import annotations

import asyncio
import logging
import time

from genesis.observability.call_site_recorder import record_last_run
from genesis.observability.events import GenesisEventBus
from genesis.observability.provider_activity import ProviderActivityTracker
from genesis.observability.types import Severity, Subsystem
from genesis.routing.circuit_breaker import CircuitBreakerRegistry
from genesis.routing.cost_tracker import CostTracker
from genesis.routing.dead_letter import DeadLetterQueue
from genesis.routing.degradation import DegradationTracker
from genesis.routing.rate_gate import RateGateRegistry
from genesis.routing.retry import classify_error, compute_delay
from genesis.routing.types import (
    BudgetStatus,
    CallDelegate,
    CallResult,
    ErrorCategory,
    RoutingConfig,
    RoutingResult,
)

logger = logging.getLogger(__name__)

# Sentinel error prefix returned by route_call() when the call_site_id does
# not exist in the current routing config. The dead-letter replay loop uses
# this to distinguish "stale call_site_id after config reload" (expire)
# from ordinary provider-exhaustion failures (retry). Tested to stay in sync
# between router and dead_letter — changing this string requires updating
# dead_letter.redispatch() and the matching test.
UNKNOWN_CALL_SITE_ERROR_PREFIX = "Unknown call site:"


class Router:
    """Routes LLM calls through provider fallback chains with resilience."""

    def __init__(
        self,
        config: RoutingConfig,
        breakers: CircuitBreakerRegistry,
        cost_tracker: CostTracker,
        degradation: DegradationTracker,
        delegate: CallDelegate,
        event_bus: GenesisEventBus | None = None,
        dead_letter: DeadLetterQueue | None = None,
    ) -> None:
        self.config = config
        self.breakers = breakers
        self.cost_tracker = cost_tracker
        self.degradation = degradation
        self.delegate = delegate
        self._event_bus = event_bus
        self._dead_letter = dead_letter
        self._activity_tracker: ProviderActivityTracker | None = None
        self._rate_gates = self._build_rate_gates(config)

    @staticmethod
    def _build_rate_gates(config: RoutingConfig) -> RateGateRegistry:
        """Create rate gates for providers with RPM limits."""
        registry = RateGateRegistry()
        for name, provider in config.providers.items():
            if provider.rpm_limit is not None and provider.rpm_limit > 0:
                registry.register(name, provider.rpm_limit)
        return registry

    def set_activity_tracker(self, tracker: ProviderActivityTracker) -> None:
        """Inject activity tracker for per-provider call metrics."""
        self._activity_tracker = tracker

    def reload_config(self, new_config: RoutingConfig) -> None:
        """Hot-swap routing config. Preserves circuit breaker state.

        Safe for asyncio — single-threaded, reference swap is atomic.
        Note: in-flight route_call() may hold references to old config's
        call sites but looks up providers from self.config. Ensure removed
        providers are not referenced by in-flight calls (practically safe
        since provider removal is rare and asyncio is cooperative).

        This method is intentionally synchronous to preserve the existing
        Flask sync-route contract. Callers that want the proactive DLQ
        orphan scan (which complements the reactive call_site_id cleanup
        inside ``DeadLetterQueue.redispatch``) should call
        ``scan_dlq_orphans_after_reload()`` immediately after this.
        """
        old_sites = set(self.config.call_sites)
        new_sites = set(new_config.call_sites)
        self.config = new_config
        self._rate_gates = self._build_rate_gates(new_config)

        # Update breaker registry so get() can create breakers for new providers
        self.breakers.update_providers(new_config.providers)

        # Ensure circuit breakers exist for all providers
        for name in new_config.providers:
            self.breakers.get(name)  # get-or-create

        added = new_sites - old_sites
        removed = old_sites - new_sites
        if added or removed:
            logger.info(
                "Routing config reloaded: +%d/-%d call sites", len(added), len(removed),
            )
        else:
            logger.info("Routing config reloaded: %d call sites", len(new_sites))

        # Validate hardcoded call site IDs used by the route() wrapper
        for label, site_id in [
            ("_FREE_TIER_SITE", self._FREE_TIER_SITE),
            *((f"_PURPOSE_SITES[{k!r}]", v) for k, v in self._PURPOSE_SITES.items()),
        ]:
            if site_id not in new_config.call_sites:
                logger.error(
                    "route() wrapper references call site %r (%s) "
                    "which is missing from routing config",
                    site_id, label,
                )

    async def scan_dlq_orphans_after_reload(self) -> int:
        """Proactively expire DLQ items whose target_provider was removed.

        Thin async wrapper around ``DeadLetterQueue.scan_orphans_by_provider``
        that scopes the scan to the current (post-reload) provider set.
        Intended to be called immediately after ``reload_config()`` from
        async contexts (dashboard async routes, scheduled jobs).

        Complements the reactive call_site_id cleanup that already runs
        inside ``DeadLetterQueue.redispatch``: that one catches items
        whose *call_site_id* was renamed/removed; this one catches items
        whose *target_provider* was removed. Different orphan key,
        different trigger — both are needed.

        Returns:
            Count of orphans expired, or 0 if no DLQ is wired.
        """
        if self._dead_letter is None:
            return 0
        return await self._dead_letter.scan_orphans_by_provider(
            self.config.providers.keys(),
        )

    async def route_call(
        self,
        call_site_id: str,
        messages: list[dict],
        *,
        budget_override: bool = False,
        suppress_dead_letter: bool = False,
        chain_offset: int = 0,
        **kwargs,
    ) -> RoutingResult:
        """Route a call through the provider chain for the given call site."""
        # 1. Check call site exists
        if call_site_id not in self.config.call_sites:
            return RoutingResult(
                success=False,
                call_site_id=call_site_id,
                error=f"{UNKNOWN_CALL_SITE_ERROR_PREFIX} {call_site_id}",
            )

        # 2. Check degradation
        if self.degradation.should_skip(call_site_id):
            return RoutingResult(
                success=False,
                call_site_id=call_site_id,
                error=f"Degradation level {self.degradation.current_level} skips {call_site_id}",
            )

        site = self.config.call_sites[call_site_id]
        policy = self.config.retry_profiles.get(site.retry_profile)
        if policy is None:
            policy = self.config.retry_profiles["default"]

        # 3. Filter chain (and rotate for parallelization)
        chain = self._filter_chain(site)
        if not chain:
            return RoutingResult(
                success=False,
                call_site_id=call_site_id,
                error="No providers available in chain after filtering",
            )
        if chain_offset:
            n = chain_offset % len(chain)
            chain = chain[n:] + chain[:n]

        # 4. Check budget once (shared across providers)
        budget_status = BudgetStatus.UNDER_LIMIT
        if not budget_override:
            budget_status = await self.cost_tracker.check_budget()

        attempts = 0
        first_provider = chain[0]
        failed_providers: list[str] = []

        for provider_name in chain:
            provider_cfg = self.config.providers[provider_name]

            # Skip providers with no API key — treat as down-by-config.
            # Same effect as a tripped CB: no LiteLLM call, no failure
            # record, no CB trip. Partial API-key configuration is the
            # normal install state on freshly-installed systems.
            if not provider_cfg.has_api_key:
                failed_providers.append(provider_name)
                continue

            # Skip if circuit breaker is open
            cb = self.breakers.get(provider_name)
            if not cb.is_available():
                failed_providers.append(provider_name)
                continue

            # Skip paid providers if budget exceeded (unless override)
            if (
                not provider_cfg.is_free
                and not budget_override
                and budget_status == BudgetStatus.EXCEEDED
            ):
                failed_providers.append(provider_name)
                continue

            # Rate gate — pace requests per provider RPM limit
            await self._rate_gates.acquire(provider_name)

            # Try with retry (timed for activity tracking)
            t0 = time.monotonic()
            result = await self._try_with_retry(
                provider_name, provider_cfg.model_id, messages, policy, **kwargs,
            )
            latency_ms = (time.monotonic() - t0) * 1000
            attempts += 1

            # Record to activity tracker (fire-and-forget, never breaks caller)
            if self._activity_tracker:
                try:
                    self._activity_tracker.record(
                        f"llm.{provider_name}",
                        latency_ms=latency_ms,
                        success=result.success,
                    )
                except Exception:
                    logger.warning(
                        "Activity tracker record failed for llm.%s",
                        provider_name, exc_info=True,
                    )

            if result.success:
                # Record cost — always record when cost is known positive,
                # AND when cost is unknown (so blind spending is visible).
                if result.cost_usd > 0 or not result.cost_known:
                    try:
                        await self.cost_tracker.record(
                            call_site_id, provider_name, result,
                            cost_known=result.cost_known,
                        )
                    except Exception:
                        logger.warning(
                            "Cost recording failed for %s/%s ($%.4f)",
                            call_site_id, provider_name, result.cost_usd, exc_info=True,
                        )
                # Alert on unknown-cost calls with real output
                if not result.cost_known and result.output_tokens > 0 and self._event_bus:
                    await self._event_bus.emit(
                        Subsystem.ROUTING, Severity.WARNING,
                        "provider.cost_unknown",
                        f"Call site {call_site_id}: {provider_name} returned "
                        f"{result.output_tokens} tokens but cost is unknown",
                        call_site=call_site_id,
                        provider=provider_name,
                    )
                cb.record_success()

                # Emit fallback event when primary provider was skipped
                if provider_name != first_provider and self._event_bus:
                    await self._event_bus.emit(
                        Subsystem.ROUTING, Severity.WARNING,
                        "provider.fallback",
                        f"Call site {call_site_id}: primary '{first_provider}' failed, "
                        f"using fallback '{provider_name}' (attempt {attempts}/{len(chain)})",
                        call_site=call_site_id,
                        provider=provider_name,
                        failed_providers=failed_providers,
                    )

                # Record last run for neural monitor
                if self.cost_tracker and self.cost_tracker.db:
                    await record_last_run(
                        self.cost_tracker.db, call_site_id,
                        provider=provider_name, model_id=provider_cfg.model_id,
                        response_text=result.content,
                        input_tokens=result.input_tokens,
                        output_tokens=result.output_tokens,
                    )


                return RoutingResult(
                    success=True,
                    call_site_id=call_site_id,
                    provider_used=provider_name,
                    model_id=provider_cfg.model_id,
                    content=result.content,
                    attempts=attempts,
                    fallback_used=(provider_name != first_provider),
                    failed_providers=tuple(failed_providers),
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    cost_usd=result.cost_usd,
                )
            else:
                # Record CB failure
                failed_providers.append(provider_name)
                category = classify_error(result.status_code, result.error or "")
                tripped = cb.record_failure(category)
                if tripped and self._event_bus:
                    await self._event_bus.emit(
                        Subsystem.ROUTING, Severity.WARNING,
                        "breaker.tripped",
                        f"Circuit breaker tripped for {provider_name}",
                        provider=provider_name,
                        call_site=call_site_id,
                    )

        # All exhausted
        if self._event_bus:
            await self._event_bus.emit(
                Subsystem.ROUTING, Severity.ERROR,
                "all_exhausted",
                f"All providers exhausted for {call_site_id}",
                call_site=call_site_id,
                attempts=attempts,
            )

        # Record failure for neural monitor visibility
        if self.cost_tracker and self.cost_tracker.db:
            await record_last_run(
                self.cost_tracker.db, call_site_id,
                provider="(exhausted)", model_id="",
                response_text=None, success=False,
            )

        dead_lettered = False
        if self._dead_letter and not suppress_dead_letter:
            try:
                await self._dead_letter.enqueue(
                    operation_type=f"chain_exhausted:{call_site_id}",
                    payload={
                        "call_site_id": call_site_id,
                        "messages": messages,
                    },
                    target_provider="all",
                    failure_reason="All providers exhausted",
                )
                dead_lettered = True
            except Exception:
                logger.error(
                    "Dead letter enqueue failed for chain exhaustion: %s",
                    call_site_id, exc_info=True,
                )

        return RoutingResult(
            success=False,
            call_site_id=call_site_id,
            attempts=attempts,
            error="All providers exhausted",
            dead_lettered=dead_lettered,
        )

    def _filter_chain(self, site) -> list[str]:
        """Filter chain based on never_pays constraint."""
        if site.never_pays:
            return [p for p in site.chain if self.config.providers[p].is_free]
        return list(site.chain)

    async def _try_with_retry(
        self, provider: str, model_id: str, messages: list[dict], policy, **kwargs,
    ) -> CallResult:
        """Try calling a provider with retries. Returns last result."""
        last_result = CallResult(success=False, error="no attempts made")
        max_attempts = policy.max_retries + 1

        for attempt in range(max_attempts):
            result = await self.delegate.call(provider, model_id, messages, **kwargs)
            if result.success:
                return result

            last_result = result
            category = classify_error(result.status_code, result.error or "")

            # Permanent errors: stop retrying
            if category == ErrorCategory.PERMANENT:
                return result

            # Transient/degraded: retry with delay (skip delay on last attempt)
            if attempt < max_attempts - 1:
                delay = compute_delay(policy, attempt)
                if delay > 0:
                    await asyncio.sleep(delay)

        return last_result

    # ------------------------------------------------------------------
    # Convenience wrapper used by modules, triage, and enrichment code.
    # ------------------------------------------------------------------

    # Call-site used when callers pass tier="free" without a specific site.
    _FREE_TIER_SITE = "12_surplus_brainstorm"

    # Map purpose strings to specific call sites.
    # Aliases: bookmark_enrichment routes through the 33_skill_refiner chain
    # (no own chain in model_routing.yaml); outreach_email_triage is the
    # canonical ID (renamed from email_triage 2026-05-10).
    _PURPOSE_SITES: dict[str, str] = {
        "bookmark_enrichment": "33_skill_refiner",
        "outreach_email_triage": "outreach_email_triage",
    }

    async def route(
        self,
        prompt: str = "",
        *,
        tier: str = "free",
        purpose: str = "",
    ) -> str:
        """Convenience wrapper: send a prompt through an appropriate call site.

        Returns the LLM response text. Raises RuntimeError on failure.
        Used by modules (crypto_ops, prediction_markets, generalization),
        pipeline triage, and bookmark enrichment.

        Currently only tier="free" is supported. Raises ValueError for
        other tiers.
        """
        if purpose and purpose in self._PURPOSE_SITES:
            call_site_id = self._PURPOSE_SITES[purpose]
        else:
            if tier != "free":
                raise ValueError(f"route() only supports tier='free', got {tier!r}")
            call_site_id = self._FREE_TIER_SITE

        result = await self.route_call(
            call_site_id=call_site_id,
            messages=[{"role": "user", "content": prompt}],
        )
        if result.success:
            return result.content or ""
        raise RuntimeError(f"Routing failed for {call_site_id}: {result.error}")
