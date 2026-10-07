"""Circuit breaker for provider health tracking."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import threading
import time
import uuid
from pathlib import Path

from genesis.routing.provider_identity import provider_identity, restored_provenance, valid_identity
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
# Categories that hold the breaker open on the LONG cap: neither an exhausted
# allowance nor a missing entitlement resolves itself within the 30m default.
_LONG_OPEN_CATEGORIES = frozenset({
    ErrorCategory.QUOTA_EXHAUSTED,
    ErrorCategory.NOT_ENTITLED,
})



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
        # True when the failure that OPENED this breaker was a real CALL.
        # The probe-heal rule reads exactly this and nothing else — see
        # `record_probe_success`. Moves in lockstep with the OPEN transitions:
        # set at every trip, cleared at every close.
        self._opened_by_call: bool = False
        self._failure_cause: str | None = None
        self._failure_identity: str | None = None
        # Legacy/account incidents retain None; retirement creates a fresh namespace.
        self._incident_identity: str | None = None
        self._incident_provider = provider.name

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
        """Category of the most recent failure — NOT only of a tripping one.

        ``record_failure`` assigns this before it evaluates the trip threshold,
        so a CLOSED breaker routinely carries a category from a failure that
        never tripped it, and a category that never trips at all (RATE_LIMITED)
        can sit here indefinitely. ``record_success`` clears it to None, so the
        honest reading is "the most recent outcome was a failure of this kind,
        and nothing has succeeded since".

        The docstring used to say "that tripped the breaker". Two separate
        defects were reasoned out from that wrong description before it was
        corrected (2026-09-07), so the precision here is load-bearing rather
        than pedantic.
        """
        return self._last_failure_category

    def _effective_open_duration(self) -> float:
        """Open duration with escalating backoff.

        First trip uses base duration; each subsequent trip doubles it.
        Capped at _MAX_QUOTA_OPEN_S (4h) for quota exhaustion and entitlement
        denial, _MAX_OPEN_S (30m) otherwise. Both of those clear on a timescale
        set by an account change, not by a passing outage, so re-probing them on
        the short cap only buys a doomed attempt 8x more often.
        """
        exponent = max(0, self._trip_count - 1)
        cap = (
            _MAX_QUOTA_OPEN_S
            if self._last_failure_category in _LONG_OPEN_CATEGORIES
            else _MAX_OPEN_S
        )
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
        had_provenance = self._failure_cause is not None or self._failure_identity is not None
        self._consecutive_failures = 0
        self._last_failure_category = None
        self._failure_cause = None
        self._failure_identity = None
        if self.state == ProviderState.HALF_OPEN:
            self._consecutive_successes += 1
            if self._consecutive_successes >= self._success_threshold:
                self._state = ProviderState.CLOSED
                self._consecutive_successes = 0
                self._trip_count = 0  # recovered — reset backoff
                self._opened_by_call = False
        else:
            # The ordinary path: a successful call on an already-CLOSED
            # breaker. Clearing the flag here is a no-op in that case (it is
            # already False) and is kept DEFENSIVELY for the other state this
            # branch accepts — an OPEN breaker whose window has not expired,
            # which closes WITHOUT resetting `_trip_count`, leaving a CLOSED
            # breaker carrying a non-zero trip count and, without this line, a
            # stale `_opened_by_call=True` that would refuse probe healing
            # forever.
            #
            # VERIFIED not reachable through either production caller today:
            # `router.py:293` and `memory/retrieval.py:1617` both skip on
            # `is_available()`, which is False while OPEN. Stated as defence,
            # not as a live path — this class of unverified "the real path is"
            # claim is what generated the bugs this redesign removes.
            self._state = ProviderState.CLOSED
            self._opened_by_call = False
        if self._state != old or had_provenance:
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
        """A free health probe reported this provider reachable, with its model
        listed, while the breaker is HALF_OPEN.

        EVIDENCE SYMMETRY — the one rule this method enforces:
        **a probe may only undo what a probe did.**

        `probe_suspect()` moves a CLOSED breaker to HALF_OPEN on a failed or
        rate-limited probe. That suspicion is the only thing a clean probe is
        entitled to clear. A breaker opened by a real CALL failure is cleared
        only by a real `record_success()`.

        Why, concretely: a `GET /v1/models` answering 200 is evidence of
        *reachable*, never of *working*. It is exactly what a 403-on-use, an
        exhausted quota, or a stream of truncated completions looks like from
        outside. Live incident 2026-08-28/29: `mistral-large-latest` stayed
        listed while every real call returned 403, so probes "confirmed health"
        repeatedly. MEASURED 2026-09-01 over the outage that began 08-27:
        EIGHT separate `provider_failure` observations, each with a fresh
        `first_trip_at`, and ALL EIGHT auto-resolved by a false heal — while
        `activity_log` for the same provider read 20 calls, 20 failures, ZERO
        successes, last error still `tier_not_allowed`. Every record of the
        outage had been closed by a listing probe while the provider was
        completely dead, so no surface could report its real age.

        This does NOT hold a provider out of rotation. A call-tripped breaker
        still leaves OPEN on its own backoff (`state` auto-transitions to
        HALF_OPEN when the window expires), and HALF_OPEN is `is_available()`,
        so the next real call routed to it IS the retry. What the probe no
        longer does is declare victory before that call happens.

        Supersedes PR #705 (2026-06-19), which introduced probe healing so "a
        low/no-traffic fallback can heal instead of being stuck in HALF_OPEN
        forever". That concern was about the DASHBOARD state, not routing —
        HALF_OPEN was already routable. The cost of #705's approach was that a
        probe could resolve an outage record it had no evidence about.
        """
        if self.state != ProviderState.HALF_OPEN:
            return
        if self._opened_by_call:
            return
        self._consecutive_successes += 1
        if self._consecutive_successes >= self._probe_success_threshold:
            old = self._state
            self._state = ProviderState.CLOSED
            self._consecutive_successes = 0
            self._consecutive_failures = 0
            self._last_failure_category = None
            self._failure_cause = None
            self._failure_identity = None
            self._trip_count = 0
            self._opened_by_call = False
            if self._state != old:
                self._notify_change()
            # NO `on_recovery` here, by construction rather than by guard.
            # That hook is what resolves the `provider_failure` observation and
            # clears its `first_trip_at`. Reaching this line means the breaker
            # was opened by a PROBE, which never trips and so never escalates —
            # there is no outage record to resolve. Firing it would let a
            # listing probe close a record it has no evidence about, which is
            # the defect this whole change removes. `record_success` still
            # fires it, which is the correct and only path.

    def record_failure(self, category: ErrorCategory, *, retirement: bool = False) -> bool:
        """Record a failed call. Returns True if this failure caused the breaker to trip OPEN."""
        identity = provider_identity(self._provider)
        cause = "retirement" if retirement and category == ErrorCategory.TRANSIENT else "other"
        if (self._failure_cause is None and self._trip_count == 0
                and self._consecutive_failures == 0 and self._state == ProviderState.CLOSED
                and self._last_failure_category is None):
            self._failure_cause = cause
            self._failure_identity = identity
        elif self._failure_cause != cause or self._failure_identity != identity:
            # Any mixed/legacy/operator history makes retirement-only unproven.
            self._failure_cause = "other"
        self._last_failure_category = category
        self._consecutive_successes = 0
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold:
            self._trip_count += 1
            self._state = ProviderState.OPEN
            self._opened_at = self._clock()
            self._consecutive_failures = 0
            self._opened_by_call = True  # a real call opened this
            self._notify_change()
            return True
        if self.state == ProviderState.HALF_OPEN:
            self._trip_count += 1
            self._state = ProviderState.OPEN
            self._opened_at = self._clock()
            self._consecutive_failures = 0
            self._opened_by_call = True  # a real call opened this
            self._notify_change()
            return True
        return False

    # ---- operator transitions -------------------------------------------
    # The dashboard's provider toggle is the ONLY caller outside this class
    # that changes breaker state. It used to assign the private fields
    # directly, which made `_opened_by_call` a CONVENTION: every external
    # mutation site had to remember to maintain it, and the first one written
    # did not. These two methods exist so the flag cannot be forgotten -- the
    # state and its origin are set together, by construction. A guard test
    # pins the rule that no module outside this file assigns breaker state
    # (best-effort: it is a source scan, so it catches the shapes people
    # actually write, not every shape that is possible).
    #
    # They are complete WITH RESPECT TO BREAKER FIELDS -- not the raw
    # assignments they replaced: each notifies, and each clears the qualifiers
    # it invalidates. Deliberately NOT complete beyond this object: neither
    # fires `on_recovery`, so an operator re-enable leaves the
    # `provider_failure` observation open and leaves `escalation._state`
    # (trip count + `first_trip_at`) accumulating across the reset. That is
    # arguably right under evidence symmetry -- an operator clicking a button
    # is not evidence the provider works -- but it IS a behaviour a caller
    # must know about, so it is stated rather than left to be discovered. That
    # matters because `_notify_change` is what the registry wires to
    # `save_state` -- without it the caller has to remember to persist, which
    # is the same class of obligation these methods exist to delete.
    #
    # KNOWN LIMIT, not fixed here: `_opened_by_call` is a two-valued answer to
    # a now three-valued question (call / probe / operator). The dashboard
    # renders an operator disable as "real calls are failing", which is wrong.
    # An origin enum is the right model and is tracked as its own change --
    # this method records the operator case as call-origin only because that
    # produces the correct HEAL rule, not the correct LABEL.

    def force_open(self) -> None:
        """Operator disable: hold this provider OPEN by explicit human action.

        Sets `_opened_by_call`, so a listing probe may not close it. Under
        evidence symmetry a probe may only undo a probe's own suspicion, and
        an operator taking a provider out of rotation is not one -- it is at
        least as strong a statement as a failed call.

        `_trip_count = 99` is the pre-existing "stay open" idiom: it drives
        `_effective_open_duration()` to its cap. NOTE a pre-existing limit this
        does NOT fix -- that cap is 30 minutes, after which `.state`
        auto-transitions to HALF_OPEN and the provider is routable again, and
        a restart caps `_trip_count` to 3 (`load_state`) AND resets
        `_opened_at` to now -- so a restart shortens the remaining window to 8
        minutes but re-starts its clock, which can EXTEND the total hold (a
        restart 29 minutes in yields ~37). Soft in both directions. Tracked
        as follow-up `506fc0f2`. The flag stops a PROBE from closing the
        breaker; it does not stop the window from expiring.
        """
        self._state = ProviderState.OPEN
        self._opened_at = self._clock()
        self._trip_count = 99
        self._failure_cause = "operator"
        self._failure_identity = None
        self._opened_by_call = True
        # An operator disable is not a failure, so it must not inherit the
        # last failure's category: `_effective_open_duration()` reads it to
        # pick the 4h quota cap, which would make the hold length depend on
        # unrelated history (4h if the last failure happened to be
        # QUOTA_EXHAUSTED, 30 min otherwise). Two other readers see the
        # erasure: `vitals.py` (`last_failure`) and `api_keys.py` (`reason`)
        # lose the last real diagnostic for a provider an operator disables,
        # and `save_state` persists that. Accepted: the category described a
        # failure, and this transition is not one.
        self._last_failure_category = None
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._notify_change()

    def force_close(self) -> None:
        """Operator re-enable: return this provider to a clean CLOSED.

        Clears `_opened_by_call` with the counters AND the failure category,
        so "clean" is literal rather than aspirational. Leaving it True would put
        "a real call opened this" on a breaker a human has just declared
        healthy -- the poison pill in the new field. The next probe blip would
        move it to HALF_OPEN, every clean probe would then be refused, and the
        dashboard would read "unverified" until real traffic or a restart,
        in defiance of the explicit reset.
        """
        self._state = ProviderState.CLOSED
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._trip_count = 0
        self._opened_by_call = False
        # A qualifier must not outlive the state it qualifies -- the same rule
        # `load_state` applies to a restored breaker. Leaving the category set
        # would render a stale `last_failure` / `reason` on a breaker a human
        # just declared healthy (`vitals.py`, `api_keys.py` both read it).
        self._last_failure_category = None
        self._failure_cause = None
        self._failure_identity = None
        self._notify_change()


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
        restore_state: bool = True,
        on_retirement: object = None,
    ) -> None:
        self._lock = threading.RLock()
        self._providers = dict(providers)
        self._clock = clock
        self._state_file = Path(state_file) if state_file else _STATE_FILE
        self._on_recovery = on_recovery
        self._on_retirement = on_retirement
        # persist=False → read-only registry (MCP child processes): load shared
        # state at construction but never write it, so only the server owns the
        # file and concurrent children can't clobber it (WS-3c).
        self._persist = persist
        # essential_site_id → [provider names]. When present, degradation is
        # COVERAGE-based: the system is degraded only when an essential site
        # has no available provider. When absent (e.g. unit tests that build a
        # bare registry), compute_degradation_level falls back to the legacy
        # provider-count behavior. See genesis.routing.essential.
        # None and {} are DIFFERENT: None is "no map" (legacy); {} is a map in
        # which every present essential site is blocked by configuration, which
        # is still coverage mode (nothing uncovered, so NORMAL).
        self._essential_sites = essential_sites
        # Whether this registry manages coverage at all, fixed at construction.
        # The map itself can be None after a reload whose config has no
        # essential site, so it cannot stand in for this.
        self._manages_coverage = essential_sites is not None
        self._breakers: dict[str, CircuitBreaker] = {}
        self._restoring_state = False
        if restore_state:
            self.load_state()

    @classmethod
    def health_view(cls, config, bindings: dict[str, CircuitBreaker]):
        """Read captured bindings through the full health registry interface.

        No disk restoration, persistence or recovery callbacks belong to a
        request's view. Coverage uses the same captured routing configuration.
        """
        from genesis.routing.essential import build_essential_provider_map

        view = cls(config.providers, persist=False, restore_state=False,
                   essential_sites=build_essential_provider_map(config))
        view._breakers = dict(bindings)
        return view

    def _alias_target(self, name: str, known) -> str | None:
        """The key in ``known`` that ``name``'s persisted/held state belongs to, or None.

        Lazy import: ``routing.config`` is the owner of the alias map, and
        importing it at module scope would tie the breaker to the config loader
        for a lookup that only matters on two paths.
        """
        try:
            from genesis.routing.config import _current_provider_name, _resolve_provider_alias
        except Exception:  # noqa: BLE001 — no alias map available is no migration.
            return name if name in known else None
        resolved = _resolve_provider_alias(name, known)
        if resolved in known:
            return resolved
        # The append-only family map also preserves holds on rollback to a sole
        # legacy alias. Never choose between two explicitly configured siblings.
        family = _current_provider_name(name)
        peers = [candidate for candidate in known if _current_provider_name(candidate) == family]
        return peers[0] if len(peers) == 1 else None

    def update_providers(self, providers: dict[str, ProviderConfig]) -> None:
        with self._lock:
            self._update_providers(providers)

    def _update_providers(self, providers: dict[str, ProviderConfig]) -> None:
        """Prepare all replacements, then swap registry-owned bindings.

        Same identity and alias keeps live health. A rename/replacement copies
        raw hold state into a rebound breaker, so late old requests cannot heal,
        poison or persist replacement health. Only proven retirement-only state
        for a different identity is cleared; all ambiguous holds are retained.
        """
        bindings = {
            name: self._bind_breaker(self._prior_breaker(name, providers), cfg)
            for name, cfg in providers.items()
        }
        # Preserve existing owners first; a newly configured sibling cannot
        # claim the historical anchor/namespace of a continuing incident.
        seen = set()
        for name in sorted(bindings, key=lambda name: self._prior_breaker(name, providers) is None):
            cb = bindings[name]
            pair = (cb._incident_provider, cb._incident_identity)
            if pair in seen:
                cb._incident_identity = self._new_incident_identity()
            seen.add((cb._incident_provider, cb._incident_identity))
        kept = {id(cb) for cb in bindings.values()}
        detached = [cb for cb in self._breakers.values() if id(cb) not in kept]
        self._providers = dict(providers)
        self._breakers = bindings
        owned = {(cb._incident_provider, cb._incident_identity) for cb in bindings.values()}
        removed = {(cb._incident_provider, cb._incident_identity) for cb in detached} - owned
        for cb in detached:
            cb._on_state_change = None
            cb._on_recovery = None
        if self._on_retirement:
            for anchor, incident in removed:
                self._on_retirement(anchor, incident)
        self.save_state()

    def _prior_breaker(self, name: str, providers) -> CircuitBreaker | None:
        if name in self._breakers:
            return self._breakers[name]
        return next((candidate for legacy, candidate in self._breakers.items()
                     if legacy not in providers and self._alias_target(legacy, providers) == name), None)

    def _bind_breaker(self, old: CircuitBreaker | None, cfg: ProviderConfig) -> CircuitBreaker:
        if old is None:
            return CircuitBreaker(
                cfg, open_duration_s=cfg.open_duration_s, clock=self._clock,
                on_state_change=self.save_state if self._persist else None,
                on_recovery=self._on_recovery,
            )
        if old._provider == cfg:
            return old
        replacement = copy.copy(old)
        replacement._provider = cfg
        replacement._open_duration_s = cfg.open_duration_s
        if self._retired_identity_changed(old._failure_cause, old._failure_identity, cfg):
            # Reset without invoking callbacks for an unobserved model.
            replacement._on_state_change = None
            replacement.force_close()
            replacement._incident_identity = self._new_incident_identity()
            replacement._incident_provider = cfg.name
        replacement._on_state_change = self.save_state if self._persist else None
        replacement._on_recovery = self._on_recovery
        return replacement

    @staticmethod
    def _retired_identity_changed(cause, identity, cfg: ProviderConfig) -> bool:
        return (
            cause == "retirement" and valid_identity(identity)
            and identity != provider_identity(cfg)
        )

    @staticmethod
    def _new_incident_identity() -> str:
        return hashlib.sha256(uuid.uuid4().bytes).hexdigest()

    def current_incident_binding(self, provider: str) -> tuple[str, str | None]:
        """Fresh configured alias lookup; no historical alias redirection."""
        with self._lock:
            cb = self.get(provider)
            return cb._incident_provider, cb._incident_identity

    def incident_owner(self, anchor: str, incident: str | None) -> str | None:
        """Current owner of historical evidence, distinct from fresh aliases."""
        with self._lock:
            return next((name for name, cb in self._breakers.items()
                         if (cb._incident_provider, cb._incident_identity) == (anchor, incident)), None)

    def current_incident_identity(self, provider: str) -> str | None:
        with self._lock:
            return self.get(provider)._incident_identity

    def current_identity(self, provider: str) -> str | None:
        """Identity of a current binding, without recreating removed providers."""
        with self._lock:
            cb = self._breakers.get(provider)
            return provider_identity(cb._provider) if cb is not None else None

    def toggle(self, provider: str) -> ProviderState:
        """Apply a dashboard toggle to the current binding atomically with reload."""
        with self._lock:
            cb = self.get(provider)
            if cb.state == ProviderState.OPEN:
                cb.force_close()
            else:
                cb.force_open()
            return cb.state

    def get(self, provider: str) -> CircuitBreaker:
        with self._lock:
            return self._get(provider)

    def _get(self, provider: str) -> CircuitBreaker:
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
            cb = self._breakers[provider]
            if any(other is not cb and (other._incident_provider, other._incident_identity)
                   == (cb._incident_provider, cb._incident_identity) for other in self._breakers.values()):
                cb._incident_identity = self._new_incident_identity()
                self.save_state()
        return self._breakers[provider]

    def save_state(self) -> None:
        with self._lock:
            self._save_state()

    def _save_state(self) -> None:
        """Persist breaker states to disk so they survive restarts.

        No-op for read-only (persist=False) registries — MCP children must not
        write the shared file. Uses an atomic write so a concurrent reader never
        observes a truncated file (which load_state would silently discard).
        """
        if not self._persist or self._restoring_state:
            return
        data = {}
        for name, cb in self._breakers.items():
            data[name] = {
                "state": cb._state.value,
                "consecutive_failures": cb._consecutive_failures,
                "trip_count": cb._trip_count,
                "last_failure_category": cb._last_failure_category.value if cb._last_failure_category else None,
                "opened_by_call": cb._opened_by_call,
                "identity": provider_identity(cb._provider),
                "failure_cause": cb._failure_cause,
                "failure_identity": cb._failure_identity,
                "incident_identity": cb._incident_identity,
                "incident_provider": cb._incident_provider,
            }
        try:
            atomic_write_text(self._state_file, json.dumps(data, indent=2))
        except Exception:
            logger.error("Failed to save circuit breaker state", exc_info=True)

    def load_state(self) -> None:
        """Restore all bindings before any callback can persist their state."""
        with self._lock:
            if not self._state_file.is_file():
                return
            self._restoring_state = True
            try:
                completed = self._load_state()
            finally:
                self._restoring_state = False
            if completed:
                self.save_state()

    def _load_state(self) -> bool:
        try:
            data = json.loads(self._state_file.read_text())
            # A RENAME retires the key this state was persisted under, and the
            # match below is exact — so an unmatched row was silently dropped and
            # the renamed provider got a fresh CLOSED breaker. An install with an
            # actively failing provider therefore resumed sending it traffic on
            # the first restart after the upgrade, until enough NEW failures
            # tripped it again, reintroducing exactly the latency and paid-fallback
            # churn that persisting this state exists to prevent. Confirmed on a
            # live install: 26 persisted rows, 7 under names a rename retired, one
            # of them `half_open`.
            #
            # TWO PASSES, because both generations can be present and the CURRENT
            # name's row has to win. Pass 1 takes every row whose key is a live
            # provider; pass 2 takes a legacy row ONLY if its target was not
            # already restored. One pass in dict order would let whichever row
            # came first decide, which is the same order-dependence bug the
            # overlay migration was fixed for.
            rows = list(data.items())
            restored: set[str] = set()
            ordered: list[tuple[str, dict, str]] = []
            for name, info in rows:
                if name in self._providers:
                    ordered.append((name, info, name))
            for name, info in rows:
                if name in self._providers:
                    continue
                target = self._alias_target(name, self._providers)
                if target:
                    ordered.append((name, info, target))

            for name, info, target in ordered:
                if target in restored:
                    logger.info(
                        "Persisted breaker state for '%s' skipped — '%s' was already "
                        "restored from its current name, which wins",
                        name, target,
                    )
                    continue
                restored.add(target)
                if target != name:
                    logger.info(
                        "Restoring persisted breaker state from '%s' onto '%s' "
                        "(provider renamed upstream)",
                        name, target,
                    )
                cfg = self._providers[target]
                cb = self.get(target)
                saved_state = info.get("state", "CLOSED")
                restored_live = saved_state in (ProviderState.OPEN.value, ProviderState.HALF_OPEN.value)
                count = info.get("consecutive_failures")
                pending = (saved_state == ProviderState.CLOSED.value
                           and type(count) is int and 0 < count < cb._failure_threshold
                           and type(info.get("trip_count")) is int and info["trip_count"] == 0)
                cause, identity = restored_provenance(info, restored_live, pending=pending)
                saved_incident = info.get("incident_identity")
                incident = saved_incident if valid_identity(saved_incident) else None
                anchor = info.get("incident_provider", name)
                from genesis.routing.config import _current_provider_name
                if not isinstance(anchor, str) or _current_provider_name(anchor) != _current_provider_name(target):
                    anchor = target
                if self._retired_identity_changed(cause, identity, cfg):
                    cb._incident_identity = self._new_incident_identity()
                    if self._on_retirement:
                        self._on_retirement(anchor, incident)
                    continue
                cb._incident_identity = incident
                cb._incident_provider = anchor
                # BOTH non-closed states restore. Restoring only OPEN made
                # the guarantee below expire at the next restart, by an
                # entirely ordinary route: `.state` assigns HALF_OPEN when
                # merely READ (it mutates and does NOT notify), and
                # `save_state` serialises EVERY breaker's raw `_state`, so
                # any other breaker's change persists this one as
                # "half_open". A restart then read it as CLOSED — a dead
                # provider reporting healthy and probe-healable again, which
                # is the original defect returning on a timer.
                # `_opened_at` is reset for OPEN only: for HALF_OPEN it is
                # not consulted (the window has already elapsed), and the
                # breaker is routable, so the next real call decides it.
                if saved_state == ProviderState.OPEN.value:
                    cb._state = ProviderState.OPEN
                    cb._opened_at = cb._clock()
                elif saved_state == ProviderState.HALF_OPEN.value:
                    cb._state = ProviderState.HALF_OPEN
                    cb._consecutive_successes = 0
                cb._consecutive_failures = info.get("consecutive_failures", 0)
                cb._trip_count = info.get("trip_count", 0)
                # Cap backoff on restart — escalating backoff is for consecutive
                # failures within a session, not across restarts spanning weeks.
                # Cap=3 → max backoff = min(120*2^2, 1800) = 480s (8 min).
                if saved_state == ProviderState.OPEN.value:
                    cb._trip_count = min(cb._trip_count, 3)
                # Pre-trip failures are evidence even while CLOSED. Restore
                # only valid positive pending counters; this does not assert
                # that a call opened the breaker or grant probe-heal immunity.
                cb._failure_cause, cb._failure_identity = restored_provenance(
                    info, restored_live, pending=pending)
                saved_origin = info.get("opened_by_call")
                if not restored_live:
                    cb._opened_by_call = False
                elif saved_origin is None:
                    cb._opened_by_call = saved_state == ProviderState.OPEN.value
                else:
                    cb._opened_by_call = bool(saved_origin)
                saved_cat = info.get("last_failure_category")
                try:
                    cb._last_failure_category = (
                        ErrorCategory(saved_cat)
                        if saved_cat and (restored_live or pending)
                        else None
                    )
                except ValueError:
                    # A category this build does not know -- a file written
                    # by a NEWER build, i.e. the rollback path. Scoped to
                    # this ONE breaker on purpose: the enclosing try wraps
                    # the whole loop, so an unscoped raise here would drop
                    # every LATER provider's restored state to CLOSED with
                    # no origin, which is "forget the outage" -- exactly
                    # what this branch exists to prevent, triggered by a
                    # rollback. `ErrorCategory` has gained members before
                    # (TIMEOUT, RATE_LIMITED, BAD_REQUEST), so this is a
                    # shape that has actually occurred.
                    logger.warning(
                        "Unknown failure category %r for provider %s; "
                        "restoring state without it",
                        saved_cat, name,
                    )
                    cb._last_failure_category = None
            logger.info("Circuit breaker state restored from %s", self._state_file)
            return True
        except Exception:
            logger.warning("Failed to load circuit breaker state", exc_info=True)
            return False

    def _provider_available(self, name: str) -> bool:
        """True if a provider can serve an essential site's traffic.

        "Available" = breaker not OPEN (CLOSED *or* HALF_OPEN) AND a usable API
        key. HALF_OPEN counts as available on purpose: the breaker has expired
        from OPEN and the router WILL attempt the provider again, so treating it
        as covered keeps coverage consistent with routing and avoids a false
        ESSENTIAL alarm while a provider is recovering. A name not in the
        provider set counts as unavailable.

        **A CLOSED breaker is not always evidence of health.** RATE_LIMITED is
        deliberately excluded from tripping (``retry.py`` — 429 is provider
        backpressure, not a health signal, and tripping would take a reachable
        provider offline for every other call site). The consequence for THIS
        function is that a provider whose steady state is 429 never trips, so it
        reads "available" forever while serving nothing — and because coverage is
        ``any(available)``, one such member is enough to mark an essential site
        covered and silence the ESSENTIAL degradation alarm while every call to
        that site fails.

        That is not hypothetical: adding a 429-throttled rung to
        ``4_light_reflection`` and ``9_fact_extraction`` did exactly this, and it
        survived three cost-focused review rounds because it is caused by chain
        MEMBERSHIP — independent of the ``free:`` flag and of ``never_pays``
        filtering, which is where everyone was looking.

        **KNOWN GAP, NOT FIXED HERE — and the obvious fix does not work.** The
        tempting guard is ``if cb.last_failure_category is RATE_LIMITED: return
        False``. It is INERT: ``router.py`` (see its RATE_LIMITED/BAD_REQUEST
        exclusion) never calls ``record_failure`` for a 429 at all, so
        ``_last_failure_category`` is never assigned that value on the production
        path — a 429-only provider carries ``None`` and zero consecutive
        failures. That guard would read as protection and provide none, which is
        worse than leaving the gap visible. It was written, MEASURED to be inert,
        and removed (2026-09-07).

        Closing this properly needs a signal the breaker does not currently keep:
        either a suppressed-failure counter recorded alongside the excluded
        categories, or a "has ever served a request" bit. Both change breaker
        semantics and belong in their own change, not in a config PR.
        """
        cfg = self._providers.get(name)
        if cfg is None:
            return False
        return self.get(name).is_available() and cfg.has_api_key

    def chain_has_available(self, chain: list[str]) -> bool:
        """True if ANY provider in ``chain`` can currently serve requests.

        Reuses ``_provider_available`` (breaker not OPEN — CLOSED or HALF_OPEN —
        AND a usable key). Callers use this to FAIL FAST when a whole chain is
        circuit-breaker-open, instead of firing a doomed ``route_call`` that just
        emits another ``all_exhausted`` event (and the reactive-cycle storm that
        feeds off it). An empty or all-unknown chain returns False.
        """
        return any(self._provider_available(p) for p in chain)

    def refresh_essential_sites(self, essential_sites: dict[str, list[str]] | None) -> None:
        """Replace the essential map after a routing reload.

        Only a registry that was BUILT with a map is refreshed: a bare or
        standalone registry stays on the legacy check, as it was constructed.
        Without this, a map built while an overlay blocked every essential site
        (``{}``, coverage mode, NORMAL) would outlive the fix until a restart.

        Keyed on the construction-time flag, not the current map: a reload with
        no essential site stores None, and a check on the map would then ignore
        every later reload, leaving the registry on the legacy check.
        """
        if self._manages_coverage:
            self._essential_sites = essential_sites

    def uncovered_essential_sites(self) -> list[str]:
        """Essential cloud sites that currently have NO available provider
        (breaker not OPEN and key present).

        Empty when no essential map was injected (coverage unknown). Shared by
        ``compute_degradation_level`` and the API-key alert severity so both
        surfaces agree on what 'critical' means.
        """
        uncovered: list[str] = []
        for site, providers in (self._essential_sites or {}).items():
            if not any(self._provider_available(p) for p in providers):
                uncovered.append(site)
        return uncovered

    def uncovered_essential_sites_for(
        self, provider: str, also_unavailable=None,
    ) -> list[str] | None:
        """The essential sites that use ``provider`` and have NO OTHER available
        provider — i.e. the sites this provider's outage leaves unserved.

        The provider itself never counts as cover: a dead provider whose breaker
        has drifted OPEN -> HALF_OPEN reads "available" to the router, and must
        not cover its own outage. ``also_unavailable(name)`` (optional) marks
        providers the router will skip for reasons the breaker does not know
        (a spent daily quota). ``[]`` = covered; ``None`` = coverage unknown (no
        essential map), which callers must treat as "could be uncovered". The
        known 429-reads-available gap of ``_provider_available`` still applies.
        """
        if self._essential_sites is None:
            return None

        def _serves(name: str) -> bool:
            if name == provider or not self._provider_available(name):
                return False
            try:
                return not (also_unavailable and also_unavailable(name))
            except Exception:
                return True  # unknown extra signal: fall back to the breaker's word

        return sorted(
            site
            for site, chain in self._essential_sites.items()
            if provider in chain and not any(_serves(p) for p in chain)
        )

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

        if self._essential_sites is not None:
            if self.uncovered_essential_sites():
                return DegradationLevel.ESSENTIAL
            return DegradationLevel.NORMAL

        # Legacy provider-count fallback (no essential map injected). In a
        # correctly-configured install the essential map is always present
        # (build_essential_provider_map), so reaching here in production means it
        # found NO essential site at all (a misconfig that essential.py already
        # logs at build time) and degradation is now the count-based heuristic that can
        # false-alarm "all paid down => ESSENTIAL". Surface it ONCE per instance
        # so the fallback is never silent; bare unit-test registries legitimately
        # hit this and warn once, which is harmless.
        if not getattr(self, "_legacy_degradation_warned", False):
            logger.warning(
                "Degradation is running the LEGACY provider-count fallback: no "
                "essential-site map was injected. In production this means "
                "build_essential_provider_map found no essential site (a misconfiguration) "
                "and coverage-based degradation is disabled — cloud degradation may "
                "false-alarm. Verify the essential provider config."
            )
            self._legacy_degradation_warned = True
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
