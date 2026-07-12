"""Sentinel diagnostic context: infrastructure body-schema section wiring."""

from __future__ import annotations

from unittest.mock import patch

from genesis.sentinel.context import assemble_diagnostic_context


def _profile():
    return {
        "collected_at": "2026-07-11T00:00:00+00:00",
        "planes": {"container": {"available": True}, "host": {"available": False}},
        "sections": {
            "memory": {
                "status": "ok",
                "hash": "h",
                "facts": {"cgroup_memory_max": 17179869184},
                "metrics": {},
            },
        },
    }


async def test_digest_section_present():
    with (
        patch("genesis.infra_profile.store.load_profile", return_value=_profile()),
        patch(
            "genesis.infra_profile.store.load_annotations",
            return_value={
                "sections": {"memory": {"annotation": "- limit gotcha", "source_hash": "h"}}
            },
        ),
    ):
        context = await assemble_diagnostic_context(
            alarms=[],
            trigger_source="test",
            trigger_reason="test",
        )
    assert "## Infrastructure Body Schema" in context
    assert "- limit gotcha" in context


async def test_missing_profile_tolerated():
    with patch("genesis.infra_profile.store.load_profile", return_value={}):
        context = await assemble_diagnostic_context(
            alarms=[],
            trigger_source="test",
            trigger_reason="test",
        )
    assert "## Infrastructure Body Schema" not in context
    assert "## Trigger" in context  # the rest of the context still assembles


async def test_digest_failure_tolerated():
    with patch(
        "genesis.infra_profile.store.load_profile",
        side_effect=RuntimeError("disk gone"),
    ):
        context = await assemble_diagnostic_context(
            alarms=[],
            trigger_source="test",
            trigger_reason="test",
        )
    assert "## Trigger" in context  # never breaks diagnosis
