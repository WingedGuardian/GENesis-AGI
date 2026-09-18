"""Daily free-provider budget ledger — unit + router chain-walk coverage.

Every counting rule here is DIRECTION-load-bearing: an undercount still
calls (the provider's 429s backstop it); an overcount deselects a servable
provider with no correcting signal until the next UTC day. The tests pin the
undercount bias explicitly.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from genesis.routing.circuit_breaker import CircuitBreakerRegistry
from genesis.routing.daily_budget import DailyBudgetLedger
from genesis.routing.degradation import DegradationTracker
from genesis.routing.router import Router
from genesis.routing.standalone import NullCostTracker
from genesis.routing.types import (
    CallResult,
    CallSiteConfig,
    ProviderConfig,
    RetryPolicy,
    RoutingConfig,
)

from .conftest import MockDelegate


@pytest.fixture(autouse=True)
def _no_kill_switch(monkeypatch):
    """Sibling-leak guard: the kill switch must never leak into these tests."""
    monkeypatch.delenv("GENESIS_DAILY_BUDGET_DISABLED", raising=False)


def _cfg(name="groq-ish", *, rpd=None, tpd=None):
    return ProviderConfig(
        name=name, provider_type="groq", model_id="m", is_free=True,
        rpm_limit=None, open_duration_s=120, rpd_limit=rpd, tpd_limit=tpd,
    )


def _ok(tokens_in=0, tokens_out=0):
    return CallResult(
        success=True, content="x", input_tokens=tokens_in, output_tokens=tokens_out,
    )


def _fail(status, *, reached=True):
    return CallResult(
        success=False, error="e", status_code=status, reached_provider=reached,
    )


def _ledger(tmp_path, *, day="2026-09-02", persist=True):
    holder = {"now": datetime.fromisoformat(f"{day}T12:00:00+00:00")}
    ledger = DailyBudgetLedger(
        state_path=tmp_path / "budget.json",
        clock=lambda: holder["now"],
        persist=persist,
    )
    return ledger, holder


class TestLedgerCounting:
    def test_requests_cap_deselects_in_its_own_unit(self, tmp_path):
        ledger, _ = _ledger(tmp_path)
        cfg = _cfg(rpd=3)
        for _ in range(3):
            ledger.record(cfg, _ok())
        assert ledger.exhausted(cfg) is True

    def test_tokens_cap_deselects_in_its_own_unit(self, tmp_path):
        ledger, _ = _ledger(tmp_path)
        cfg = _cfg(tpd=100)
        ledger.record(cfg, _ok(30, 30))
        assert ledger.exhausted(cfg) is False
        ledger.record(cfg, _ok(30, 30))
        assert ledger.exhausted(cfg) is True

    def test_units_are_never_converted(self, tmp_path):
        """A token-capped provider is never exhausted by REQUEST volume —
        the unit-swap mutant (requests compared against tpd_limit) dies here."""
        ledger, _ = _ledger(tmp_path)
        cfg = _cfg(tpd=50)
        for _ in range(200):
            ledger.record(cfg, _ok(0, 0))  # 200 requests, 0 tokens
        assert ledger.exhausted(cfg) is False

    def test_429_and_connectionless_failures_are_never_counted(self, tmp_path):
        """Undercount bias: a 429 (unproven to debit provider-side quota) and
        a timeout/connection error (no status) must not consume budget —
        overcounting deselects with no backstop."""
        ledger, _ = _ledger(tmp_path)
        cfg = _cfg(rpd=2)
        for _ in range(10):
            ledger.record(cfg, _fail(429))
            ledger.record(cfg, _fail(None))
        assert ledger.exhausted(cfg) is False
        assert ledger.status(cfg)["requests_used"] == 0

    def test_non_429_failure_counts_a_request_but_no_tokens(self, tmp_path):
        ledger, _ = _ledger(tmp_path)
        cfg = _cfg(rpd=5, tpd=1000)
        ledger.record(cfg, _fail(500))
        budget_status = ledger.status(cfg)
        assert budget_status["requests_used"] == 1
        assert budget_status["tokens_used"] == 0

    def test_crossing_reported_exactly_once(self, tmp_path):
        ledger, _ = _ledger(tmp_path)
        cfg = _cfg(rpd=2)
        assert ledger.record(cfg, _ok()) is False
        assert ledger.record(cfg, _ok()) is True  # the crossing
        assert ledger.record(cfg, _ok()) is False  # already exhausted

    def test_status_names_both_units(self, tmp_path):
        ledger, _ = _ledger(tmp_path)
        cfg = _cfg(rpd=10, tpd=100)
        ledger.record(cfg, _ok(5, 5))
        assert ledger.status(cfg) == {
            "requests_used": 1, "rpd_limit": 10,
            "tokens_used": 10, "tpd_limit": 100,
            "exhausted": False,
        }
        assert ledger.status(_cfg("unlimited")) is None


class TestDayBoundaryAndPersistence:
    def test_utc_day_rollover_resets_counters(self, tmp_path):
        ledger, holder = _ledger(tmp_path)
        cfg = _cfg(rpd=1)
        ledger.record(cfg, _ok())
        assert ledger.exhausted(cfg) is True
        holder["now"] = datetime(2026, 9, 3, 0, 0, 1, tzinfo=UTC)
        assert ledger.exhausted(cfg) is False

    def test_persistence_roundtrip_survives_restart(self, tmp_path):
        ledger, _ = _ledger(tmp_path)
        cfg = _cfg(rpd=1)
        ledger.record(cfg, _ok())
        reborn, _ = _ledger(tmp_path)
        assert reborn.exhausted(cfg) is True

    def test_reloaded_exhaustion_does_not_recross(self, tmp_path):
        """Restart with already-exhausted counters must not re-report the
        crossing (the router would re-emit the once-per-day event)."""
        ledger, _ = _ledger(tmp_path)
        cfg = _cfg(rpd=1)
        ledger.record(cfg, _ok())
        reborn, _ = _ledger(tmp_path)
        assert reborn.record(cfg, _ok()) is False

    def test_corrupt_state_fails_open_to_zero(self, tmp_path):
        (tmp_path / "budget.json").write_text("{not json")
        ledger, _ = _ledger(tmp_path)
        assert ledger.exhausted(_cfg(rpd=1)) is False

    def test_unlimited_provider_never_touches_the_state_file(self, tmp_path):
        """Empty-state no-op: no limits configured anywhere -> the file is
        never created, and record() is a pure no-op."""
        ledger, _ = _ledger(tmp_path)
        for _ in range(5):
            ledger.record(_cfg(), _ok(10, 10))
        assert not (tmp_path / "budget.json").exists()

    def test_persist_false_never_writes(self, tmp_path):
        """WS-3c single-writer rule: an MCP-side ledger loads but never
        writes the shared state file."""
        ledger, _ = _ledger(tmp_path, persist=False)
        ledger.record(_cfg(rpd=1), _ok())
        assert not (tmp_path / "budget.json").exists()

    def test_kill_switch_makes_ledger_inert(self, tmp_path, monkeypatch):
        ledger, _ = _ledger(tmp_path)
        cfg = _cfg(rpd=1)
        ledger.record(cfg, _ok())
        assert ledger.exhausted(cfg) is True
        monkeypatch.setenv("GENESIS_DAILY_BUDGET_DISABLED", "1")
        assert ledger.exhausted(cfg) is False
        assert ledger.record(cfg, _ok()) is False


class TestRouterIntegration:
    def _stack(self, tmp_path, *, providers, chain, event_bus=None):
        config = RoutingConfig(
            providers=providers,
            call_sites={"site": CallSiteConfig(id="site", chain=chain)},
            retry_profiles={
                "default": RetryPolicy(max_retries=0, base_delay_ms=1, jitter_pct=0.0),
            },
        )
        ledger = DailyBudgetLedger(
            state_path=tmp_path / "budget.json",
            clock=lambda: datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
        )
        delegate = MockDelegate()
        router = Router(
            config=config,
            breakers=CircuitBreakerRegistry(providers, persist=False),
            cost_tracker=NullCostTracker(),
            degradation=DegradationTracker(),
            delegate=delegate,
            daily_budget=ledger,
            event_bus=event_bus,
        )
        return router, ledger, delegate

    @pytest.mark.asyncio
    async def test_chain_walk_skips_an_exhausted_provider(self, tmp_path):
        first = _cfg("first", rpd=1)
        second = _cfg("second")
        router, ledger, delegate = self._stack(
            tmp_path, providers={"first": first, "second": second},
            chain=["first", "second"],
        )
        ledger.record(first, _ok())  # spend first's whole budget
        result = await router.route_call("site", [{"role": "user", "content": "x"}])
        assert result.success is True
        assert result.provider_used == "second"
        assert [c["provider"] for c in delegate.calls] == ["second"]

    @pytest.mark.asyncio
    async def test_an_all_exhausted_walk_names_the_provider_and_the_reason(
        self, tmp_path
    ):
        """The journal message is built from `skipped`, not `failed_providers`.

        A budget-deselected provider was appended only to `failed_providers`,
        so a walk where every provider was exhausted rendered as
        "0 attempted of N walkable" — no provider named, no reason given.
        That message is what this codebase relies on to diagnose why nothing
        was called, and it was silent in exactly the case it exists for.
        (Codex P2, PR #1624.)
        """
        bus = _RecordingBus()
        only = _cfg("only", rpd=1)
        router, ledger, delegate = self._stack(
            tmp_path, providers={"only": only}, chain=["only"], event_bus=bus,
        )
        ledger.record(only, _ok())  # spend the whole budget

        result = await router.route_call("site", [{"role": "user", "content": "x"}])

        assert result.success is False
        assert delegate.calls == [], "an exhausted provider was still called"
        # The diagnosis lives on the JOURNAL event, not on the result — that is
        # the message this codebase reads to find out why nothing was called.
        exhausted = [e for e in bus.events if e["type"] == "all_exhausted"]
        assert exhausted, f"no all_exhausted event was emitted: {bus.events}"
        msg = exhausted[-1]["message"]
        assert "only" in msg, (
            f"the exhausted provider is not named in the journal: {msg!r}"
        )
        assert "budget" in msg.lower(), (
            f"the budget reason is not stated in the journal: {msg!r}"
        )

    @pytest.mark.asyncio
    async def test_a_post_gate_deselection_is_named_in_the_journal(self, tmp_path):
        """The RECHECK branch is the harder one to reach, so it is the one
        that must not be silent — and it was the untested one: deleting only
        the post-gate `skipped.append` left all tests green while the pre-gate
        deletion was caught.

        Reaches it the way production does: the budget is NOT spent when the
        pre-gate check runs, and a concurrent caller crosses it while this
        turn is queued in the rate gate.
        """
        bus = _RecordingBus()
        only = _cfg("only", rpd=1)
        router, ledger, delegate = self._stack(
            tmp_path, providers={"only": only}, chain=["only"], event_bus=bus,
        )
        real_acquire = router._rate_gates.acquire

        async def _someone_else_spends_it_mid_gate(name, *a, **kw):
            out = await real_acquire(name, *a, **kw)
            ledger.record(only, _ok())
            return out

        router._rate_gates.acquire = _someone_else_spends_it_mid_gate
        result = await router.route_call("site", [{"role": "user", "content": "x"}])

        assert result.success is False
        assert delegate.calls == [], "an exhausted provider was still called"
        events = [e for e in bus.events if e["type"] == "all_exhausted"]
        assert events, f"no all_exhausted event: {bus.events}"
        msg = events[-1]["message"]
        assert "after rate gate" in msg, (
            f"a POST-GATE deselection reached the journal with no cause: {msg!r}"
        )

    @pytest.mark.asyncio
    async def test_the_budget_is_rechecked_after_the_rate_gate(self, tmp_path):
        """The first check happens BEFORE a sleep that can last seconds.

        `ProviderRateGate.acquire()` queues concurrent callers, so several can
        pass a not-yet-exhausted budget, wait, and each resume into the delegate
        after an earlier one has already crossed the limit — the classic
        check-then-act race, with a real sleep in the middle (Codex P2,
        PR #1624). The crossing is simulated INSIDE the gate here, which is
        exactly where a concurrent caller's `record` would land.

        The first check is still worth having: it avoids sleeping for a provider
        we will not call. It is simply not the last word.
        """
        first = _cfg("first", rpd=1)
        second = _cfg("second")
        router, ledger, delegate = self._stack(
            tmp_path, providers={"first": first, "second": second},
            chain=["first", "second"],
        )
        real_acquire = router._rate_gates.acquire

        async def _acquire_then_someone_else_spends_it(name, *a, **kw):
            out = await real_acquire(name, *a, **kw)
            if name == "first":
                ledger.record(first, _ok())  # a concurrent caller crosses it
            return out

        router._rate_gates.acquire = _acquire_then_someone_else_spends_it
        result = await router.route_call("site", [{"role": "user", "content": "x"}])

        assert result.success is True
        assert [c["provider"] for c in delegate.calls] == ["second"], (
            "the call went to a provider whose budget was spent while it waited "
            "in the rate gate — the pre-gate check was treated as final"
        )

    @pytest.mark.asyncio
    async def test_a_provider_within_budget_still_passes_the_recheck(self, tmp_path):
        """CONTROL. The recheck must not become a second way to reject a healthy
        provider — without this, deleting the `continue` body would pass the test
        above for the wrong reason."""
        first = _cfg("first", rpd=10)
        router, ledger, delegate = self._stack(
            tmp_path, providers={"first": first}, chain=["first"],
        )
        result = await router.route_call("site", [{"role": "user", "content": "x"}])
        assert result.success is True
        assert [c["provider"] for c in delegate.calls] == ["first"]

    @pytest.mark.asyncio
    async def test_route_records_the_visit(self, tmp_path):
        first = _cfg("first", rpd=10, tpd=1000)
        router, ledger, delegate = self._stack(
            tmp_path, providers={"first": first}, chain=["first"],
        )
        result = await router.route_call("site", [{"role": "user", "content": "x"}])
        assert result.success is True
        assert ledger.status(first)["requests_used"] == 1

    @pytest.mark.asyncio
    async def test_unbudgeted_routing_is_unchanged(self, tmp_path):
        """No limits configured -> identical routing, no state file."""
        first = _cfg("first")
        router, ledger, delegate = self._stack(
            tmp_path, providers={"first": first}, chain=["first"],
        )
        result = await router.route_call("site", [{"role": "user", "content": "x"}])
        assert result.success is True
        assert result.provider_used == "first"
        assert not (tmp_path / "budget.json").exists()


class _RecordingBus:
    """Minimal event-bus double implementing the emit contract the router
    uses (positional subsystem/severity/type/message + detail kwargs)."""

    def __init__(self):
        self.events = []

    async def emit(self, subsystem, severity, event_type, message, **details):
        self.events.append(
            {"type": event_type, "message": message, "details": details},
        )


class TestExhaustionEvent:
    @pytest.mark.asyncio
    async def test_crossing_emits_exactly_one_unit_named_event(self, tmp_path):
        """WIRING: the router must actually deliver provider.budget_exhausted
        at the crossing — once, with the spent unit named — and stay silent
        on the already-exhausted calls after it."""
        bus = _RecordingBus()
        first = _cfg("first", rpd=1)
        second = _cfg("second")
        router, ledger, delegate = TestRouterIntegration()._stack(
            tmp_path, providers={"first": first, "second": second},
            chain=["first", "second"], event_bus=bus,
        )
        msg = [{"role": "user", "content": "x"}]
        await router.route_call("site", msg)   # spends first's budget: crossing
        await router.route_call("site", msg)   # first now skipped
        await router.route_call("site", msg)
        budget_events = [e for e in bus.events if e["type"] == "provider.budget_exhausted"]
        assert len(budget_events) == 1
        event = budget_events[0]
        assert event["details"]["provider"] == "first"
        assert "requests 1/1" in event["message"]
        assert "tokens" not in event["message"]  # no tpd_limit -> unit not named
        assert event["details"]["exhausted"] is True


class TestParseValidationAndPeek:
    def test_string_limits_from_env_expansion_coerce(self):
        from genesis.routing.config import _parse_daily_limit
        assert _parse_daily_limit("p", "rpd_limit", "1000") == 1000
        assert _parse_daily_limit("p", "rpd_limit", None) is None

    def test_garbage_and_nonpositive_limits_are_rejected_at_parse(self):
        """A bad limit must die at config load, not inside the router's
        chain walk (where it would fail every call through the chain)."""
        from genesis.routing.config import _parse_daily_limit
        with pytest.raises(ValueError, match="must be an integer"):
            _parse_daily_limit("p", "tpd_limit", "lots")
        with pytest.raises(ValueError, match="must be positive"):
            _parse_daily_limit("p", "rpd_limit", 0)
        with pytest.raises(ValueError, match="must be positive"):
            _parse_daily_limit("p", "tpd_limit", -5)

    def test_reads_never_mutate_or_write(self, tmp_path):
        """status()/exhausted() are cross-thread readers (the dashboard route
        runs on a Flask worker thread) — they must not insert counter rows or
        touch the state file."""
        ledger, _ = _ledger(tmp_path)
        cfg = _cfg(rpd=5)
        for _ in range(3):
            assert ledger.exhausted(cfg) is False
            assert ledger.status(cfg)["requests_used"] == 0
        assert ledger._counters == {}
        assert not (tmp_path / "budget.json").exists()


# --- the ledger's authority: counted right, stored right, read at the right
# --- time (Codex P2 x4, PR #1624)


class TestATimeoutDoesNotSpendTheBudget:
    """`litellm_delegate` returns 408 for BOTH its timeout paths, including the
    LOCAL deadline where Genesis gave up and the provider may never have been
    asked. Counting those spends a vendor's daily allowance on requests it did
    not serve, so a flaky network deselects a healthy free provider for the rest
    of the UTC day — and undetectably, because the provider just stops being
    chosen."""

    def test_a_timeout_is_not_counted(self, tmp_path):
        ledger, _ = _ledger(tmp_path)
        cfg = _cfg(rpd=2)
        for _ in range(5):
            ledger.record(cfg, _fail(408))
        assert not ledger.exhausted(cfg), "timeouts exhausted a 2-request budget"

    def test_a_rate_limit_is_still_not_counted(self, tmp_path):
        """CONTROL on the pre-existing exclusion — 408 was added beside 429, not
        instead of it."""
        ledger, _ = _ledger(tmp_path)
        cfg = _cfg(rpd=2)
        for _ in range(5):
            ledger.record(cfg, _fail(429))
        assert not ledger.exhausted(cfg)

    def test_a_transport_failure_is_not_counted_despite_its_500(self, tmp_path):
        """A statusless exception — DNS, socket, TLS — is reported by the
        delegate as a SYNTHESIZED 500, indistinguishable from a real server
        error by status alone. Counting it spends the vendor's allowance on a
        request it never saw, so repeated connection failures would deselect a
        usable provider for the rest of the day — the OVERCOUNT direction this
        module's invariant forbids. (Codex P2, PR #1624.)
        """
        ledger, _ = _ledger(tmp_path)
        cfg = _cfg(rpd=2)
        for _ in range(5):
            ledger.record(cfg, _fail(500, reached=False))
        assert not ledger.exhausted(cfg), (
            "transport failures the provider never saw exhausted its daily budget"
        )

    def test_a_real_server_error_IS_counted(self, tmp_path):
        """CONTROL on the other side, and it is what keeps the exclusion honest:
        a 500 means the vendor took the request. Widening the exclusion set until
        nothing counts would pass both tests above and make the ledger inert."""
        ledger, _ = _ledger(tmp_path)
        cfg = _cfg(rpd=2)
        for _ in range(2):
            ledger.record(cfg, _fail(500))
        assert ledger.exhausted(cfg)


class TestTheLedgerLivesUnderGenesisHome:
    """GENESIS_HOME exists precisely so two installs can share a Unix account.
    An import-time `Path.home() / ".genesis"` ignores it, so both would read and
    overwrite ONE ledger — each seeing the other's requests against its own
    limits and deselecting providers it had barely used."""

    def test_the_path_follows_genesis_home(self, tmp_path, monkeypatch):
        import genesis.routing.daily_budget as mod

        monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "install-a"))
        assert mod._state_file().parent == tmp_path / "install-a"
        monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "install-b"))
        assert mod._state_file().parent == tmp_path / "install-b", (
            "the path was frozen — a second install shares the first's ledger"
        )

    def test_two_installs_do_not_share_a_ledger(self, tmp_path, monkeypatch):
        """The consequence, end to end: exhausting install A must leave install
        B untouched."""
        import genesis.routing.daily_budget as mod

        cfg = _cfg(rpd=1)
        monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "a"))
        (tmp_path / "a").mkdir()
        a = DailyBudgetLedger(state_path=mod._state_file())
        a.record(cfg, _ok())
        assert a.exhausted(cfg)

        monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "b"))
        (tmp_path / "b").mkdir()
        b = DailyBudgetLedger(state_path=mod._state_file())
        assert not b.exhausted(cfg), "install B inherited install A's spend"
class TestADamagedRowCannotBuyExtraBudget:
    """A persisted counter is only trustworthy if it is a COUNT.

    `isinstance(x, int)` was the whole check, and it admits two values that
    defeat the budget this ledger exists to enforce. A NEGATIVE count offsets
    later increments, so a row carrying `"requests": -1000` lets the provider
    run a thousand calls past its configured limit before `exhausted()` turns
    true. And `isinstance(True, int)` is True, so a JSON boolean is accepted
    and then behaves as 1 — the same trap `_parse_daily_limit` already guards
    on the config side. (CodeRabbit Major, PR #1624.)

    Discarding the row rather than repairing it is the fail-open direction
    this class already takes for an unreadable file: start that provider from
    zero, never silence it for a day on bad data.
    """

    @staticmethod
    def _write_state(tmp_path, entry: dict):
        import json as _json

        # Must match the path `_ledger` constructs, or the row is written
        # where nothing reads it and every assertion below is vacuous.
        path = tmp_path / "budget.json"
        path.write_text(_json.dumps({"providers": {"groq-ish": entry}}))
        return path

    def test_a_negative_count_does_not_extend_the_limit(self, tmp_path):
        self._write_state(tmp_path, {"day": "2026-09-02", "requests": -1000, "tokens": 0})
        ledger, _ = _ledger(tmp_path)
        cfg = _cfg(rpd=2)
        for _ in range(2):
            ledger.record(cfg, _ok())
        assert ledger.exhausted(cfg), (
            "a negative persisted counter bought extra calls past the daily limit"
        )

    def test_a_boolean_count_is_not_read_as_one(self, tmp_path):
        self._write_state(tmp_path, {"day": "2026-09-02", "requests": True, "tokens": 0})
        ledger, _ = _ledger(tmp_path)
        cfg = _cfg(rpd=2)
        # Discarded -> starts at 0, so exactly the limit is still available.
        ledger.record(cfg, _ok())
        assert not ledger.exhausted(cfg)
        ledger.record(cfg, _ok())
        assert ledger.exhausted(cfg)

    def test_a_valid_row_is_still_loaded(self, tmp_path):
        # CONTROL: a validator that rejected everything would pass both tests
        # above and make persistence inert across restarts.
        self._write_state(tmp_path, {"day": "2026-09-02", "requests": 2, "tokens": 0})
        ledger, _ = _ledger(tmp_path)
        cfg = _cfg(rpd=2)
        assert ledger.exhausted(cfg), "a VALID persisted row was discarded"

    def test_a_negative_token_count_does_not_extend_the_limit(self, tmp_path):
        """TOKENS is the unit groq actually caps (`tpd_limit: 200000`), and it
        was the untested half: mutating the validator to `("requests",)` left
        all 32 tests green. The module's own opening line says Groq caps
        tokens per day, so the unvalidated half was the live one.
        """
        self._write_state(tmp_path, {"day": "2026-09-02", "requests": 0, "tokens": -1000})
        ledger, _ = _ledger(tmp_path)
        cfg = _cfg(tpd=100)
        ledger.record(cfg, _ok(50, 50))
        assert ledger.exhausted(cfg), (
            "a negative persisted TOKEN counter bought extra tokens past the limit"
        )

    def test_one_damaged_counter_does_not_refund_its_sibling(self, tmp_path):
        """The INVERSE defect, and the first version of this fix had it.

        Rejecting the whole row on one bad field discards the other field's
        valid spend, handing the provider a fresh allowance on a day it
        already spent — the same over-spend the validator exists to prevent,
        in the other unit. MEASURED before the fix: a row with 190000 tokens
        spent and a damaged `requests` loaded as tokens_used=0.
        """
        self._write_state(
            tmp_path, {"day": "2026-09-02", "requests": -1, "tokens": 190_000}
        )
        ledger, _ = _ledger(tmp_path)
        cfg = _cfg(rpd=1000, tpd=200_000)
        status = ledger.status(cfg)
        assert status["tokens_used"] == 190_000, (
            "the damaged requests counter wiped a valid 190k token spend — "
            "the provider gets a fresh daily allowance it already used"
        )
        assert status["requests_used"] == 0, "the damaged counter should zero"

    def test_a_row_that_is_not_a_counter_row_is_dropped_whole(self, tmp_path):
        # CONTROL on the per-field repair: without `day` there is no row to
        # repair, and per-field salvage must not resurrect one.
        self._write_state(tmp_path, {"requests": 999, "tokens": 190_000})
        ledger, _ = _ledger(tmp_path)
        cfg = _cfg(rpd=1000, tpd=200_000)
        status = ledger.status(cfg)
        assert status["requests_used"] == 0 and status["tokens_used"] == 0

    def test_a_non_string_day_never_yields_stale_counters(self, tmp_path):
        """Pins the OUTCOME, and says plainly which mechanism it does not pin.

        Two things independently produce it: `_sanitized_counters` rejects a
        non-string `day`, and `_peek` compares `day` for equality with
        today's date string, which a non-string can never satisfy. Mutating
        the type check away leaves this green — measured — so this test does
        NOT cover that check; the equality comparison alone carries it.

        Kept anyway, because the property is the one that matters: a row with
        a malformed day must not contribute counters to today. Recorded as a
        behaviourally-redundant guard rather than left looking like coverage.
        """
        self._write_state(tmp_path, {"day": 20260902, "requests": 999, "tokens": 5})
        ledger, _ = _ledger(tmp_path)
        cfg = _cfg(rpd=1000, tpd=200_000)
        status = ledger.status(cfg)
        assert status["requests_used"] == 0 and status["tokens_used"] == 0, (
            "a row with a non-string `day` was loaded as a counter row"
        )
