"""Tests for fallback_chains — static obstacle resolution chains."""

from __future__ import annotations

from genesis.learning.fallback_chains import CHAINS, get_chain, get_next_method


def test_all_chains_are_non_empty():
    for name, chain in CHAINS.items():
        assert len(chain) > 0, f"Chain '{name}' is empty"


def test_get_chain_known():
    chain = get_chain("web_fetch")
    assert chain is not None
    assert chain[0] == "firecrawl"


def test_get_chain_unknown():
    assert get_chain("nonexistent_obstacle") is None


def test_get_next_method_first():
    method = get_next_method("web_fetch", [])
    assert method == "firecrawl"


def test_get_next_method_skips_failed():
    method = get_next_method("web_fetch", ["firecrawl"])
    assert method == "playwright"


def test_get_next_method_skips_multiple_failed():
    method = get_next_method("web_fetch", ["firecrawl", "playwright"])
    assert method == "requests_fallback"


def test_get_next_method_exhausted():
    chain = get_chain("web_fetch")
    assert chain is not None
    method = get_next_method("web_fetch", chain)
    assert method is None


def test_get_next_method_unknown_obstacle():
    assert get_next_method("nonexistent", []) is None


def test_permission_error_chain():
    chain = get_chain("permission_error")
    assert chain is not None
    assert "escalate_to_user" in chain


def test_model_unavailable_chain():
    chain = get_chain("model_unavailable")
    assert chain is not None
    assert chain[0] == "fallback_model"


def test_api_rate_limit_chain():
    method = get_next_method("api_rate_limit", ["wait_and_retry"])
    assert method == "alternative_provider"


def test_tool_failure_chain():
    chain = get_chain("tool_failure")
    assert chain is not None
    assert "report_gap" in chain
