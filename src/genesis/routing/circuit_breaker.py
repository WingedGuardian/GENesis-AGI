"""Circuit breaker for provider health tracking."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from genesis.routing.types import (
    DegradationLevel,
    ErrorCategory,
    ProviderConfig,
    ProviderState,
)
from genesis.util.atomic import atomic_write_text

logger = logging.getLogger(__name__)

_STATE_FILE = Path.home() / ".genesis" / "circuit_breaker_state.json"


_MAX_OPEN_S = 1800  # 30-minute cap on escalating backoff
_MAX_QUOTA_OPEN_S = 14400  # 4-hour cap for quota/billing exhaustion


class CircuitBreaker:
    """Per-provider circuit breaker with CLOSED → OPEN → HALF_OPEN state machine.

    Open duration uses escalating backoff: each consecutive trip doubles the
    open period (base_duration * 2^(trip_count-1)), capped at _MAX_OPEN_S.
    For quota exhaustion errors, the cap is _MAX_QUOTA_OPEN_S (4 hours)
    to avoid wasting probe calls on keys that won't recover for hours.
    Resets to base duration when the provider recovers to CLOSED.
    """

    def __init__(
        self,
        provider: ProviderConfig,
        failure_threshold: int = 3,
        open_duration_s: int = 120,
        success_threshold: int = 2,
        probe_success_threshold: int = 3,
        clock: object = None,
        on_state_change: object = None,
        on_recovery: object = None,
    ) -> None:
        self._provider = provider
        self._failure_threshold = failure_threshold
        self._open_duration_s = open_duration_s
        self._success_threshold = success_threshold
        # Stricter than success_threshold: a free /v1/models probe (used by
        # record_probe_success) is weaker evidence than a real completion, so a
        # HALF_OPEN provider needs MORE consecutive clean probes to heal.
        self._probe_success_threshold = probe_success_threshold
        self._clock = clock or time.monotonic
        self._on_state_change = on_state_change
        self._on_recovery = on_recovery

        self._state = ProviderState.CLOSED
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._opened_at: float = 0.0
        self._trip_count: int = 0
        self._last_failure_category: ErrorCategory | None = None

    @property
    def consecutive_failures(self) -> int:
        """Current consecutive failure count."""
        return self._consecutive_failures

    @property
    def trip_count(self) -> int:
        """Number of consecutive trips without recovery."""
        return self._trip_count

    @property
    def last_failure_category(self) -> ErrorCategory | None:
        """Category of the most recent failure that tripped the breaker."""
        return self._last_failure_category

    def _effective_open_duration(self) -> float:
        """Open duration with escalating backoff.

        First trip uses base duration; each subsequent trip doubles it.
        Capped at _MAX_QUOTA_OPEN_S (4h) for quota exhaustion, _MAX_OPEN_S (30m) otherwise.
        """
        exponent = max(0, self._trip_count - 1)
        cap = _MAX_QUOTA_OPEN_S if self._last_failure_category == ErrorCategory.QUOTA_EXHAUSTED else _MAX_OPEN_S
        return min(self._open_duration_s * (2 ** exponent), cap)

    @property
    def state(self) -> ProviderState:
        """Return current state, auto-transitioning OPEN → HALF_OPEN when expired."""
        if (
            self._state == ProviderState.OPEN
            and self._clock() - self._opened_at >= self._effective_open_duration()
        ):
            self._state = ProviderState.HALF_OPEN
            self._consecutive_successes = 0
        return self._state

    def is_available(self) -> bool:
        """True if the provider can accept requests (not OPEN)."""
        return self.state != ProviderState.OPEN

    def _notify_change(self) -> None:
        if self._on_state_change:
            self._on_state_change()

    def record_success(self) -> None:
        """Record a successful call."""
        old = self._state
        was_tripped = self._trip_count > 0
        self._consecutive_failures = 0
        self._last_failure_category = None
        if self.state == ProviderState.HALF_OPEN:
            self._consecutive_successes += 1
            if self._consecutive_successes >= self._success_threshold:
                self._state = ProviderState.CLOSED
                self._consecutive_successes = 0
                self._trip_count = 0  # recovered — reset backoff
        else:
            self._state = ProviderState.CLOSED
        if self._state != old:
            self._notify_change()
        # Notify recovery listeners when provider fully recovers
        if was_tripped and self._trip_count == 0 and self._on_recovery:
            self._on_recovery(self._provider.name)

    def probe_suspect(self) -> bool:
        """Probe reported this provider may be down. Move to HALF_OPEN for verification.

        Only downgrades: CLOSED → HALF_OPEN. Does not change OPEN (already worse)
        or HALF_OPEN (already suspect). Returns True if state changed.
        """
        if self._state == ProviderState.CLOSED:
            self._state = ProviderState.HALF_OPEN
            self._consecutive_successes = 0
            self._notify_change()
            return True
        return False

    def record_probe_success(self) -> None:
        """A free health probe (GET /v1/models returned 200 with this provider's
        model listed) confirmed reachability while the breaker is HALF_OPEN.

        Advances toward recovery with the STRICTER ``probe_success_threshold``
        (a probe is weaker evidence than a real completion), so a low/no-traffic
        fallback provider can heal instead of being stuck in HALF_OPEN forever.
        No-op outside HALF_OPEN (uses the ``state`` property so an OPEN breaker
        whose window has expired transitions to HALF_OPEN first). On full
        recovery it fires ``on_recovery`` — exactly like ``record_success`` — so
        the provider's lingering ``provider_failure`` observation auto-resolves.
        """
        if self.state != ProviderState.HALF_OPEN:
            return
        was_tripped = self._trip_count > 0
        self._consecutive_successes += 1
        if self._consecutive_successes >= self._probe_success_threshold:
            old = self._state
            self._state = ProviderState.CLOSED
            self._consecutive_successes = 0
            self._consecutive_failures = 0
            self._last_failure_category = None
            self._trip_count = 0  # recovered — reset backoff
            if self._state != old:
                self._notify_change()
            if was_tripped and self._on_recovery:
                self._on_recovery(self._provider.name)

    def record_failure(self, category: ErrorCategory) -> bool:
        """Record a failed call. Returns True if this failure caused the breaker to trip OPEN."""
        self._last_failure_category = category
        self._consecutive_successes = 0
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold:
            self._trip_count += 1
            self._state = ProviderState.OPEN
            self._opened_at = self._clock()
            self._consecutive_failures = 0
            self._notify_change()
            return True
        if self.state == ProviderState.HALF_OPEN:
            self._trip_count += 1
            self._state = ProviderState.OPEN
            self._opened_at = self._clock()
            self._consecutive_failures = 0
            self._notify_change()
            return True
        return False


class CircuitBreakerRegistry:
    """Registry of circuit breakers, one per provider."""

    def __init__(
        self,
        providers: dict[str, ProviderConfig],
        clock: object = None,
        state_file: Path | str | None = None,
        on_recovery: object = None,
        persist: bool = True,
        essential_sites: dict[str, list[str]] | None = None,
    ) -> None:
        self._providers = providers
        self._clock = clock
        self._state_file = Path(state_file) if state_file else _STATE_FILE
        self._on_recovery = on_recovery
        # persist=False → read-only registry (MCP child processes): load shared
        # state at construction but never write it, so only the server owns the
        # file and concurrent children can't clobber it (WS-3c).
        self._persist = persist
        # essential_site_id → [provider names]. When present, degradation is
        # COVERAGE-based: the system is degraded only when an essential site
        # has no available provider. When absent (e.g. unit tests that build a
        # bare registry), compute_degradation_level falls back to the legacy
        # provider-count behavior. See genesis.routing.essential.
        self._essential_sites = essential_sites or {}
        self._breakers: dict[str, CircuitBreaker] = {}
        self.load_state()

    def update_providers(self, providers: dict[str, ProviderConfig]) -> None:
        """Merge new provider configs into the registry (for hot-reload)."""
        self._providers.update(providers)

    def get(self, provider: str) -> CircuitBreaker:
        """Get or create a circuit breaker for the named provider."""
        if provider not in self._breakers:
            cfg = self._providers[provider]
            self._breakers[provider] = CircuitBreaker(
                provider=cfg,
                open_duration_s=cfg.open_duration_s,
                clock=self._clock,
                on_state_change=self.save_state if self._persist else None,
                on_recovery=self._on_recovery,
            )
        return self._breakers[provider]

    def save_state(self) -> None:
        """Persist breaker states to disk so they survive restarts.

        No-op for read-only (persist=False) registries — MCP children must not
        write the shared file. Uses an atomic write so a concurrent reader never
        observes a truncated file (which load_state would silently discard).
        """
        if not self._persist:
            return
        data = {}
        for name, cb in self._breakers.items():
            data[name] = {
                "state": cb._state.value,
                "consecutive_failures": cb._consecutive_failures,
                "trip_count": cb._trip_count,
                "last_failure_category": cb._last_failure_category.value if cb._last_failure_category else None,
            }
        try:
            atomic_write_text(self._state_file, json.dumps(data, indent=2))
        except Exception:
            logger.error("Failed to save circuit breaker state", exc_info=True)

    def load_state(self) -> None:
        """Restore breaker states from disk after restart."""
        if not self._state_file.is_file():
            return
        try:
            data = json.loads(self._state_file.read_text())
            for name, info in data.items():
                if name in self._providers:
                    cb = self.get(name)
                    saved_state = info.get("state", "CLOSED")
                    if saved_state == ProviderState.OPEN.value:
                        cb._state = ProviderState.OPEN
                        cb._opened_at = cb._clock()
                    cb._consecutive_failures = info.get("consecutive_failures", 0)
                    cb._trip_count = info.get("trip_count", 0)
                    # Cap backoff on restart — escalating backoff is for consecutive
                    # failures within a session, not across restarts spanning weeks.
                    # Cap=3 → max backoff = min(120*2^2, 1800) = 480s (8 min).
                    if saved_state == ProviderState.OPEN.value:
                        cb._trip_count = min(cb._trip_count, 3)
                    saved_cat = info.get("last_failure_category")
                    cb._last_failure_category = ErrorCategory(saved_cat) if saved_cat else None
            logger.info("Circuit breaker state restored from %s", self._state_file)
        except Exception:
            logger.warning("Failed to load circuit breaker state", exc_info=True)

    def _provider_available(self, name: str) -> bool:
        """True if a provider can serve an essential site's traffic.

        "Available" = breaker not OPEN (CLOSED *or* HALF_OPEN) AND a usable API
        key. HALF_OPEN counts as available on purpose: the breaker has expired
        from OPEN and the router WILL attempt the provider again, so treating it
        as covered keeps coverage consistent with routing and avoids a false
        ESSENTIAL alarm while a provider is recovering. A name not in the
        provider set counts as unavailable.
        """
        cfg = self._providers.get(name)
        if cfg is None:
            return False
        return self.get(name).is_available() and cfg.has_api_key

    def uncovered_essential_sites(self) -> list[str]:
        """Essential cloud sites that currently have NO available provider
        (breaker not OPEN and key present).

        Empty when no essential map was injected (coverage unknown). Shared by
        ``compute_degradation_level`` and the API-key alert severity so both
        surfaces agree on what 'critical' means.
        """
        uncovered: list[str] = []
        for site, providers in self._essential_sites.items():
            if not any(self._provider_available(p) for p in providers):
                uncovered.append(site)
        return uncovered

    def compute_degradation_level(self) -> DegradationLevel:
        """Compute system-wide degradation.

        The ollama (local-compute) axis is independent and unchanged.

        Cloud axis — two modes:
          * COVERAGE-based (when an essential-site→providers map was injected):
            the system is degraded ONLY when an essential cloud site has no
            available provider. A paid-provider outage that free providers still
            cover does NOT degrade — it returns NORMAL. This is the fix for the
            false "all paid providers down ⇒ ESSENTIAL" alarm. Per the product
            decision, the cloud axis is binary here: NORMAL (all essentials
            covered) or ESSENTIAL (≥1 essential uncovered).
          * LEGACY provider-count (no map injected, e.g. bare unit-test
            registries): preserved exactly so existing behavior/tests hold.
        """
        ollama_providers = [
            name
            for name, cfg in self._providers.items()
            if cfg.provider_type == "ollama"
        ]
        ollama_down = sum(
            1 for name in ollama_providers if not self.get(name).is_available()
        )
        # Check ollama axis first (independent)
        if ollama_providers and ollama_down == len(ollama_providers):
            return DegradationLevel.LOCAL_COMPUTE_DOWN

        if self._essential_sites:
            if self.uncovered_essential_sites():
                return DegradationLevel.ESSENTIAL
            return DegradationLevel.NORMAL

        # Legacy provider-count fallback (no essential map injected).
        cloud_providers = [
            name
            for name, cfg in self._providers.items()
            if cfg.provider_type != "ollama" and not cfg.is_free
        ]
        cloud_down = sum(
            1 for name in cloud_providers if not self.get(name).is_available()
        )
        if cloud_providers and cloud_down == len(cloud_providers):
            return DegradationLevel.ESSENTIAL
        if cloud_down > 1:
            return DegradationLevel.REDUCED
        if cloud_down == 1:
            return DegradationLevel.FALLBACK

        return DegradationLevel.NORMAL
