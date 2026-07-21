"""Unit tests for the server-side proactive recall engine.

Covers the presentation layer that moved out of the old subprocess fork:
stance→budget, KB slot cap, the ``[Memory | ...]`` cc_hook renderer, the H-1
shadow projection, structured result rows (pre-bump retrieved_count), and the
full ``proactive_context`` wiring with a faked ``_proactive_impl`` + db.
Install-agnostic: no live retriever, Qdrant, network, or DB.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from genesis.memory import proactive as P
from genesis.memory.intent import classify_stance

# --- stance -----------------------------------------------------------------


def test_classify_stance_command_vs_question_vs_chatter():
    assert classify_stance("restart the server") == "command"
    assert classify_stance("deploy the new build to edge") == "command"
    assert classify_stance("what did we decide about the reranker") == "question_decision"
    assert classify_stance("why did the container OOM") == "question_decision"
    assert classify_stance("hi") == "chatter"
    assert classify_stance("") == "chatter"
    assert classify_stance("the dashboard chart looks wrong") == "general"
    # imperative verb but phrased as a question → not a command
    assert classify_stance("should we restart the server") == "general"


# --- budget -----------------------------------------------------------------


def test_budget_curve_and_cap():
    # Code defaults (no yaml overlay in the test env).
    assert P._budget_for("command", "cc_hook") == (1, 8)
    assert P._budget_for("chatter", "cc_hook") == (1, 8)
    assert P._budget_for("general", "cc_hook") == (3, 8)
    assert P._budget_for("question_decision", "cc_hook") == (6, 8)
    # Unknown stance falls back to general's count.
    assert P._budget_for("nonsense", "cc_hook")[0] == 3


# --- KB slot cap ------------------------------------------------------------


def test_rerank_for_honors_live_config_kill_switch():
    # Documented latency/cost kill switch: proactive.profiles.cc_hook.rerank: off
    with patch.object(
        P, "_proactive_config", return_value={"profiles": {"cc_hook": {"rerank": "off"}}}
    ):
        assert P._rerank_for("cc_hook") is False
    with patch.object(
        P, "_proactive_config", return_value={"profiles": {"cc_hook": {"rerank": False}}}
    ):
        assert P._rerank_for("cc_hook") is False
    with patch.object(
        P, "_proactive_config", return_value={"profiles": {"cc_hook": {"rerank": "on"}}}
    ):
        assert P._rerank_for("cc_hook") is True
    # Silent config → profile default (cc_hook ships rerank on).
    with patch.object(P, "_proactive_config", return_value={}):
        assert P._rerank_for("cc_hook") is True


# (The KB slot cap moved into _proactive_impl's backfill loop — see
# test_proactive_impl_kb_slot_cap_backfills below.)


# --- renderer ---------------------------------------------------------------


def test_render_cc_hook_tags_and_labels():
    enriched = [
        {
            "memory_id": "aaaaaaaa1111",
            "content": "We decided to swap ambient STT to SenseVoice.",
            "collection": "episodic_memory",
            "memory_class": "fact",
            "origin_class": None,
            "_created_at": "2000-01-01T00:00:00+00:00",  # old → year age
            "_wing": "voice",
            "related_ids": ["bbbbbbbb"],
        },
        {
            "memory_id": "kbkbkbkb2222",
            "content": "External doc about ASR models.",
            "collection": "knowledge_base",
            "source_pipeline": "knowledge_ingest",
            "memory_class": "fact",
        },
        {
            "memory_id": "extextext333",
            "content": "Overheard external snippet.",
            "collection": "episodic_memory",
            "origin_class": "external_untrusted",
            "memory_class": "fact",
            "_created_at": "2000-01-01T00:00:00+00:00",
        },
    ]
    lines = P.render_cc_hook(enriched)
    # First line: first-party memory with wing + id + breadcrumb.
    assert lines[0].startswith("[Memory | ")
    assert "voice" in lines[0]
    assert "id:aaaaaaaa" in lines[0]
    assert "→ id:bbbbbbbb" in lines[0]
    # KB hit renders the external source tier, not [Memory].
    assert lines[1].startswith("[KB·")
    # Stored-external episodic renders as external tier, not first-party.
    assert lines[2].startswith("[Memory·external")
    # Trailer appended when there are hits.
    assert lines[-1].startswith("Need more?")


def test_render_cc_hook_empty_is_empty():
    assert P.render_cc_hook([]) == []


def test_clean_content_strips_prefix_and_truncates():
    out = P._clean_content("[discovery] " + ("word. " * 100), max_len=40)
    assert not out.startswith("[discovery]")
    assert len(out) <= 40


# --- shadow projection ------------------------------------------------------


def test_shadow_projection_suppress_and_serendipity():
    dicts = [
        {"memory_id": "m1", "collection": "episodic_memory", "payload": {"retrieved_count": 0}},
        {"memory_id": "m2", "collection": "episodic_memory", "payload": {"retrieved_count": 3}},
        {"memory_id": "k1", "collection": "knowledge_base", "payload": {"retrieved_count": 0}},
    ]
    shadow = P._shadow_projection(dicts, frozenset({"m1"}))
    assert shadow["suppressed"] == 1
    assert shadow["projected_injected"] == 2  # m2, k1 (m1 suppressed)
    assert shadow["projected_ids"] == ["m2", "k1"]
    # only never-surfaced NON-KB counts as serendipity-eligible
    assert shadow["serendipity_boosted"] == 1


# --- result rows ------------------------------------------------------------


def test_result_row_prebump_count_present_and_absent():
    vector_hit = {
        "memory_id": "v1",
        "collection": "episodic_memory",
        "payload": {"retrieved_count": 0},
        "score": 0.02,
        "origin_class": None,
        "source_pipeline": None,
    }
    fts_only = {
        "memory_id": "f1",
        "collection": "episodic_memory",
        "payload": {},
        "score": 0.01,
    }
    r_v = P._result_row(vector_hit)
    r_f = P._result_row(fts_only)
    assert r_v["retrieved_count"] == 0 and r_v["kind"] == "memory"
    # FTS-only hit: key ABSENT so the hook's default (-1) excludes it from the
    # never-surfaced stat (matches the old fork's "unknown ≠ 0").
    assert "retrieved_count" not in r_f


def test_kind_classification():
    assert P._kind({"memory_id": "code:x:y", "collection": "episodic_memory"}) == "code"
    assert P._kind({"memory_id": "a", "collection": "knowledge_base"}) == "kb"
    assert P._kind({"memory_id": "a", "collection": "episodic_memory"}) == "memory"


def test_render_procedure_line_dormant_labeled_unproven():
    line = P._render_procedure_line(
        {
            "id": "proc1234abcd",
            "task_type": "git_safety",
            "principle": "Always branch.",
            "tier": "DORMANT",
        }
    )
    assert "unproven draft" in line
    assert "git_safety" in line
    assert "id:proc1234" in line


# --- full engine wiring (faked deps) ----------------------------------------


class _FakeRetriever:
    async def _embed_query(self, _q):
        return ([0.1] * 8, True)


class _FakeDB:
    async def execute_fetchall(self, sql, params=()):
        s = sql.lower()
        if "memory_metadata" in s:
            return [("aaaaaaaa1111", "2000-01-01T00:00:00+00:00", "voice")]
        return []  # memory_links + procedural_memory → nothing


class _FakeMod:
    _retriever = _FakeRetriever()
    _db = _FakeDB()

    @staticmethod
    def _require_init():
        return None


async def test_proactive_context_end_to_end_faked():
    delivered = [
        {
            "memory_id": "aaaaaaaa1111",
            "content": "We decided to swap ambient STT to SenseVoice.",
            "collection": "episodic_memory",
            "memory_class": "fact",
            "origin_class": None,
            "source_pipeline": None,
            "score": 0.03,
            "payload": {"retrieved_count": 0, "wing": "voice"},
            "via_graph": False,
        },
        {
            "memory_id": "kbkbkbkb2222",
            "content": "External doc about ASR.",
            "collection": "knowledge_base",
            "memory_class": "fact",
            "origin_class": None,
            # intentional-ingestion pipeline → survives the proactive KB filter
            # (a non-allowlisted pipeline like "curated"/"surplus" is dropped;
            # see test_proactive_context_filters_noisy_kb).
            "source_pipeline": "knowledge_ingest",
            "score": 0.01,
            "payload": {"retrieved_count": 5},
            "via_graph": False,
        },
    ]
    with (
        patch("genesis.mcp.memory.core._memory_mod", return_value=_FakeMod()),
        patch("genesis.mcp.memory.core._proactive_impl", new=AsyncMock(return_value=delivered)),
    ):
        resp = await P.proactive_context(
            prompt="what did we decide about voice STT",
            session_id="sess-1",
            file_keywords=["retrieval", "store"],
            suppress_ids=["aaaaaaaa1111"],
        )

    assert resp["status"] == "ok"
    assert resp["budget"]["stance"] == "question_decision"
    assert resp["budget"]["limit"] == 6
    assert resp["budget"]["kb_slots"] == 2
    # rendered lines: episodic first-party + KB + trailer
    assert resp["lines"][0].startswith("[Memory | ")
    assert any(line.startswith("[KB·") for line in resp["lines"])
    # structured results carry pre-bump counts + kinds
    rows = {r["memory_id"]: r for r in resp["results"]}
    assert rows["aaaaaaaa1111"]["retrieved_count"] == 0
    assert rows["aaaaaaaa1111"]["kind"] == "memory"
    assert rows["kbkbkbkb2222"]["kind"] == "kb"
    # shadow: aaaa suppressed, one never-surfaced non-KB
    assert resp["shadow"]["suppressed"] == 1
    assert resp["shadow"]["serendipity_boosted"] == 1
    # embedding returned for the hook's ambient fold
    assert resp["embedding"] == [0.1] * 8
    assert resp["engine"]["profile"] == "cc_hook"
    # ``reranked`` reports EXECUTED, not requested — the faked impl never
    # populates the stats sink, so this is honestly False even though the
    # cc_hook profile requested reranking.
    assert resp["engine"]["reranked"] is False
    assert resp["engine"]["rerank_requested"] is True
    assert resp["engine"]["rerank_timed_out"] is False
    # Per-stage timings present (values are wall-clock, just assert shape).
    for key in ("embed", "recall", "enrich", "procedure", "total"):
        assert key in resp["timings_ms"]


async def test_proactive_context_passes_file_keywords_as_extra_fts_terms():
    captured = {}

    async def _fake_impl(
        prompt,
        limit=5,
        *,
        rerank=False,
        extra_fts_terms=None,
        filter_noise=False,
        kb_slots=None,
        rerank_timeout_s=None,
        stats=None,
    ):
        captured["extra"] = extra_fts_terms
        captured["rerank"] = rerank
        captured["limit"] = limit
        captured["rerank_timeout_s"] = rerank_timeout_s
        return []

    with (
        patch("genesis.mcp.memory.core._memory_mod", return_value=_FakeMod()),
        patch("genesis.mcp.memory.core._proactive_impl", new=_fake_impl),
    ):
        await P.proactive_context(
            prompt="restart the server",  # command → budget 1
            file_keywords=["memory", "retrieval", ""],
        )

    assert captured["extra"] == ["memory", "retrieval"]  # falsy dropped
    assert captured["rerank"] is True  # cc_hook default
    assert captured["limit"] == 1  # command budget
    assert captured["rerank_timeout_s"] == P._RERANK_TIMEOUT_S  # hot-path timebox threaded


async def test_proactive_context_bumps_surfaced_count_not_invocation():
    """When the engine surfaces a procedure it bumps ``surfaced_count`` (the
    honest funnel signal), NOT ``invocation_count`` — parity with the pre-flip
    fork's ``_record_procedure_surfaced``, now single-sited server-side so every
    profile records it. Regression guard for the thin-client flip: the hook
    stops writing this, so the engine MUST.
    """

    class _BumpDB:
        def __init__(self) -> None:
            self.executed: list[tuple[str, tuple]] = []

        async def execute_fetchall(self, sql, params=()):
            return []  # no enrich/breadcrumb rows needed

        async def execute(self, sql, params=()):
            self.executed.append((sql, tuple(params) if params else ()))

        async def commit(self):
            return None

    class _BumpMod:
        _retriever = _FakeRetriever()
        _db = _BumpDB()

        @staticmethod
        def _require_init():
            return None

    mod = _BumpMod()
    proc = {"id": "proc-abc", "task_type": "deploy", "principle": "do the thing", "tier": "CORE"}
    with (
        patch("genesis.mcp.memory.core._memory_mod", return_value=mod),
        patch("genesis.mcp.memory.core._proactive_impl", new=AsyncMock(return_value=[])),
        patch("genesis.memory.proactive._surface_procedure", new=AsyncMock(return_value=proc)),
    ):
        resp = await P.proactive_context(prompt="deploy the latest build", session_id="s")

    assert resp["procedure"] == {"id": "proc-abc", "tier": "CORE"}
    bumps = [(sql, params) for sql, params in mod._db.executed if "surfaced_count" in sql.lower()]
    assert len(bumps) == 1, f"expected exactly one surfaced_count bump, got {mod._db.executed}"
    sql, params = bumps[0]
    assert "invocation_count" not in sql.lower()  # must NEVER feed the promoter
    assert params == ("proc-abc",)


async def test_proactive_context_no_procedure_no_bump():
    """No procedure surfaced → no surfaced_count write at all."""

    class _BumpDB:
        def __init__(self) -> None:
            self.executed: list[tuple[str, tuple]] = []

        async def execute_fetchall(self, sql, params=()):
            return []

        async def execute(self, sql, params=()):
            self.executed.append((sql, tuple(params) if params else ()))

        async def commit(self):
            return None

    class _BumpMod:
        _retriever = _FakeRetriever()
        _db = _BumpDB()

        @staticmethod
        def _require_init():
            return None

    mod = _BumpMod()
    with (
        patch("genesis.mcp.memory.core._memory_mod", return_value=mod),
        patch("genesis.mcp.memory.core._proactive_impl", new=AsyncMock(return_value=[])),
        patch("genesis.memory.proactive._surface_procedure", new=AsyncMock(return_value=None)),
    ):
        resp = await P.proactive_context(prompt="deploy the latest build", session_id="s")

    assert resp["procedure"] is None
    assert not [s for s, _ in mod._db.executed if "surfaced_count" in s.lower()]


def _mem(mid, collection, content="ordinary memory content", source_pipeline=None):
    return {
        "memory_id": mid,
        "content": content,
        "collection": collection,
        "memory_class": "fact",
        "origin_class": None,
        "source_pipeline": source_pipeline,
        "score": 0.02,
        "payload": {"retrieved_count": 1},
        "via_graph": False,
    }


async def test_proactive_context_requests_noise_filter():
    """The endpoint delegates noise filtering to the engine (filter_noise=True) so
    it happens in the backfill loop + before wrapping — not post-hoc (Codex P2)."""
    captured = {}

    async def _fake_impl(
        prompt,
        limit=5,
        *,
        rerank=False,
        extra_fts_terms=None,
        filter_noise=False,
        kb_slots=None,
        rerank_timeout_s=None,
        stats=None,
    ):
        captured["filter_noise"] = filter_noise
        return []

    with (
        patch("genesis.mcp.memory.core._memory_mod", return_value=_FakeMod()),
        patch("genesis.mcp.memory.core._proactive_impl", new=_fake_impl),
    ):
        await P.proactive_context(prompt="what did we decide about the recall reranker")

    assert captured["filter_noise"] is True


async def test_proactive_context_surfaces_engine_stats():
    """Rerank telemetry flows from the engine's stats sink into the response:
    ``engine.reranked`` mirrors EXECUTED (not requested), ``timings_ms.rerank``
    carries the stage latency, and a timebox expiry is flagged."""

    async def _fake_impl(
        prompt,
        limit=5,
        *,
        rerank=False,
        extra_fts_terms=None,
        filter_noise=False,
        kb_slots=None,
        rerank_timeout_s=None,
        stats=None,
    ):
        if stats is not None:
            stats["rerank_executed"] = True
            stats["rerank_ms"] = 123.4
        return []

    with (
        patch("genesis.mcp.memory.core._memory_mod", return_value=_FakeMod()),
        patch("genesis.mcp.memory.core._proactive_impl", new=_fake_impl),
    ):
        resp = await P.proactive_context(prompt="what did we decide about voice STT")

    assert resp["engine"]["reranked"] is True
    assert resp["engine"]["rerank_timed_out"] is False
    assert resp["timings_ms"]["rerank"] == 123.4

    async def _timed_out_impl(
        prompt,
        limit=5,
        *,
        rerank=False,
        extra_fts_terms=None,
        filter_noise=False,
        kb_slots=None,
        rerank_timeout_s=None,
        stats=None,
    ):
        if stats is not None:
            stats["rerank_executed"] = False
            stats["rerank_timed_out"] = True
            stats["rerank_ms"] = 1000.2
        return []

    with (
        patch("genesis.mcp.memory.core._memory_mod", return_value=_FakeMod()),
        patch("genesis.mcp.memory.core._proactive_impl", new=_timed_out_impl),
    ):
        resp = await P.proactive_context(prompt="what did we decide about voice STT")

    assert resp["engine"]["reranked"] is False
    assert resp["engine"]["rerank_timed_out"] is True
    assert resp["timings_ms"]["rerank"] == 1000.2


def test_is_proactive_noise_predicate():
    """provenance.is_proactive_noise — garbage from any collection OR a
    non-intentional knowledge_base hit; intentional KB + clean episodic kept."""
    from genesis.memory.provenance import is_proactive_noise as noise

    # garbage content from ANY collection (operates on RAW, unwrapped content)
    assert noise("episodic_memory", None, '{"drift_detected": 1}') is True
    assert noise("episodic_memory", None, "---\ntype: obs\n") is True
    assert noise("episodic_memory", None, None) is True
    # non-intentional knowledge_base
    assert noise("knowledge_base", "surplus", "clean text") is True
    assert noise("knowledge_base", "recon", "clean text") is True
    assert noise("knowledge_base", None, "clean text") is True
    # intentional knowledge_base survives (incl. dashboard file/URL uploads =
    # source_pipeline "curated" — genesis.knowledge.ingest_upload)
    assert noise("knowledge_base", "knowledge_ingest", "clean text") is False
    assert noise("knowledge_base", "reference_store", "clean text") is False
    assert noise("knowledge_base", "curated", "clean text") is False
    # clean episodic survives
    assert noise("episodic_memory", None, "a normal memory") is False


def _rr(mid, collection, content, source_pipeline=None):
    from genesis.memory.types import RetrievalResult

    return RetrievalResult(
        memory_id=mid,
        content=content,
        source="",
        memory_type="fact",
        score=0.5,
        vector_rank=1,
        fts_rank=None,
        activation_score=0.0,
        payload={},
        collection=collection,
        source_pipeline=source_pipeline,
    )


async def _run_impl(candidates, *, capture=None, **kwargs):
    """Run the real _proactive_impl over ``candidates`` with the external
    collaborators (retriever / graph expansion / immunity) mocked out. If
    ``capture`` is a dict, the ``skip_writeback`` predicate recall received is
    stored under ``capture['skip_writeback']``."""
    from genesis.mcp.memory import core as C

    class _Retriever:
        async def recall(self, *a, skip_writeback=None, **k):
            if capture is not None:
                capture["skip_writeback"] = skip_writeback
            return list(candidates)

    fake_mod = SimpleNamespace(
        _retriever=_Retriever(),
        _db=MagicMock(),
        _require_init=lambda: None,
    )

    async def _maybe_expand(db, kept, surface):
        return list(kept)  # no graph neighbors

    async def _record(*a, **k):
        return None

    with (
        patch.object(C, "_memory_mod", return_value=fake_mod),
        patch.object(C.graph_expansion, "maybe_expand", new=_maybe_expand),
        patch.object(C.immunity_shadow, "should_enforce_drop", return_value=False),
        patch.object(C.immunity_shadow, "item_is_blockable", return_value=False),
        patch.object(C.immunity_shadow, "is_dispatched_session_env", return_value=False),
        patch.object(C.immunity_shadow, "record_would_block", new=_record),
    ):
        return await C._proactive_impl("q", **kwargs)


async def test_proactive_impl_filter_noise_backfills_and_predates_wrap():
    """filter_noise drops garbage + non-intentional KB INSIDE the backfill loop
    (a dropped noisy top-K item is replaced by the next safe candidate — Codex P2
    #1) and BEFORE wrapping (raw content is checked — Codex P2 #2). filter_noise
    =False leaves them, proving the flag gates the behavior (MCP tool unchanged)."""
    candidates = [
        _rr("kbsurplus", "knowledge_base", "surplus insight", "surplus"),
        _rr("garbageep", "episodic_memory", '{"operation": "store"}'),
        _rr("cleanep01", "episodic_memory", "clean memory one"),
        _rr("cleanep02", "episodic_memory", "clean memory two"),
    ]
    filtered = await _run_impl(candidates, limit=2, filter_noise=True)
    unfiltered = await _run_impl(candidates, limit=2, filter_noise=False)

    fids = [d["memory_id"] for d in filtered]
    uids = [d["memory_id"] for d in unfiltered]
    # backfill: the two noise items are dropped, deeper clean rows fill both slots
    assert fids == ["cleanep01", "cleanep02"], fids
    # without the flag the top-2 (incl. noise) survive → the flag gates it
    assert "kbsurplus" in uids and "garbageep" in uids


async def test_proactive_impl_kb_slot_cap_backfills():
    """kb_slots caps knowledge_base hits IN the loop, backfilling from deeper safe
    candidates instead of leaving the prompt under-filled (Codex #1169)."""
    candidates = [
        _rr("kb1", "knowledge_base", "kb one", "knowledge_ingest"),
        _rr("kb2", "knowledge_base", "kb two", "knowledge_ingest"),
        _rr("ep1", "episodic_memory", "episodic one"),
        _rr("ep2", "episodic_memory", "episodic two"),
    ]
    out = await _run_impl(candidates, limit=3, filter_noise=True, kb_slots=1)
    ids = [d["memory_id"] for d in out]
    # kb2 is over the 1-slot cap → dropped + backfilled with ep2, so 3 items land.
    assert ids == ["kb1", "ep1", "ep2"], ids
    # kb_slots=None (the MCP-tool default) → no cap.
    out2 = await _run_impl(candidates, limit=3)
    assert [d["memory_id"] for d in out2] == ["kb1", "kb2", "ep1"]


async def test_proactive_impl_noise_skips_writeback():
    """A noise-dropped candidate must skip recall's retrieved_count write-back so
    filtered content can't farm ranking energy from every session (Codex #1169)."""
    capture: dict = {}
    await _run_impl([], capture=capture, limit=3, filter_noise=True)
    skip = capture["skip_writeback"]
    assert skip(_rr("g", "episodic_memory", '{"operation": "x"}')) is True  # garbage
    assert skip(_rr("kb", "knowledge_base", "clean", "surplus")) is True  # noisy KB
    assert skip(_rr("ok", "episodic_memory", "a normal memory")) is False  # kept
    # With filter_noise off, the noise predicate does not skip write-backs.
    capture2: dict = {}
    await _run_impl([], capture=capture2, limit=3)
    assert capture2["skip_writeback"](_rr("g", "episodic_memory", '{"operation": "x"}')) is False


def test_is_garbage_predicate():
    """provenance.is_garbage — the shared guard used by the engine + degraded hook."""
    from genesis.memory.provenance import is_garbage

    assert is_garbage(None) is True
    assert is_garbage("") is False
    assert is_garbage("a normal memory") is False
    assert is_garbage('{"drift_detected": 1}') is True
    assert is_garbage('   {"operation": "store"}') is True  # leading whitespace tolerated
    assert is_garbage('{"type": "x"}') is True
    assert is_garbage("---\ntype: observation\n") is True
    assert is_garbage("{just braces, no json keys}") is False
    assert is_garbage("--- not frontmatter, just dashes") is False
