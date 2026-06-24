"""Regression test for the Internals-tab LLM totals undercount.

`_build_llm_section` totals previously counted calls from `cost_events`, which
omits free-tier providers (the router writes a cost_events row only when cost is
non-zero/unknown). That undercounted total LLM calls by ~75%. The totals call
count must come from `activity_log` (where every call is logged); only the
dollar cost comes from `cost_events`.
"""

from __future__ import annotations

from types import SimpleNamespace

from genesis.dashboard.routes.vitals import _build_llm_section


async def _seed(db):
    # 5 free-tier calls (no cost_events) + 2 paid calls (with cost_events).
    for _ in range(5):
        await db.execute(
            "INSERT INTO activity_log (provider, latency_ms, success, created_at) "
            "VALUES ('llm.groq-free', 120, 1, datetime('now'))"
        )
    for _ in range(2):
        await db.execute(
            "INSERT INTO activity_log (provider, latency_ms, success, created_at) "
            "VALUES ('llm.openrouter-deepseek-v4', 300, 1, datetime('now'))"
        )
    # A non-LLM row that must NOT be counted in LLM totals.
    await db.execute(
        "INSERT INTO activity_log (provider, latency_ms, success, created_at) "
        "VALUES ('qdrant.search', 8, 1, datetime('now'))"
    )
    # cost_events only for the paid provider (free calls emit none).
    for i in range(2):
        await db.execute(
            "INSERT INTO cost_events (id, event_type, provider, cost_usd, "
            "cost_known, created_at) VALUES (?, 'llm_call', "
            "'openrouter-deepseek-v4', 0.2, 1, datetime('now'))",
            (f"ce-{i}",),
        )
    await db.commit()


async def test_totals_count_calls_from_activity_log_not_cost_events(db):
    await _seed(db)
    rt = SimpleNamespace(db=db, circuit_breakers=None)
    routing_ctx = {"providers": {}, "call_site_assignments": {}, "profiles": {}}

    section = await _build_llm_section(rt, routing_ctx)
    totals = section["totals"]

    # 7 LLM calls in activity_log (5 free + 2 paid); the qdrant row excluded.
    # Counting from cost_events would have yielded 2 — the bug.
    assert totals["calls_24h"] == 7
    assert totals["calls_1h"] == 7
    # Dollar cost still sourced from cost_events: 2 × $0.2.
    assert totals["cost_24h"] == 0.4


async def test_totals_zero_when_no_activity(db):
    rt = SimpleNamespace(db=db, circuit_breakers=None)
    section = await _build_llm_section(
        rt, {"providers": {}, "call_site_assignments": {}, "profiles": {}}
    )
    assert section["totals"]["calls_24h"] == 0
    assert section["totals"]["cost_24h"] == 0.0
