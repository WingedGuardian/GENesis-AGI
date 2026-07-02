"""Guards for health MCP constants."""

from __future__ import annotations


def test_job_stale_gap_days_is_positive_float():
    from genesis.mcp.health.constants import JOB_STALE_GAP_DAYS

    assert isinstance(JOB_STALE_GAP_DAYS, float)
    assert JOB_STALE_GAP_DAYS > 0
