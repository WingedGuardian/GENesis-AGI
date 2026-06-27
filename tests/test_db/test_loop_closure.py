"""Unit tests for the loop-closure funnel aggregations (LC0).

Verifies the per-artifact captured → invoked/surfaced → actuated → measured →
leak accounting that powers ``loop_closure_status``. The funnel must (a) count
the real columns, (b) derive the ``loop`` label from the data (never hardcode),
and (c) correctly flag OPEN seams — captured-but-never-acted-on — which is the
whole point of the assurance view. The MCP assembly + open_seams formatting are
covered too.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from genesis.db.crud import loop_closure as lc

pytestmark = pytest.mark.asyncio

_OLD = "2020-01-01T00:00:00+00:00"   # before the stale cutoff
_NEW = "2099-01-01T00:00:00+00:00"   # after the stale cutoff
_STALE_BEFORE = "2024-01-01T00:00:00+00:00"


async def _proc(db, pid, *, surfaced=0, invocation=0, success=0, failure=0, tier="DORMANT", deprecated=0):
    await db.execute(
        "INSERT INTO procedural_memory "
        "(id, task_type, principle, steps, tools_used, context_tags, created_at, "
        " surfaced_count, invocation_count, success_count, failure_count, activation_tier, deprecated) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, "t", "p", "[]", "[]", "[]", _OLD, surfaced, invocation, success, failure, tier, deprecated),
    )


async def _obs(db, oid, *, otype="generic", influenced=0, resolved=0, surfaced=0, created=_NEW):
    await db.execute(
        "INSERT INTO observations "
        "(id, source, type, content, priority, created_at, influenced_action, resolved, surfaced_count) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (oid, "s", otype, "c", "medium", created, influenced, resolved, surfaced),
    )


async def _refl(db, rid, *, used=0, quality=None):
    await db.execute(
        "INSERT INTO reflection_corpus "
        "(id, depth, prompt_text, response_text, created_at, used_in_optimization, quality_label) "
        "VALUES (?,?,?,?,?,?,?)",
        (rid, "light", "q", "a", _NEW, used, quality),
    )


async def _fu(db, fid, *, status, strategy="ego_judgment", created=_NEW):
    await db.execute(
        "INSERT INTO follow_ups (id, source, content, strategy, status, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (fid, "session_retro", "do x", strategy, status, created),
    )


async def _prop(db, pid, *, status, created=_NEW):
    await db.execute(
        "INSERT INTO ego_proposals (id, action_type, content, status, created_at) "
        "VALUES (?,?,?,?,?)",
        (pid, "investigate", "do y", status, created),
    )


# ── procedures: captured → surfaced/invoked → measured; leak = never reached ──

async def test_procedure_funnel_counts_surfaced_invoked_and_leak(db):
    await _proc(db, "p1", invocation=5, success=3, tier="ADVISORY")  # invoked + measured
    await _proc(db, "p2", surfaced=4, tier="DORMANT")               # surfaced only (NOT invoked)
    await _proc(db, "p3", invocation=0, tier="DORMANT")            # never reached — the real leak
    await _proc(db, "p4", invocation=0, tier="CORE", deprecated=1)  # never reached + deprecated
    await db.commit()

    f = await lc.procedure_funnel(db)
    assert f["captured"] == 4
    assert f["surfaced"] == 1          # only p2 (surfaced_count > 0)
    assert f["invoked"] == 1           # only p1 (invocation_count > 0)
    assert f["measured"] == 1          # only p1 has outcome counts
    assert f["leak_never_reached"] == 2  # p3 + p4 — neither surfaced nor invoked
    assert f["deprecated"] == 1
    assert f["by_tier"] == {"ADVISORY": 1, "DORMANT": 2, "CORE": 1}
    assert f["loop"] == "PARTIAL"      # p1 invoked + p2 surfaced flow; p3/p4 leak


async def test_procedure_surfaced_only_flips_loop_open_to_partial(db):
    # The C-honest fix: a DORMANT draft that the proactive hook surfaces (but is
    # never explicitly invoked) now counts as 'reached' — the loop is no longer
    # falsely OPEN just because invocation_count stayed 0.
    await _proc(db, "p1", surfaced=2, invocation=0, tier="DORMANT")
    await db.commit()

    f = await lc.procedure_funnel(db)
    assert f["surfaced"] == 1
    assert f["invoked"] == 0
    assert f["leak_never_reached"] == 0
    assert f["loop"] == "CLOSED"       # the one procedure reached context; nothing leaks


async def test_procedure_funnel_only_one_leak_key(db):
    # open_seams iterates every ``leak_*`` key; the procedure funnel must emit
    # exactly one so a single dark procedure isn't double-counted as a seam.
    await _proc(db, "p1", invocation=0, tier="DORMANT")
    await db.commit()

    f = await lc.procedure_funnel(db)
    leak_keys = [k for k in f if k.startswith("leak_")]
    assert leak_keys == ["leak_never_reached"]
    assert f["loop"] == "OPEN"         # nothing surfaced or invoked yet


# ── observations: actuation = influenced_action ──────────────────────────────

async def test_observation_funnel_open_when_none_actuated(db):
    await _obs(db, "o1", influenced=0, resolved=0, created=_OLD)  # stale, un-actuated → leak
    await _obs(db, "o2", influenced=0, resolved=0, created=_NEW)
    await db.commit()

    f = await lc.observation_funnel(db, stale_before=_STALE_BEFORE)
    assert f["captured"] == 2
    assert f["actuated"] == 0
    assert f["leak_stale_unactuated"] == 1   # only o1 is past the cutoff
    assert f["loop"] == "OPEN"


async def test_observation_funnel_partial_with_leak(db):
    await _obs(db, "o1", influenced=1, resolved=1)                # actuated
    await _obs(db, "o2", influenced=0, resolved=0, created=_OLD)  # stale leak
    await db.commit()
    f = await lc.observation_funnel(db, stale_before=_STALE_BEFORE)
    assert f["actuated"] == 1
    assert f["leak_stale_unactuated"] == 1
    assert f["loop"] == "PARTIAL"


# ── reflections: actuation measured via reflection-output observations ────────

async def test_reflection_funnel_measures_observation_actuation(db):
    # reflection-output observations carry the actuation signal
    await _obs(db, "ro1", otype="micro_reflection", influenced=1)    # actuated
    await _obs(db, "ro2", otype="reflection_summary", influenced=0)  # not actuated
    await _obs(db, "ro3", otype="light_reflection", influenced=1)    # actuated
    # quarantined_reflection is a gatekept failure → must NOT be counted
    await _obs(db, "qr1", otype="quarantined_reflection", influenced=0)
    # a non-reflection observation → must NOT be counted
    await _obs(db, "g1", otype="generic", influenced=0)
    # raw corpus transcripts: context only; used_in_optimization is a dead column
    await _refl(db, "c1", used=0)
    await db.commit()

    f = await lc.reflection_funnel(db)
    assert f["captured"] == 3            # ro1/ro2/ro3 only (not qr1, not g1)
    assert f["actuated"] == 2            # ro1 + ro3
    assert f["corpus_captured"] == 1     # transcript log, context only
    assert f["optimization_pipeline"] == "not_built"
    # emits NO leak_ key — those rows' staleness is owned by observation_funnel
    assert not any(k.startswith("leak_") for k in f)
    assert f["loop"] == "PARTIAL"        # some actuate, some don't — NOT OPEN


async def test_reflection_funnel_open_only_when_no_actuation(db):
    # genuinely OPEN (real, not a dead-column artifact) when nothing influenced
    await _obs(db, "ro1", otype="reflection_observation", influenced=0)
    await db.commit()
    f = await lc.reflection_funnel(db)
    assert f["captured"] == 1 and f["actuated"] == 0
    assert f["loop"] == "OPEN"


# ── follow-ups: actuated = scheduled/in_progress/completed; leak = stale pending ─

async def test_followup_funnel_actuation_and_graveyard(db):
    await _fu(db, "f1", status="scheduled")               # actuated
    await _fu(db, "f2", status="completed")               # actuated
    await _fu(db, "f5", status="in_progress")             # actuated (now counted)
    await _fu(db, "f3", status="pending", created=_OLD)   # graveyard
    await _fu(db, "f4", status="pending", created=_NEW)   # fresh pending (not yet a leak)
    await db.commit()

    f = await lc.followup_funnel(db, stale_before=_STALE_BEFORE)
    assert f["captured"] == 5
    assert f["actuated"] == 3
    assert f["leak_pending_stale"] == 1   # only f3
    assert f["by_status"]["pending"] == 2


# ── ego proposals: actuated = approved/executed; leak = stale pending ─────────

async def test_proposal_funnel(db):
    await _prop(db, "g1", status="executed")
    await _prop(db, "g2", status="approved")              # sanctioned → actuated
    await _prop(db, "g3", status="pending", created=_OLD)
    await db.commit()

    f = await lc.proposal_funnel(db, stale_before=_STALE_BEFORE)
    assert f["captured"] == 3
    assert f["actuated"] == 2
    assert f["leak_pending_stale"] == 1


# ── empty DB: every funnel returns zeros + EMPTY label, no error ──────────────

async def test_funnels_on_empty_db(db):
    pf = await lc.procedure_funnel(db)
    assert pf["captured"] == 0 and pf["loop"] == "EMPTY"
    assert (await lc.observation_funnel(db, stale_before=_STALE_BEFORE))["loop"] == "EMPTY"
    assert (await lc.reflection_funnel(db))["loop"] == "EMPTY"
    assert (await lc.followup_funnel(db, stale_before=_STALE_BEFORE))["loop"] == "EMPTY"
    assert (await lc.proposal_funnel(db, stale_before=_STALE_BEFORE))["loop"] == "EMPTY"


# ── MCP assembly + open_seams formatting (W3) ────────────────────────────────

async def test_impl_assembly_and_open_seams(db, monkeypatch):
    import genesis.mcp.health_mcp as hm

    monkeypatch.setattr(hm, "_service", SimpleNamespace(_db=db))

    # reflections actuate via observations → NOT a false OPEN on the dead column
    await _obs(db, "ro1", otype="micro_reflection", influenced=1)
    await _refl(db, "r1", used=0)        # raw transcript (context only)
    await _proc(db, "p1", invocation=0)  # never-reached procedure → leak seam
    await db.commit()

    from genesis.mcp.health.loop_closure_status import _impl_loop_closure_status

    res = await _impl_loop_closure_status()
    assert res["status"] == "ok"
    assert isinstance(res["funnel"], list) and len(res["funnel"]) == 5
    assert "outcome_bus" in res

    seams = res["open_seams"]
    # the procedure leak still surfaces honestly (now: never reached)
    assert any("procedure" in s and "never reached" in s for s in seams)
    # reflections must NOT be flagged OPEN any more (dead-column false positive gone)
    assert not any("reflection" in s and "OPEN" in s for s in seams)
    # by_tier / by_status dicts must NEVER be rendered as a seam string
    assert all("{" not in s and "[" not in s for s in seams)
