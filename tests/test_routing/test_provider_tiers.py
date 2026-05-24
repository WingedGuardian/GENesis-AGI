"""Tests for genesis.routing.provider_tiers."""

from __future__ import annotations

from genesis.routing.provider_tiers import (
    PROVIDER_TIERS,
    ProviderTier,
    get_display_name,
    get_tier,
)


class TestProviderTier:
    """ProviderTier enum."""

    def test_ordering(self) -> None:
        assert ProviderTier.INFO < ProviderTier.WARNING < ProviderTier.CRITICAL

    def test_values(self) -> None:
        assert ProviderTier.INFO == 1
        assert ProviderTier.WARNING == 2
        assert ProviderTier.CRITICAL == 3


class TestGetTier:
    """get_tier() lookup."""

    def test_embeddings_is_critical(self) -> None:
        assert get_tier("episodic_memory_embedding") == ProviderTier.CRITICAL

    def test_qdrant_search_is_critical(self) -> None:
        assert get_tier("qdrant.search") == ProviderTier.CRITICAL

    def test_qdrant_upsert_is_critical(self) -> None:
        assert get_tier("qdrant.upsert") == ProviderTier.CRITICAL

    def test_web_search_is_warning(self) -> None:
        assert get_tier("web_search") == ProviderTier.WARNING

    def test_web_fetch_is_warning(self) -> None:
        assert get_tier("web_fetch") == ProviderTier.WARNING

    def test_unknown_defaults_to_info(self) -> None:
        assert get_tier("some_random_provider") == ProviderTier.INFO

    def test_empty_string_defaults_to_info(self) -> None:
        assert get_tier("") == ProviderTier.INFO


class TestGetDisplayName:
    """get_display_name() lookup."""

    def test_known_provider(self) -> None:
        assert get_display_name("episodic_memory_embedding") == "Embeddings"

    def test_qdrant(self) -> None:
        assert get_display_name("qdrant.search") == "Qdrant Search"

    def test_unknown_returns_raw_name(self) -> None:
        assert get_display_name("my_custom_provider") == "my_custom_provider"


class TestTierRegistry:
    """PROVIDER_TIERS registry consistency."""

    def test_all_entries_are_valid_tiers(self) -> None:
        for name, tier in PROVIDER_TIERS.items():
            assert isinstance(tier, ProviderTier), f"{name} has invalid tier {tier}"

    def test_has_at_least_one_critical(self) -> None:
        critical = [n for n, t in PROVIDER_TIERS.items() if t == ProviderTier.CRITICAL]
        assert len(critical) >= 1, "No CRITICAL tier providers defined"

    def test_has_at_least_one_warning(self) -> None:
        warning = [n for n, t in PROVIDER_TIERS.items() if t == ProviderTier.WARNING]
        assert len(warning) >= 1, "No WARNING tier providers defined"
