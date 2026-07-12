"""Tests for credit exhaustion detection in health alerts."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@dataclass(frozen=True)
class _ProviderCfg:
    name: str = ""
    provider_type: str = ""
    model_id: str = ""
    is_free: bool = False
    rpm_limit: int | None = None
    open_duration_s: int = 30
    enabled: bool = True
    has_api_key: bool = True
    profile: str | None = None
    base_url: str | None = None
    keep_alive: str | int | None = None


@dataclass(frozen=True)
class _CallSiteCfg:
    id: str = ""
    chain: list[str] = field(default_factory=list)
    dispatch: str = "dual"
    default_paid: bool = False
    never_pays: bool = False
    retry_profile: str = "default"


@dataclass(frozen=True)
class _RoutingCfg:
    providers: dict = field(default_factory=dict)
    call_sites: dict = field(default_factory=dict)
    retry_profiles: dict = field(default_factory=dict)
    disabled_providers: dict = field(default_factory=dict)


def _mock_routing_config(*provider_names, sole=False):
    """Build a mock routing config where named providers are in chains.

    By default, adds a dummy fallback so providers are "active" not "sole".
    Set sole=True to make single-provider chains.
    """
    providers = {}
    for name in provider_names:
        providers[name] = _ProviderCfg(name=name, provider_type=name)
    if not sole:
        providers["_fallback"] = _ProviderCfg(name="_fallback", provider_type="_fallback")
    call_sites = {}
    for i, name in enumerate(provider_names):
        chain = [name] if sole else [name, "_fallback"]
        call_sites[f"site_{i}"] = _CallSiteCfg(id=f"site_{i}", chain=chain)
    return _RoutingCfg(providers=providers, call_sites=call_sites)


def _mock_routing_config_typed(*name_type_pairs, sole=False, is_free=False):
    """Routing config where provider NAME differs from provider_type.

    Reproduces the production reality that activity_log stores ``llm.<name>``
    while ``derive_criticality`` keys by ``provider_type`` -- the mismatch the
    two-hop resolution fixes. Each arg is a ``(name, provider_type)`` tuple.
    """
    providers = {}
    types = {t for _, t in name_type_pairs}
    for name, ptype in name_type_pairs:
        providers[name] = _ProviderCfg(name=name, provider_type=ptype, is_free=is_free)
    if not sole:
        providers["_fallback"] = _ProviderCfg(name="_fallback", provider_type="_fallback")
    call_sites = {}
    for i, ptype in enumerate(types):
        names_of_type = [n for n, t in name_type_pairs if t == ptype]
        chain = list(names_of_type) if sole else [*names_of_type, "_fallback"]
        call_sites[f"site_{i}"] = _CallSiteCfg(id=f"site_{i}", chain=chain)
    return _RoutingCfg(providers=providers, call_sites=call_sites)


def _make_mock_service(*, rows_recent=None, rows_baseline=None, provider_names=None,
                       routing_cfg=None):
    """Build a mock HealthDataService with a mock DB."""
    svc = MagicMock()
    svc._db = AsyncMock()
    svc._breakers = None
    svc._routing_config = None
    svc._provider_health = None

    # Mock the snapshot to return minimal data (no call sites, no infra, etc.)
    svc.snapshot = AsyncMock(return_value={
        "call_sites": {},
        "infrastructure": {},
        "cc_sessions": {},
        "resilience": {"level": "L0"},
        "queues": {},
    })

    # Set up DB execute responses for the credit exhaustion queries
    recent_cursor = AsyncMock()
    recent_cursor.fetchall = AsyncMock(return_value=rows_recent or [])

    baseline_cursor = AsyncMock()
    baseline_cursor.fetchone = AsyncMock(return_value=rows_baseline)

    # The DB gets called multiple times — we need to handle the
    # update-check queries too (they come after credit exhaustion)
    update_cursor = AsyncMock()
    update_cursor.fetchone = AsyncMock(return_value=None)

    call_count = 0

    async def mock_execute(query, params=None):
        nonlocal call_count
        call_count += 1
        if "FROM activity_log" in query and "GROUP BY provider" in query:
            return recent_cursor
        if "FROM activity_log" in query and "provider = ?" in query:
            return baseline_cursor
        return update_cursor

    svc._db.execute = mock_execute

    # Build a routing config with the provider names in chains so
    # derive_criticality() classifies them as active (not dormant).
    if routing_cfg is not None:
        mock_rt = MagicMock()
        mock_rt._routing_config = routing_cfg
        svc._mock_rt = mock_rt
        return svc
    if provider_names is None:
        provider_names = [r[0] for r in (rows_recent or [])]
    if provider_names:
        mock_rt = MagicMock()
        mock_rt._routing_config = _mock_routing_config(*provider_names)
        svc._mock_rt = mock_rt  # stash for patching

    return svc


@pytest.mark.asyncio
async def test_credit_exhaustion_detected():
    """Alert fires when a previously healthy provider starts failing."""
    # Provider had 100 calls, 2 errors over 7d (98% success)
    # Now has 20 calls, 15 errors in last hour (75% error rate)
    svc = _make_mock_service(
        rows_recent=[("deepinfra", 20, 15)],
        rows_baseline=(100, 2),
    )

    with patch("genesis.mcp.health_mcp._service", svc), \
         patch("genesis.mcp.health_mcp._activity_tracker", None), \
         patch("genesis.mcp.health_mcp._job_retry_registry", None), \
         patch("genesis.mcp.health_mcp._alert_history", {}), \
         patch("genesis.runtime.GenesisRuntime.instance", return_value=svc._mock_rt):

        from genesis.mcp.health.errors import _impl_health_alerts
        alerts = await _impl_health_alerts(active_only=True)

    credit_alerts = [a for a in alerts if "credit_exhaustion" in a.get("id", "")]
    assert len(credit_alerts) == 1
    alert = credit_alerts[0]
    assert alert["severity"] == "WARNING"  # sole provider = WARNING (is_free=False but active, not systemic)
    assert "deepinfra" in alert["id"]
    assert "credit" in alert["message"].lower() or "exhaustion" in alert["message"].lower()


@pytest.mark.asyncio
async def test_no_alert_when_baseline_was_unhealthy():
    """No alert if the provider was already failing in the 7-day baseline."""
    # Provider had 100 calls, 30 errors over 7d (70% success — already bad)
    svc = _make_mock_service(
        rows_recent=[("deepinfra", 20, 15)],
        rows_baseline=(100, 30),  # >5% baseline error rate
    )

    with patch("genesis.mcp.health_mcp._service", svc), \
         patch("genesis.mcp.health_mcp._activity_tracker", None), \
         patch("genesis.mcp.health_mcp._job_retry_registry", None), \
         patch("genesis.mcp.health_mcp._alert_history", {}), \
         patch("genesis.runtime.GenesisRuntime.instance", return_value=svc._mock_rt):

        from genesis.mcp.health.errors import _impl_health_alerts
        alerts = await _impl_health_alerts(active_only=True)

    credit_alerts = [a for a in alerts if "credit_exhaustion" in a.get("id", "")]
    assert len(credit_alerts) == 0


@pytest.mark.asyncio
async def test_no_alert_for_info_tier_provider():
    """INFO-tier providers don't trigger credit exhaustion alerts."""
    svc = _make_mock_service(
        rows_recent=[("some_random_provider", 20, 15)],
        rows_baseline=(100, 2),
    )

    with patch("genesis.mcp.health_mcp._service", svc), \
         patch("genesis.mcp.health_mcp._activity_tracker", None), \
         patch("genesis.mcp.health_mcp._job_retry_registry", None), \
         patch("genesis.mcp.health_mcp._alert_history", {}):

        from genesis.mcp.health.errors import _impl_health_alerts
        alerts = await _impl_health_alerts(active_only=True)

    credit_alerts = [a for a in alerts if "credit_exhaustion" in a.get("id", "")]
    assert len(credit_alerts) == 0


@pytest.mark.asyncio
async def test_warning_severity_for_active_provider():
    """Active (non-sole, non-systemic) providers get WARNING severity."""
    svc = _make_mock_service(
        rows_recent=[("web_search", 20, 15)],
        rows_baseline=(100, 2),
    )

    with patch("genesis.mcp.health_mcp._service", svc), \
         patch("genesis.mcp.health_mcp._activity_tracker", None), \
         patch("genesis.mcp.health_mcp._job_retry_registry", None), \
         patch("genesis.mcp.health_mcp._alert_history", {}), \
         patch("genesis.runtime.GenesisRuntime.instance", return_value=svc._mock_rt):

        from genesis.mcp.health.errors import _impl_health_alerts
        alerts = await _impl_health_alerts(active_only=True)

    credit_alerts = [a for a in alerts if "credit_exhaustion" in a.get("id", "")]
    assert len(credit_alerts) == 1
    assert credit_alerts[0]["severity"] == "WARNING"


@pytest.mark.asyncio
async def test_no_alert_when_recent_rate_is_low():
    """No alert if recent error rate is below 50%."""
    svc = _make_mock_service(
        rows_recent=[("deepinfra", 20, 5)],  # 25% error rate
        rows_baseline=(100, 2),
    )

    with patch("genesis.mcp.health_mcp._service", svc), \
         patch("genesis.mcp.health_mcp._activity_tracker", None), \
         patch("genesis.mcp.health_mcp._job_retry_registry", None), \
         patch("genesis.mcp.health_mcp._alert_history", {}), \
         patch("genesis.runtime.GenesisRuntime.instance", return_value=svc._mock_rt):

        from genesis.mcp.health.errors import _impl_health_alerts
        alerts = await _impl_health_alerts(active_only=True)

    credit_alerts = [a for a in alerts if "credit_exhaustion" in a.get("id", "")]
    assert len(credit_alerts) == 0


@pytest.mark.asyncio
async def test_no_alert_with_insufficient_baseline():
    """No alert if baseline has fewer than 10 calls."""
    svc = _make_mock_service(
        rows_recent=[("deepinfra", 20, 15)],
        rows_baseline=(5, 0),  # Only 5 baseline calls
    )

    with patch("genesis.mcp.health_mcp._service", svc), \
         patch("genesis.mcp.health_mcp._activity_tracker", None), \
         patch("genesis.mcp.health_mcp._job_retry_registry", None), \
         patch("genesis.mcp.health_mcp._alert_history", {}), \
         patch("genesis.runtime.GenesisRuntime.instance", return_value=svc._mock_rt):

        from genesis.mcp.health.errors import _impl_health_alerts
        alerts = await _impl_health_alerts(active_only=True)

    credit_alerts = [a for a in alerts if "credit_exhaustion" in a.get("id", "")]
    assert len(credit_alerts) == 0


@pytest.mark.asyncio
async def test_llm_prefixed_provider_resolves_via_two_hop():
    """PROD-FORMAT regression: activity_log stores ``llm.<name>`` but crit_map
    is keyed by provider_type. The detector must strip the ``llm.`` prefix and
    resolve name->provider_type->criticality (two-hop), else it is 100% dead.

    Fails on pre-fix code: ``crit_map.get("llm.groq-free")`` misses -> dormant.
    """
    svc = _make_mock_service(
        rows_recent=[("llm.groq-free", 20, 15)],
        rows_baseline=(100, 2),
        routing_cfg=_mock_routing_config_typed(("groq-free", "groq")),
    )

    with patch("genesis.mcp.health_mcp._service", svc), \
         patch("genesis.mcp.health_mcp._activity_tracker", None), \
         patch("genesis.mcp.health_mcp._job_retry_registry", None), \
         patch("genesis.mcp.health_mcp._alert_history", {}), \
         patch("genesis.runtime.GenesisRuntime.instance", return_value=svc._mock_rt):

        from genesis.mcp.health.errors import _impl_health_alerts
        alerts = await _impl_health_alerts(active_only=True)

    credit_alerts = [a for a in alerts if "credit_exhaustion" in a.get("id", "")]
    assert len(credit_alerts) == 1
    # alert_id / message use the RESOLVED bare name, not the raw llm.* string.
    assert credit_alerts[0]["id"] == "provider:credit_exhaustion:groq-free"
    assert "llm." not in credit_alerts[0]["id"]
    assert credit_alerts[0]["severity"] == "WARNING"


@pytest.mark.asyncio
async def test_non_llm_provider_row_is_skipped():
    """A non-routed provider string (embedding/qdrant/mcp.*) resolves to no
    provider config -> skipped cleanly, never alerts."""
    svc = _make_mock_service(
        rows_recent=[("embedding", 20, 15)],
        rows_baseline=(100, 2),
        routing_cfg=_mock_routing_config_typed(("groq-free", "groq")),
    )

    with patch("genesis.mcp.health_mcp._service", svc), \
         patch("genesis.mcp.health_mcp._activity_tracker", None), \
         patch("genesis.mcp.health_mcp._job_retry_registry", None), \
         patch("genesis.mcp.health_mcp._alert_history", {}), \
         patch("genesis.runtime.GenesisRuntime.instance", return_value=svc._mock_rt):

        from genesis.mcp.health.errors import _impl_health_alerts
        alerts = await _impl_health_alerts(active_only=True)

    credit_alerts = [a for a in alerts if "credit_exhaustion" in a.get("id", "")]
    assert len(credit_alerts) == 0


@pytest.mark.asyncio
async def test_sole_paid_provider_is_warning_not_critical():
    """Credit exhaustion is dashboard-WARNING ONLY -- never CRITICAL (which
    would page). A sole PAID provider used to escalate to CRITICAL; the
    redesign makes every credit-exhaustion alert WARNING (Sentinel can't
    refill credits; Telegram is reserved for genuine outages).

    Fails on pre-fix code: sole+paid -> CRITICAL.
    """
    svc = _make_mock_service(
        rows_recent=[("llm.openrouter-deepseek", 20, 15)],
        rows_baseline=(100, 2),
        routing_cfg=_mock_routing_config_typed(
            ("openrouter-deepseek", "openrouter"), sole=True, is_free=False
        ),
    )

    with patch("genesis.mcp.health_mcp._service", svc), \
         patch("genesis.mcp.health_mcp._activity_tracker", None), \
         patch("genesis.mcp.health_mcp._job_retry_registry", None), \
         patch("genesis.mcp.health_mcp._alert_history", {}), \
         patch("genesis.runtime.GenesisRuntime.instance", return_value=svc._mock_rt):

        from genesis.mcp.health.errors import _impl_health_alerts
        alerts = await _impl_health_alerts(active_only=True)

    credit_alerts = [a for a in alerts if "credit_exhaustion" in a.get("id", "")]
    assert len(credit_alerts) == 1
    assert credit_alerts[0]["severity"] == "WARNING"
