"""Unit tests for the loop-closure funnel aggregations (LC0).

Verifies the per-artifact captured → invoked/surfaced → actuated → measured →
leak accounting that powers ``loop_closure_status``. The funnel must (a) count
the real columns, (b) derive the ``loop`` label from the data (never hardcode),
and (c) correctly flag OPEN seams — captured-but-never-acted-on — which is the
whole point of the assurance view. The MCP assembly + open_seams formatting are
covered too.
"""

from __future__ import annotations

import json
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


async def _sess(db, sid, *, skill_tags, status="completed", session_type="background_task"):
    """Insert a cc_sessions row whose metadata carries ``skill_tags`` (the only
    skill-usage signal the funnel can read today)."""
    meta = None if skill_tags is None else json.dumps({"skill_tags": list(skill_tags)})
    await db.execute(
        "INSERT INTO cc_sessions "
        "(id, session_type, model, status, started_at, last_activity_at, metadata) "
        "VALUES (?,?,?,?,?,?,?)",
        (sid, session_type, "m", status, _NEW, _NEW, meta),
    )


# A fixed library so skill-funnel tests don't depend on the real on-disk skill set.
_LIB = ["alpha", "beta", "gamma"]


def _patch_library(monkeypatch, names=_LIB):
    import genesis.learning.skills.wiring as wiring

    monkeypatch.setattr(wiring, "list_available_skills", lambda: list(names))


# ── procedures: captured → surfaced/invoked → measured; leak = never reached ──

async def test_procedure_funnel_counts_surfaced_invoked_and_leak(db):
    await _proc(db, "p1", invocation=5, success=3, tier="ADVISORY")  # invoked + measured
    await _proc(db, "p2", surfaced=4, tier="DORMANT")               # surfaced only (NOT invoked)
    await _proc(db, "p3", invocation=0, tier="DORMANT")            # never reached — the real leak
    await _proc(db, "p4", invocation=0, tier="CORE", deprecated=1)  # never reached + deprecated
    await _proc(db, "p5", surfaced=2, invocation=1, tier="LIBRARY")  # BOTH — must not double-count
    await db.commit()

    f = await lc.procedure_funnel(db)
    assert f["captured"] == 5
    assert f["surfaced"] == 2          # p2 + p5 (surfaced_count > 0)
    assert f["invoked"] == 2           # p1 + p5 (invocation_count > 0)
    assert f["reached"] == 3           # p1 + p2 + p5 — de-duped union (NOT surfaced+invoked=4)
    assert f["measured"] == 1          # only p1 has outcome counts
    assert f["leak_never_reached"] == 2  # p3 + p4 — neither surfaced nor invoked
    assert f["deprecated"] == 1
    assert f["by_tier"] == {"ADVISORY": 1, "DORMANT": 2, "CORE": 1, "LIBRARY": 1}
    assert f["loop"] == "PARTIAL"      # p1/p2/p5 flow; p3/p4 leak


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


# ── skills: file-based, NOT outcome-graded; honest instrumentation funnel ─────

async def test_skill_funnel_uninstrumented_is_leak(db, monkeypatch):
    # No session tags any skill → every library skill is uninstrumented (the leak).
    _patch_library(monkeypatch)
    f = await lc.skill_funnel(db)
    assert f["captured"] == 3
    assert f["instrumented"] == 0
    assert f["measured"] == 0
    assert f["leak_uninstrumented"] == 3
    assert f["graded"] is False
    assert f["loop"] == "OPEN"            # nothing instrumented → loop open


async def test_skill_funnel_instrumented_and_measured(db, monkeypatch):
    _patch_library(monkeypatch)
    await _sess(db, "s1", skill_tags=["alpha"], status="completed")  # measured
    await _sess(db, "s2", skill_tags=["beta"], status="failed")      # measured (failed IS terminal)
    await db.commit()
    f = await lc.skill_funnel(db)
    assert f["instrumented"] == 2          # alpha + beta
    assert f["measured"] == 2              # both in terminal-status sessions
    assert f["leak_uninstrumented"] == 1   # gamma untouched
    assert f["loop"] == "PARTIAL"          # some flow, some leak


async def test_skill_funnel_success_only_skill_is_measured(db, monkeypatch):
    # Regression guard for the DISCARDED plan's design bug: a 100%-success skill
    # (never failed) MUST still count as measured — 'measured' means outcome is
    # knowable, NOT 'has a failure'.
    _patch_library(monkeypatch)
    await _sess(db, "s1", skill_tags=["alpha"], status="completed")
    await _sess(db, "s2", skill_tags=["alpha"], status="completed")
    await db.commit()
    f = await lc.skill_funnel(db)
    assert f["instrumented"] == 1
    assert f["measured"] == 1              # alpha measured despite zero failures


async def test_skill_funnel_non_terminal_is_instrumented_not_measured(db, monkeypatch):
    # An 'active' (non-terminal) session tags a skill: usage signal exists
    # (instrumented) but no determinable outcome yet (not measured).
    _patch_library(monkeypatch)
    await _sess(db, "s1", skill_tags=["alpha"], status="active")
    await db.commit()
    f = await lc.skill_funnel(db)
    assert f["instrumented"] == 1
    assert f["measured"] == 0
    assert f["leak_uninstrumented"] == 2


async def test_skill_funnel_non_library_tag_does_not_inflate(db, monkeypatch):
    # A pseudo-label that is not a real library skill (e.g. a reflection depth
    # label, or a 'profile' value) must NOT count as instrumented — exact
    # membership against the library, not a loose substring.
    _patch_library(monkeypatch)
    await _sess(db, "s1", skill_tags=["strategic-reflection", "research"], status="completed")
    await db.commit()
    f = await lc.skill_funnel(db)
    assert f["instrumented"] == 0
    assert f["leak_uninstrumented"] == 3


async def test_skill_funnel_ignores_malformed_metadata(db, monkeypatch):
    # A row matching the LIKE filter but carrying invalid JSON must be skipped
    # silently (fail-safe), not crash the funnel.
    _patch_library(monkeypatch)
    await db.execute(
        "INSERT INTO cc_sessions "
        "(id, session_type, model, status, started_at, last_activity_at, metadata) "
        "VALUES (?,?,?,?,?,?,?)",
        ("bad", "background_task", "m", "completed", _NEW, _NEW, '{"skill_tags": not json'),
    )
    await _sess(db, "ok", skill_tags=["alpha"], status="completed")
    await db.commit()
    f = await lc.skill_funnel(db)
    assert f["instrumented"] == 1          # only the valid row counted; bad row skipped


async def test_skill_funnel_exactly_one_leak_key(db, monkeypatch):
    # open_seams iterates every leak_* key; the skill funnel must emit exactly one.
    _patch_library(monkeypatch)
    f = await lc.skill_funnel(db)
    leak_keys = [k for k in f if k.startswith("leak_")]
    assert leak_keys == ["leak_uninstrumented"]


async def test_skill_funnel_empty_library(db, monkeypatch):
    _patch_library(monkeypatch, names=[])
    f = await lc.skill_funnel(db)
    assert f["captured"] == 0
    assert f["leak_uninstrumented"] == 0
    assert f["loop"] == "EMPTY"


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
    _patch_library(monkeypatch)          # deterministic skill funnel (3-skill library)

    # reflections actuate via observations → NOT a false OPEN on the dead column
    await _obs(db, "ro1", otype="micro_reflection", influenced=1)
    await _refl(db, "r1", used=0)        # raw transcript (context only)
    await _proc(db, "p1", invocation=0)  # never-reached procedure → leak seam
    await _sess(db, "s1", skill_tags=["alpha"], status="completed")  # 1 skill instrumented
    await db.commit()

    from genesis.mcp.health.loop_closure_status import _impl_loop_closure_status

    res = await _impl_loop_closure_status()
    assert res["status"] == "ok"
    assert isinstance(res["funnel"], list) and len(res["funnel"]) == 6
    assert any(f["artifact"] == "skill" for f in res["funnel"])
    assert "outcome_bus" in res
    assert "skills_note" not in res       # the hardcoded stub note is gone

    seams = res["open_seams"]
    # the procedure leak still surfaces honestly (now: never reached)
    assert any("procedure" in s and "never reached" in s for s in seams)
    # the skill instrumentation gap surfaces honestly (2 of 3 skills uninstrumented)
    assert any("skill" in s and "uninstrumented" in s for s in seams)
    # reflections must NOT be flagged OPEN any more (dead-column false positive gone)
    assert not any("reflection" in s and "OPEN" in s for s in seams)
    # by_tier / by_status dicts must NEVER be rendered as a seam string
    assert all("{" not in s and "[" not in s for s in seams)
