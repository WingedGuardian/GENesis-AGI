"""CRUD round-trip / prune / baseline tests."""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.db.crud import memory_integrity as mi

from .conftest import build_db

pytestmark = pytest.mark.asyncio


async def _conn(tmp_path):
    path = str(tmp_path / "t.db")
    await build_db(path)
    conn = await aiosqlite.connect(path)
    return conn


# ── crud ──────────────────────────────────────────────────────────────────


async def test_consistency_report_round_trip(tmp_path):
    conn = await _conn(tmp_path)
    rid = await mi.insert_consistency_report(
        conn,
        status="degraded",
        counts={"lying_mirror": 3, "ghost_points": 7},
        total_rows=100,
        sampled_rows=100,
        sample_fraction=1.0,
        truncated=False,
        offender_sample={"lying_mirror": ["a"]},
        duration_ms=42,
    )
    assert rid
    latest = await mi.latest_consistency_report(conn)
    assert latest["status"] == "degraded"
    assert '"lying_mirror": 3' in latest["counts_json"]
    await conn.close()


async def test_trailing_hit_rate_excludes_unknown(tmp_path):
    conn = await _conn(tmp_path)
    await mi.insert_recall_probe_run(
        conn,
        status="healthy",
        probes_total=10,
        probes_hit=9,
        hit_rate=0.9,
        mean_rr=0.8,
        created_at="2026-07-20 03:00:00",
    )
    await mi.insert_recall_probe_run(
        conn,
        status="unknown",
        probes_total=0,
        probes_hit=0,
        hit_rate=None,
        mean_rr=None,
        unknown_reason="golden_set_too_small",
        created_at="2026-07-21 03:00:00",
    )
    await mi.insert_recall_probe_run(
        conn,
        status="healthy",
        probes_total=10,
        probes_hit=7,
        hit_rate=0.7,
        mean_rr=0.6,
        created_at="2026-07-22 03:00:00",
    )
    mean, n = await mi.trailing_hit_rate(conn, window=7)
    assert n == 2
    assert abs(mean - 0.8) < 1e-9
    await conn.close()


async def test_trailing_hit_rate_no_runs_is_none(tmp_path):
    conn = await _conn(tmp_path)
    mean, n = await mi.trailing_hit_rate(conn, window=7)
    assert mean is None and n == 0
    await conn.close()


async def test_has_recent_non_unknown_report(tmp_path):
    conn = await _conn(tmp_path)
    await mi.insert_consistency_report(
        conn,
        status="unknown",
        counts={},
        total_rows=0,
        sampled_rows=0,
        sample_fraction=1.0,
        truncated=False,
        unknown_reason="qdrant_unavailable",
        created_at="2026-07-22 03:00:00",
    )
    # only an unknown row exists → not "recent non-unknown"
    assert await mi.has_recent_non_unknown_report(conn, since_iso="2026-07-01") is False
    await mi.insert_consistency_report(
        conn,
        status="healthy",
        counts={},
        total_rows=5,
        sampled_rows=5,
        sample_fraction=1.0,
        truncated=False,
        created_at="2026-07-23 03:00:00",
    )
    assert await mi.has_recent_non_unknown_report(conn, since_iso="2026-07-01") is True
    await conn.close()


async def test_prune_spares_nothing_but_reports(tmp_path):
    conn = await _conn(tmp_path)
    await mi.insert_consistency_report(
        conn,
        status="healthy",
        counts={},
        total_rows=1,
        sampled_rows=1,
        sample_fraction=1.0,
        truncated=False,
        created_at="2020-01-01 00:00:00",
    )
    await mi.insert_recall_probe_run(
        conn,
        status="healthy",
        probes_total=1,
        probes_hit=1,
        hit_rate=1.0,
        mean_rr=1.0,
        created_at="2020-01-01 00:00:00",
    )
    deleted = await mi.prune_memory_integrity(conn, older_than_days=90, now="2030-01-01 00:00:00")
    assert deleted == 2
    assert await mi.latest_consistency_report(conn) is None
    await conn.close()


async def test_crud_noop_before_migration(tmp_path):
    """Tables absent → writers no-op, readers return None/empty (self-heal)."""
    # Reset the per-process table cache so this test isn't masked by a prior one.
    mi._tables_verified = False
    path = str(tmp_path / "bare.db")
    conn = await aiosqlite.connect(path)
    # no schema created
    assert (
        await mi.insert_consistency_report(
            conn,
            status="healthy",
            counts={},
            total_rows=0,
            sampled_rows=0,
            sample_fraction=1.0,
            truncated=False,
        )
        is None
    )
    assert await mi.latest_consistency_report(conn) is None
    assert await mi.has_recent_non_unknown_report(conn, since_iso="2000-01-01") is True
    assert await mi.prune_memory_integrity(conn, older_than_days=90, now="2030-01-01") == 0
    await conn.close()
