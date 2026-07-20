"""Unit tests for the server-side proactive recall engine.

Covers the presentation layer that moved out of the old subprocess fork:
stance→budget, KB slot cap, the ``[Memory | ...]`` cc_hook renderer, the H-1
shadow projection, structured result rows (pre-bump retrieved_count), and the
full ``proactive_context`` wiring with a faked ``_proactive_impl`` + db.
Install-agnostic: no live retriever, Qdrant, network, or DB.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

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


def test_kb_cap_keeps_all_episodic_caps_kb():
    dicts = [
        {"memory_id": "e1", "collection": "episodic_memory"},
        {"memory_id": "k1", "collection": "knowledge_base"},
        {"memory_id": "k2", "collection": "knowledge_base"},
        {"memory_id": "e2", "collection": "episodic_memory"},
        {"memory_id": "k3", "collection": "knowledge_base"},
    ]
    kept = P._apply_kb_cap(dicts, kb_slots=1)
    ids = [d["memory_id"] for d in kept]
    assert ids == ["e1", "k1", "e2"]  # both episodic kept, only first KB


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
            "source_pipeline": "curated",
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
    assert resp["engine"]["reranked"] is True


async def test_proactive_context_passes_file_keywords_as_extra_fts_terms():
    captured = {}

    async def _fake_impl(prompt, limit=5, *, rerank=False, extra_fts_terms=None):
        captured["extra"] = extra_fts_terms
        captured["rerank"] = rerank
        captured["limit"] = limit
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
