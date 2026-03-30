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
        """
        old_sites = set(self.config.call_sites)
        new_sites = set(new_config.call_sites)
        self.config = new_config

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

    async def route_call(
        self,
        call_site_id: str,
        messages: list[dict],
        *,
        budget_override: bool = False,
        **kwargs,
    ) -> RoutingResult:
        """Route a call through the provider chain for the given call site."""
        # 1. Check call site exists
        if call_site_id not in self.config.call_sites:
            return RoutingResult(
                success=False,
                call_site_id=call_site_id,
                error=f"Unknown call site: {call_site_id}",
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

        # 3. Filter chain
        chain = self._filter_chain(site)
        if not chain:
            return RoutingResult(
                success=False,
                call_site_id=call_site_id,
                error="No providers available in chain after filtering",
            )

        # 4. Check budget once (shared across providers)
        budget_status = BudgetStatus.UNDER_LIMIT
        if not budget_override:
            budget_status = await self.cost_tracker.check_budget()

        attempts = 0
        first_provider = chain[0]
        failed_providers: list[str] = []

        for provider_name in chain:
            provider_cfg = self.config.providers[provider_name]

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
                # Record cost
                if result.cost_usd > 0:
                    try:
                        await self.cost_tracker.record(call_site_id, provider_name, result)
                    except Exception:
                        logger.warning(
                            "Cost recording failed for %s/%s ($%.4f)",
                            call_site_id, provider_name, result.cost_usd, exc_info=True,
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

        dead_lettered = False
        if self._dead_letter:
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
    _PURPOSE_SITES: dict[str, str] = {
        "bookmark_enrichment": "33_skill_refiner",
        "email_triage": "email_triage",
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

        Currently only tier="free" is supported. Other tiers log a warning
        and fall through to the free-tier site.
        """
        if purpose and purpose in self._PURPOSE_SITES:
            call_site_id = self._PURPOSE_SITES[purpose]
        else:
            if tier != "free":
                logger.warning("route() called with tier=%r but only 'free' is supported; using free-tier site", tier)
            call_site_id = self._FREE_TIER_SITE

        result = await self.route_call(
            call_site_id=call_site_id,
            messages=[{"role": "user", "content": prompt}],
        )
        if result.success:
            return result.content or ""
        raise RuntimeError(f"Routing failed for {call_site_id}: {result.error}")
