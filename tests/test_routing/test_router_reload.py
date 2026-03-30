"""Tests for Router.reload_config()."""

from unittest.mock import AsyncMock, MagicMock

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
