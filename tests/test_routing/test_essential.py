"""Tests for genesis.routing.essential."""

from __future__ import annotations

import logging
from types import SimpleNamespace

from genesis.routing.essential import (
    ESSENTIAL_CLOUD_SITES,
    build_essential_provider_map,
)


def _cfg(call_sites: dict[str, list[str]]):
    return SimpleNamespace(
        call_sites={
            sid: SimpleNamespace(chain=chain) for sid, chain in call_sites.items()
        }
    )


def test_build_map_includes_present_essential_sites_only():
    cfg = _cfg({
        "4_light_reflection": ["gemini-free", "groq-free"],
        "9_fact_extraction": ["groq-free", "gemini-free"],
        "13_morning_report": ["openrouter"],  # non-essential — must be ignored
    })
    m = build_essential_provider_map(cfg)
    assert m["4_light_reflection"] == ["gemini-free", "groq-free"]
    assert m["9_fact_extraction"] == ["groq-free", "gemini-free"]
    assert "13_morning_report" not in m


def test_build_map_warns_on_missing_site_no_silent_skip(caplog):
    # Attach caplog's handler directly to the module logger: Genesis's logging
    # config sets propagate=False, so relying on root propagation makes this
    # capture ordering-dependent across the suite.
    logger = logging.getLogger("genesis.routing.essential")
    logger.addHandler(caplog.handler)
    old_level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        m = build_essential_provider_map(_cfg({"4_light_reflection": ["gemini-free"]}))
    finally:
        logger.removeHandler(caplog.handler)
        logger.setLevel(old_level)
    assert set(m) == {"4_light_reflection"}
    # Every absent essential site must have produced a warning (no silent skip).
    warned_text = " ".join(r.getMessage() for r in caplog.records)
    for site in ESSENTIAL_CLOUD_SITES - {"4_light_reflection"}:
        assert site in warned_text


def test_build_map_empty_when_all_sites_missing_logs_error(caplog):
    """If NONE of the essential ids are present (e.g. a config rename), the map
    collapses to {} — the registry would fall back to legacy degradation, so
    this must be LOUD (error), not silent."""
    logger = logging.getLogger("genesis.routing.essential")
    logger.addHandler(caplog.handler)
    old_level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        m = build_essential_provider_map(_cfg({"13_morning_report": ["openrouter"]}))
    finally:
        logger.removeHandler(caplog.handler)
        logger.setLevel(old_level)
    assert m == {}
    assert any(r.levelno >= logging.ERROR for r in caplog.records)
    assert any(
        "coverage-based degradation is DISABLED" in r.getMessage()
        for r in caplog.records
    )
