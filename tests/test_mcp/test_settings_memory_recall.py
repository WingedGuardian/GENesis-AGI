"""memory_recall settings domain: registration + validator (graph expansion).

Mirrors ``test_settings_ws3_immunity.py`` — the domain is read live per call
by ``genesis.memory.graph_expansion`` (needs_restart=False), and the
validator must reject anything ``load_recall_config`` would misread, so a
``settings_update`` can never write a config that degrades-with-warning.
"""

from __future__ import annotations

from genesis.mcp.health.settings import (
    _DOMAIN_REGISTRY,
    _DOMAIN_VALIDATORS,
    _validate_memory_recall,
)


def test_memory_recall_domain_registered():
    assert "memory_recall" in _DOMAIN_REGISTRY
    d = _DOMAIN_REGISTRY["memory_recall"]
    assert d.readonly is False
    assert d.needs_restart is False  # read live per-call by graph_expansion
    assert d.config_filename == "memory_recall.yaml"
    assert "memory_recall" in _DOMAIN_VALIDATORS


def test_valid_changes_pass():
    assert _validate_memory_recall({"enabled": True}) == []
    assert (
        _validate_memory_recall(
            {
                "graph_expansion": {
                    "mode": "live",
                    "max_neighbors": 10,
                    "proactive_max_neighbors": 2,
                    "exclude_link_types": ["contradicts"],
                },
            },
        )
        == []
    )
    assert _validate_memory_recall({"entity_lane": {"mode": "off"}}) == []


def test_unknown_top_key_rejected():
    errs = _validate_memory_recall({"graph_expansions": {"mode": "live"}})
    assert errs and "graph_expansions" in errs[0]


def test_invalid_mode_rejected():
    errs = _validate_memory_recall({"graph_expansion": {"mode": "enforce"}})
    assert errs
    # entity_lane has no "live" implementation yet — off/shadow only for now
    assert _validate_memory_recall({"entity_lane": {"mode": "banana"}})


def test_enabled_must_be_bool():
    assert _validate_memory_recall({"enabled": "yes"})


def test_caps_must_be_bounded_ints():
    assert _validate_memory_recall({"graph_expansion": {"max_neighbors": -1}})
    assert _validate_memory_recall({"graph_expansion": {"max_neighbors": 26}})
    assert _validate_memory_recall({"graph_expansion": {"max_neighbors": True}})
    assert _validate_memory_recall({"graph_expansion": {"proactive_max_neighbors": "2"}})
    assert _validate_memory_recall({"graph_expansion": {"max_neighbors": 0}}) == []
    assert _validate_memory_recall({"graph_expansion": {"max_neighbors": 25}}) == []


def test_exclude_link_types_must_be_str_list():
    assert _validate_memory_recall({"graph_expansion": {"exclude_link_types": "contradicts"}})
    assert _validate_memory_recall({"graph_expansion": {"exclude_link_types": [1]}})
    assert _validate_memory_recall({"graph_expansion": {"exclude_link_types": []}}) == []


def test_section_must_be_mapping():
    assert _validate_memory_recall({"graph_expansion": "live"})
    assert _validate_memory_recall({"entity_lane": []})
