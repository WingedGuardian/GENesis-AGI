"""Recall-health probe tests — hit/rank, fail-closed, drift, skip_writeback."""

from __future__ import annotations

import json

import aiosqlite
import pytest

from genesis.db.crud import memory_integrity as mi
from genesis.memory.recall_probe import load_golden_set, run_recall_probe

from .conftest import build_db

pytestmark = pytest.mark.asyncio


class _Result:
    def __init__(self, memory_id: str) -> None:
        self.memory_id = memory_id


class FakeRetriever:
    """Returns a canned id list per query; records recall kwargs for assertions;
    can raise to simulate a provider outage."""

    def __init__(self, responses: dict[str, list[str]], *, raise_exc: Exception | None = None):
        self.responses = responses
        self.raise_exc = raise_exc
        self.calls: list[dict] = []

    async def recall(self, query, **kwargs):
        # Mirror the REAL HybridRetriever.recall contract: `source` is the
        # collection selector, validated against a fixed set. Enforcing it here
        # is what makes the fake honest — an invalid source (the bug the probe
        # shipped with) must fail the test, not pass silently.
        source = kwargs.get("source")
        if source not in ("episodic", "knowledge", "both", None):
            raise ValueError(f"source must be episodic|knowledge|both, got {source!r}")
        self.calls.append({"query": query, **kwargs})
        if self.raise_exc is not None:
            raise self.raise_exc
        return [_Result(mid) for mid in self.responses.get(query, [])]


def _write_golden(tmp_path, cases: list[dict]) -> str:
    p = tmp_path / "golden.jsonl"
    lines = ["# header comment", ""]
    lines += [json.dumps(c) for c in cases]
    p.write_text("\n".join(lines))
    return str(p)


async def _db(tmp_path):
    path = str(tmp_path / "t.db")
    await build_db(path)
    return await aiosqlite.connect(path)


# ── golden loader ──


async def test_load_golden_skips_comments_and_bad(tmp_path):
    p = tmp_path / "g.jsonl"
    p.write_text(
        "# comment\n\n"
        '{"id":"a","query":"q1","expected_memory_ids":["m1"]}\n'
        '{"id":"b","query":"","expected_memory_ids":["m2"]}\n'  # bad: empty query
        '{"id":"c","query":"q3","expected_memory_ids":[]}\n'  # bad: empty expected
        "not json\n"
        '{"id":"d","query":"q4","expected_memory_ids":["m4","m5"]}\n'
    )
    cases = load_golden_set(p)
    assert [c["id"] for c in cases] == ["a", "d"]
    assert cases[1]["expected"] == ["m4", "m5"]


async def test_load_golden_absent_file_is_empty(tmp_path):
    assert load_golden_set(tmp_path / "nope.jsonl") == []


# ── probe status ──


async def test_too_small_golden_is_unknown(tmp_path):
    conn = await _db(tmp_path)
    gp = _write_golden(tmp_path, [{"id": "a", "query": "q", "expected_memory_ids": ["m"]}])
    res = await run_recall_probe(
        db=conn, retriever=FakeRetriever({}), golden_path=gp, min_golden_for_status=5
    )
    assert res.status == "unknown"
    assert res.unknown_reason == "golden_set_too_small"
    await conn.close()


async def test_retriever_none_is_unknown(tmp_path):
    conn = await _db(tmp_path)
    res = await run_recall_probe(db=conn, retriever=None, min_golden_for_status=1)
    assert res.status == "unknown"
    assert res.unknown_reason == "retriever_unavailable"
    await conn.close()


async def test_recall_error_is_unknown_not_miss(tmp_path):
    """A provider outage must not be scored as a recall miss."""
    conn = await _db(tmp_path)
    cases = [{"id": str(i), "query": f"q{i}", "expected_memory_ids": ["m"]} for i in range(5)]
    gp = _write_golden(tmp_path, cases)
    retr = FakeRetriever({}, raise_exc=ConnectionError("provider down"))
    res = await run_recall_probe(db=conn, retriever=retr, golden_path=gp, min_golden_for_status=5)
    assert res.status == "unknown"
    assert "recall_error" in res.unknown_reason
    await conn.close()


async def test_hit_rate_and_mrr(tmp_path):
    conn = await _db(tmp_path)
    cases = [
        {"id": "a", "query": "qa", "expected_memory_ids": ["ma"]},  # hit at rank 1
        {"id": "b", "query": "qb", "expected_memory_ids": ["mb"]},  # hit at rank 3
        {"id": "c", "query": "qc", "expected_memory_ids": ["mc"]},  # miss
        {"id": "d", "query": "qd", "expected_memory_ids": ["md"]},  # miss
        {"id": "e", "query": "qe", "expected_memory_ids": ["me"]},  # hit at rank 1
    ]
    gp = _write_golden(tmp_path, cases)
    retr = FakeRetriever(
        {
            "qa": ["ma", "x"],
            "qb": ["y", "z", "mb"],
            "qc": ["n"],
            "qd": [],
            "qe": ["me"],
        }
    )
    res = await run_recall_probe(
        db=conn, retriever=retr, golden_path=gp, min_golden_for_status=5, baseline_min_runs=3
    )
    assert res.probes_total == 5
    assert res.probes_hit == 3
    assert abs(res.hit_rate - 0.6) < 1e-9
    # MRR = (1 + 1/3 + 0 + 0 + 1) / 5
    assert abs(res.mean_rr - (1 + 1 / 3 + 1) / 5) < 1e-9
    # no baseline history yet → observation period, healthy, no drift
    assert res.status == "healthy"
    assert res.baseline_hit_rate is None and res.drift is None
    await conn.close()


async def test_probe_passes_skip_writeback(tmp_path):
    conn = await _db(tmp_path)
    cases = [{"id": str(i), "query": f"q{i}", "expected_memory_ids": ["m"]} for i in range(5)]
    gp = _write_golden(tmp_path, cases)
    retr = FakeRetriever({f"q{i}": ["m"] for i in range(5)})
    await run_recall_probe(db=conn, retriever=retr, golden_path=gp, min_golden_for_status=5)
    # every recall must disable activation write-back (probe must not entrench)
    # and pass a VALID collection source (regression for the ValueError bug).
    for call in retr.calls:
        assert callable(call["skip_writeback"])
        assert call["skip_writeback"]("anything") is True
        assert call["source"] == "both"
    await conn.close()


async def test_drift_degraded_vs_baseline(tmp_path):
    conn = await _db(tmp_path)
    # seed 3 prior healthy runs at hit_rate 0.9 → baseline 0.9
    for i in range(3):
        await mi.insert_recall_probe_run(
            conn,
            status="healthy",
            probes_total=10,
            probes_hit=9,
            hit_rate=0.9,
            mean_rr=0.9,
            created_at=f"2026-07-2{i} 03:00:00",
        )
    # current run hits 0.5 → drift 0.4 > band 0.2 → degraded
    cases = [{"id": str(i), "query": f"q{i}", "expected_memory_ids": ["m"]} for i in range(5)]
    gp = _write_golden(tmp_path, cases)
    retr = FakeRetriever({"q0": ["m"], "q1": ["m"], "q2": ["m"], "q3": ["x"], "q4": ["x"]})
    res = await run_recall_probe(
        db=conn,
        retriever=retr,
        golden_path=gp,
        min_golden_for_status=5,
        baseline_min_runs=3,
        drift_band=0.2,
    )
    assert abs(res.hit_rate - 0.6) < 1e-9
    assert res.baseline_hit_rate == 0.9
    assert abs(res.drift - 0.3) < 1e-9  # 0.9 - 0.6
    assert res.status == "degraded"  # drift 0.3 > band 0.2
    await conn.close()
