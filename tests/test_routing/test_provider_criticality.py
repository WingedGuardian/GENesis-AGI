"""Tests for dynamic provider criticality derivation."""

from __future__ import annotations

from dataclasses import dataclass, field

from genesis.routing.provider_criticality import (
    call_sites_for_provider_type,
    derive_criticality,
)


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


def _make_config(providers, call_sites, disabled=None):
    return _RoutingCfg(
        providers={p.name: p for p in providers},
        call_sites={cs.id: cs for cs in call_sites},
        disabled_providers=disabled or {},
    )


class TestCallSitesForProviderType:
    def test_finds_sites_using_type(self):
        config = _make_config(
            providers=[
                _ProviderCfg(name="groq-free", provider_type="groq", is_free=True),
                _ProviderCfg(name="gemini-free", provider_type="google", is_free=True),
            ],
            call_sites=[
                _CallSiteCfg(id="micro", chain=["groq-free", "gemini-free"]),
                _CallSiteCfg(id="light", chain=["gemini-free"]),
                _CallSiteCfg(id="deep", chain=["groq-free"]),
            ],
        )
        assert sorted(call_sites_for_provider_type(config, "groq")) == ["deep", "micro"]
        assert sorted(call_sites_for_provider_type(config, "google")) == ["light", "micro"]

    def test_excludes_cc_dispatched(self):
        config = _make_config(
            providers=[
                _ProviderCfg(name="openrouter-opus", provider_type="openrouter"),
            ],
            call_sites=[
                _CallSiteCfg(id="ego", chain=["openrouter-opus"], dispatch="cli"),
                _CallSiteCfg(id="compact", chain=["openrouter-opus"], dispatch="dual"),
            ],
        )
        result = call_sites_for_provider_type(config, "openrouter")
        assert result == ["compact"]

    def test_empty_for_unknown_type(self):
        config = _make_config(providers=[], call_sites=[])
        assert call_sites_for_provider_type(config, "nonexistent") == []


class TestDeriveCriticality:
    def test_sole_provider(self):
        config = _make_config(
            providers=[
                _ProviderCfg(name="or-deepseek-v4", provider_type="openrouter"),
            ],
            call_sites=[
                _CallSiteCfg(id="judge", chain=["or-deepseek-v4"]),
            ],
        )
        result = derive_criticality(config)
        assert result["openrouter"]["criticality"] == "sole"
        assert result["openrouter"]["sole_sites"] == ["judge"]

    def test_systemic_provider(self):
        config = _make_config(
            providers=[
                _ProviderCfg(name="groq-free", provider_type="groq", is_free=True),
                _ProviderCfg(name="other", provider_type="other"),
            ],
            call_sites=[
                _CallSiteCfg(id=f"site_{i}", chain=["groq-free", "other"])
                for i in range(12)
            ],
        )
        result = derive_criticality(config)
        assert result["groq"]["criticality"] == "systemic"
        assert result["groq"]["chain_count"] == 12
        assert result["groq"]["is_free"] is True

    def test_active_provider(self):
        config = _make_config(
            providers=[
                _ProviderCfg(name="deepinfra", provider_type="deepinfra"),
                _ProviderCfg(name="ollama", provider_type="ollama"),
            ],
            call_sites=[
                _CallSiteCfg(id="embed_write", chain=["ollama", "deepinfra"]),
                _CallSiteCfg(id="embed_read", chain=["deepinfra", "ollama"]),
            ],
        )
        result = derive_criticality(config)
        assert result["deepinfra"]["criticality"] == "active"
        assert result["deepinfra"]["chain_count"] == 2

    def test_dormant_provider(self):
        config = _make_config(
            providers=[
                _ProviderCfg(name="xai-grok", provider_type="xai"),
            ],
            call_sites=[
                _CallSiteCfg(id="micro", chain=["some-other"]),
            ],
        )
        result = derive_criticality(config)
        assert result["xai"]["criticality"] == "dormant"
        assert result["xai"]["chain_count"] == 0

    def test_disabled_providers_show_as_dormant(self):
        config = _make_config(
            providers=[],
            call_sites=[],
            disabled={"xai-grok": "xai"},
        )
        result = derive_criticality(config)
        assert "xai" in result
        assert result["xai"]["criticality"] == "dormant"

    def test_is_free_flag(self):
        config = _make_config(
            providers=[
                _ProviderCfg(name="groq-free", provider_type="groq", is_free=True),
                _ProviderCfg(name="openrouter-sonnet", provider_type="openrouter", is_free=False),
            ],
            call_sites=[],
        )
        result = derive_criticality(config)
        assert result["groq"]["is_free"] is True
        assert result["openrouter"]["is_free"] is False

    def test_mixed_free_paid_same_type(self):
        """If any provider of a type is paid, the type is not free."""
        config = _make_config(
            providers=[
                _ProviderCfg(name="or-free", provider_type="openrouter", is_free=True),
                _ProviderCfg(name="or-paid", provider_type="openrouter", is_free=False),
            ],
            call_sites=[],
        )
        result = derive_criticality(config)
        assert result["openrouter"]["is_free"] is False

    def test_sole_overrides_systemic(self):
        """If a type has a sole-provider chain AND 10+ chains, it's 'sole'."""
        config = _make_config(
            providers=[
                _ProviderCfg(name="or-deepseek", provider_type="openrouter"),
                _ProviderCfg(name="other", provider_type="other"),
            ],
            call_sites=[
                # 1 sole-provider chain
                _CallSiteCfg(id="judge", chain=["or-deepseek"]),
                # 11 multi-provider chains
                *[_CallSiteCfg(id=f"s{i}", chain=["or-deepseek", "other"]) for i in range(11)],
            ],
        )
        result = derive_criticality(config)
        assert result["openrouter"]["criticality"] == "sole"
        assert result["openrouter"]["chain_count"] == 12
