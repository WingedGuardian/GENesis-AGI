"""Tests for Router.reload_config()."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.routing.circuit_breaker import CircuitBreakerRegistry
from genesis.routing.types import (
    CallSiteConfig,
    ProviderConfig,
    RetryPolicy,
    RoutingConfig,
)


def _make_config(sites=None, providers=None):
    providers = providers or {
        "prov_a": ProviderConfig(name="prov_a", provider_type="test", model_id="m1", is_free=True, rpm_limit=None, open_duration_s=60),
        "prov_b": ProviderConfig(name="prov_b", provider_type="test", model_id="m2", is_free=False, rpm_limit=None, open_duration_s=60),
    }
    sites = sites or {
        "site_1": CallSiteConfig(id="site_1", chain=["prov_a", "prov_b"]),
    }
    return RoutingConfig(
        providers=providers,
        call_sites=sites,
        retry_profiles={"default": RetryPolicy()},
    )


def test_reload_config_swaps():
    from genesis.routing.router import Router

    old_config = _make_config()
    breakers = CircuitBreakerRegistry(old_config.providers)
    router = Router(
        config=old_config,
        breakers=breakers,
        cost_tracker=MagicMock(),
        degradation=MagicMock(),
        delegate=AsyncMock(),
    )

    new_config = _make_config(sites={
        "site_1": CallSiteConfig(id="site_1", chain=["prov_b", "prov_a"]),
        "site_2": CallSiteConfig(id="site_2", chain=["prov_a"]),
    })

    router.reload_config(new_config)

    assert router.config is new_config
    assert "site_2" in router.config.call_sites
    assert list(router.config.call_sites["site_1"].chain) == ["prov_b", "prov_a"]


def test_reload_registers_new_providers():
    from genesis.routing.router import Router

    old_config = _make_config()
    breakers = CircuitBreakerRegistry(old_config.providers)
    router = Router(
        config=old_config,
        breakers=breakers,
        cost_tracker=MagicMock(),
        degradation=MagicMock(),
        delegate=AsyncMock(),
    )

    new_providers = {
        **old_config.providers,
        "prov_c": ProviderConfig(name="prov_c", provider_type="test", model_id="m3", is_free=True, rpm_limit=None, open_duration_s=60),
    }
    new_config = _make_config(providers=new_providers)
    router.reload_config(new_config)

    # prov_c should now have a circuit breaker
    cb = breakers.get("prov_c")
    assert cb is not None
    assert cb.is_available()


# ── DLQ orphan scan on reload ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scan_dlq_orphans_after_reload_expires_provider_orphans():
    """After reload, pending DLQ items for dropped providers must expire."""
    import aiosqlite

    from genesis.db.schema import create_all_tables
    from genesis.routing.dead_letter import DeadLetterQueue
    from genesis.routing.router import Router

    async with aiosqlite.connect(":memory:") as db:
        db.row_factory = aiosqlite.Row
        await create_all_tables(db)

        dlq = DeadLetterQueue(db)
        # Enqueue three items: one for prov_a (stays), one for prov_b
        # (will be dropped from config), one for prov_b again.
        await dlq.enqueue(
            operation_type="route_call",
            payload={"call_site_id": "site_1", "messages": []},
            target_provider="prov_a",
            failure_reason="provider down",
        )
        dropped_id_1 = await dlq.enqueue(
            operation_type="route_call",
            payload={"call_site_id": "site_1", "messages": []},
            target_provider="prov_b",
            failure_reason="provider down",
        )
        dropped_id_2 = await dlq.enqueue(
            operation_type="route_call",
            payload={"call_site_id": "site_1", "messages": []},
            target_provider="prov_b",
            failure_reason="provider down",
        )

        # Build router with the full config including both providers.
        old_config = _make_config()
        breakers = CircuitBreakerRegistry(old_config.providers)
        router = Router(
            config=old_config,
            breakers=breakers,
            cost_tracker=MagicMock(),
            degradation=MagicMock(),
            delegate=AsyncMock(),
            dead_letter=dlq,
        )

        # Reload with ONLY prov_a — prov_b is now removed from config.
        new_config = _make_config(providers={
            "prov_a": ProviderConfig(
                name="prov_a", provider_type="test", model_id="m1",
                is_free=True, rpm_limit=None, open_duration_s=60,
            ),
        })
        router.reload_config(new_config)

        # Scan should expire both prov_b items and leave prov_a untouched.
        count = await router.scan_dlq_orphans_after_reload()
        assert count == 2

        # Verify statuses in DB.
        from genesis.db.crud import dead_letter as dl_crud
        pending = await dl_crud.query_pending(db)
        pending_ids = {p["id"] for p in pending}
        assert dropped_id_1 not in pending_ids
        assert dropped_id_2 not in pending_ids
        # prov_a item should still be pending.
        assert len(pending_ids) == 1


@pytest.mark.asyncio
async def test_scan_dlq_orphans_after_reload_is_idempotent():
    """Running the scan twice with no config change must expire zero extras."""
    import aiosqlite

    from genesis.db.schema import create_all_tables
    from genesis.routing.dead_letter import DeadLetterQueue
    from genesis.routing.router import Router

    async with aiosqlite.connect(":memory:") as db:
        db.row_factory = aiosqlite.Row
        await create_all_tables(db)

        dlq = DeadLetterQueue(db)
        await dlq.enqueue(
            operation_type="route_call",
            payload={"call_site_id": "site_1", "messages": []},
            target_provider="prov_gone",
            failure_reason="provider down",
        )

        old_config = _make_config()
        breakers = CircuitBreakerRegistry(old_config.providers)
        router = Router(
            config=old_config,
            breakers=breakers,
            cost_tracker=MagicMock(),
            degradation=MagicMock(),
            delegate=AsyncMock(),
            dead_letter=dlq,
        )

        # First scan — prov_gone is not in config, so it's an orphan.
        assert await router.scan_dlq_orphans_after_reload() == 1
        # Second scan — already expired, nothing to do.
        assert await router.scan_dlq_orphans_after_reload() == 0


@pytest.mark.asyncio
async def test_scan_dlq_orphans_handles_large_batch_atomically():
    """Scan must expire ALL orphans in one shot, not cap at a pagination limit.

    Regression test for a code-review finding: earlier implementation
    used `query_pending` which defaults to LIMIT 50, silently leaving
    larger orphan batches to wait 72h for age-based expiry. The atomic
    SQL-side UPDATE ... RETURNING must handle arbitrary counts.
    """
    import aiosqlite

    from genesis.db.schema import create_all_tables
    from genesis.routing.dead_letter import DeadLetterQueue
    from genesis.routing.router import Router

    async with aiosqlite.connect(":memory:") as db:
        db.row_factory = aiosqlite.Row
        await create_all_tables(db)

        dlq = DeadLetterQueue(db)
        # Enqueue 150 orphans for a provider that will be dropped.
        for _ in range(150):
            await dlq.enqueue(
                operation_type="route_call",
                payload={"call_site_id": "site_1", "messages": []},
                target_provider="prov_gone",
                failure_reason="provider down",
            )
        # Plus 5 non-orphans that must survive.
        for _ in range(5):
            await dlq.enqueue(
                operation_type="route_call",
                payload={"call_site_id": "site_1", "messages": []},
                target_provider="prov_a",
                failure_reason="provider down",
            )

        old_config = _make_config()
        breakers = CircuitBreakerRegistry(old_config.providers)
        router = Router(
            config=old_config,
            breakers=breakers,
            cost_tracker=MagicMock(),
            degradation=MagicMock(),
            delegate=AsyncMock(),
            dead_letter=dlq,
        )

        # Reload with only prov_a — 150 orphans should expire in one call.
        new_config = _make_config(providers={
            "prov_a": ProviderConfig(
                name="prov_a", provider_type="test", model_id="m1",
                is_free=True, rpm_limit=None, open_duration_s=60,
            ),
        })
        router.reload_config(new_config)

        count = await router.scan_dlq_orphans_after_reload()
        assert count == 150, (
            f"Expected all 150 orphans expired in one scan (regression: "
            f"earlier code capped at LIMIT 50), got {count}"
        )

        # 5 non-orphans still pending.
        from genesis.db.crud import dead_letter as dl_crud
        pending = await dl_crud.query_pending(db, limit=1000)
        assert len(pending) == 5
        assert all(p["target_provider"] == "prov_a" for p in pending)


@pytest.mark.asyncio
async def test_scan_dlq_orphans_no_dlq_wired_returns_zero():
    """Router without a DLQ returns 0 orphans safely."""
    old_config = _make_config()
    breakers = CircuitBreakerRegistry(old_config.providers)

    from genesis.routing.router import Router
    router = Router(
        config=old_config,
        breakers=breakers,
        cost_tracker=MagicMock(),
        degradation=MagicMock(),
        delegate=AsyncMock(),
        # no dead_letter passed — defaults to None
    )

    assert await router.scan_dlq_orphans_after_reload() == 0
