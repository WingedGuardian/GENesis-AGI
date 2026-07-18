"""Orchestration test: run_longmemeval actually runs questions concurrently.

Guards the code-review finding that a synchronous OpenAI client called from
``async def`` without ``to_thread`` would block the event loop and silently
defeat ``concurrency``. With the ``asyncio.to_thread`` offload, multiple
questions' LLM calls overlap. Uses fakes only — no network.
"""

from __future__ import annotations

import hashlib
import math
import re
import threading
import time

import pytest

from genesis.eval.longmemeval.dataset import LongMemEvalInstance, Turn
from genesis.eval.longmemeval.query import QueryArm
from genesis.eval.longmemeval.runner import Arm, run_longmemeval
from genesis.qdrant.collections import VECTOR_DIM
from tests.test_eval.lme_fixtures import make_linkable_instance as _multi_evidence_instance

_TOKEN = re.compile(r"[a-z0-9]+")


class _HashingEmbedder:
    tracker = None

    async def embed(self, text: str) -> list[float]:
        vec = [0.0] * VECTOR_DIM
        for tok in _TOKEN.findall(text.lower()):
            h = int(hashlib.sha1(tok.encode()).hexdigest(), 16)  # noqa: S324
            vec[h % VECTOR_DIM] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    async def embed_batch(self, texts):
        return [await self.embed(t) for t in texts]


class _ConcurrencyTrackingClient:
    """Sync client whose create() sleeps and records max concurrent calls."""

    def __init__(self):
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0

        chat = type("Chat", (), {})()
        completions = type("Completions", (), {})()

        def create(**kwargs):
            with self._lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            time.sleep(0.15)  # simulate a blocking network round-trip
            with self._lock:
                self.active -= 1
            return _FakeCompletion("yes Business Administration")

        completions.create = create
        chat.completions = completions
        self.chat = chat


class _FakeChoice:
    def __init__(self, content):
        self.message = type("M", (), {"content": content})()


class _FakeCompletion:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]
        self.usage = type("U", (), {"prompt_tokens": 10, "completion_tokens": 2})()
        self.model = "openai/gpt-4o-2024-08-06"


def _instance(qid: str) -> LongMemEvalInstance:
    return LongMemEvalInstance(
        question_id=qid,
        question_type="single-session-user",
        question="What degree did I graduate with?",
        answer="Business Administration",
        question_date="2023/05/23 (Tue) 19:11",
        haystack_dates=["2023/05/01 (Mon) 10:00"],
        haystack_session_ids=["s1"],
        haystack_sessions=[
            [
                Turn("user", "I graduated with a degree in Business Administration.", True),
            ],
        ],
        answer_session_ids=["a1"],
    )


class _PromptRecordingClient:
    """Sync client that records every prompt and answers deterministically."""

    def __init__(self):
        self.prompts: list[str] = []
        self._lock = threading.Lock()

        chat = type("Chat", (), {})()
        completions = type("Completions", (), {})()

        def create(**kwargs):
            with self._lock:
                self.prompts.append(kwargs["messages"][0]["content"])
            return _FakeCompletion("yes Business Administration")

        completions.create = create
        chat.completions = completions
        self.chat = chat


@pytest.mark.asyncio
async def test_run_longmemeval_dumps_jsonl_and_anchors_date(tmp_path):
    """--dump-dir writes one JSONL per arm with per-question diagnostics, and
    the reader prompt carries the upstream 'Current Date:' anchor end-to-end."""
    import json as _json

    instances = [_instance("q1"), _instance("q2")]
    client = _PromptRecordingClient()
    summaries = await run_longmemeval(
        instances,
        db=None,
        arms=[Arm(QueryArm.RAW, rerank=False)],
        k=5,
        concurrency=2,
        client=client,
        embedding_provider=_HashingEmbedder(),
        dump_dir=tmp_path,
    )
    dump = tmp_path / "raw.jsonl"
    assert dump.exists()
    recs = [_json.loads(line) for line in dump.read_text().splitlines()]
    assert len(recs) == 2
    for rec in recs:
        assert rec["arm"] == "raw"
        assert rec["query"]
        assert rec["recalled_ids"]
        assert rec["evidence_coverage"] == 1.0  # single evidence turn, k=5
        assert rec["hypothesis"]
        assert "judged_correct" in rec
    # dump path is recorded on the persisted summary metadata
    assert summaries["raw"].metadata["dump_path"] == str(dump)
    # reader prompts (not judge prompts) carry the upstream date anchor
    reader_prompts = [p for p in client.prompts if "MEMORIES:" in p]
    assert reader_prompts
    assert all("Current Date: 2023/05/23 (Tue) 19:11" in p for p in reader_prompts)


@pytest.mark.asyncio
async def test_graph_arm_isolated_dual_store_and_expansion(tmp_path):
    """The graph arm runs on its OWN linked store (baseline store stays
    link-free: activation-connectivity would otherwise tint baseline ranking),
    and 1-hop expansion merges the linked-but-unretrieved evidence turn."""
    import json as _json

    instances = [_multi_evidence_instance("q1")]
    client = _PromptRecordingClient()
    summaries = await run_longmemeval(
        instances,
        db=None,
        arms=[
            Arm(QueryArm.RAW, rerank=False),
            Arm(QueryArm.RAW, rerank=False, graph=True),
        ],
        k=1,
        concurrency=1,
        client=client,
        embedding_provider=_HashingEmbedder(),
        dump_dir=tmp_path,
    )
    base = _json.loads((tmp_path / "raw.jsonl").read_text().splitlines()[0])
    graph = _json.loads((tmp_path / "raw+graph.jsonl").read_text().splitlines()[0])

    # baseline arm: link-free store, no expansion fields populated
    assert base["links_created"] == 0
    assert base["expanded_ids"] == []
    assert base["evidence_coverage"] == 0.5  # k=1 of 2 evidence turns

    # graph arm: links were created at ingest on its own store
    assert graph["links_created"] >= 1
    # top-K metrics stay baseline-comparable...
    assert graph["evidence_coverage"] == 0.5
    # ...and expansion pulls the linked sibling: full coverage post-expansion
    assert graph["expanded_ids"]
    assert graph["evidence_coverage_final"] == 1.0
    assert graph["evidence_recalled_final"] is True

    # summary means surface the graph diagnostics
    assert summaries["raw+graph"].scores["evidence_coverage_final_mean"] == 1.0
    assert summaries["raw+graph"].metadata["links_created_mean"] >= 1
    # the reader saw MORE memories in the graph arm (expanded context)
    reader_prompts = [p for p in client.prompts if "MEMORIES:" in p]
    assert max(p.count("\n- ") for p in reader_prompts) > min(
        p.count("\n- ") for p in reader_prompts
    )


@pytest.mark.asyncio
async def test_link_threshold_reaches_the_linker(tmp_path, caplog):
    """The link_threshold plumbing chain (run_longmemeval -> run_question ->
    ephemeral_store -> MemoryLinker) is real: a near-1.0 threshold makes the
    otherwise-linkable pair form ZERO links and fires the loud warning."""
    import json as _json

    instances = [_multi_evidence_instance("q1")]
    client = _PromptRecordingClient()
    with caplog.at_level("WARNING", logger="genesis.eval.longmemeval"):
        await run_longmemeval(
            instances,
            db=None,
            arms=[Arm(QueryArm.RAW, rerank=False, graph=True)],
            k=1,
            concurrency=1,
            client=client,
            embedding_provider=_HashingEmbedder(),
            dump_dir=tmp_path,
            link_threshold=0.999,
        )
    rec = _json.loads((tmp_path / "raw+graph.jsonl").read_text().splitlines()[0])
    assert rec["links_created"] == 0
    assert rec["expanded_ids"] == []
    assert any("zero links" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_graph_block_failure_keeps_baseline_results(tmp_path, caplog):
    """A graph-store failure skips ONLY the graph arms: completed (judged)
    baseline results are kept, and the skip ledger never records an attempted
    arm as 'NOT attempted' (per-arm skip attribution)."""
    import json as _json

    instances = [_multi_evidence_instance("q1")]
    client = _PromptRecordingClient()
    with caplog.at_level("ERROR", logger="genesis.eval.longmemeval"):
        summaries = await run_longmemeval(
            instances,
            db=None,
            arms=[
                Arm(QueryArm.RAW, rerank=False),
                Arm(QueryArm.RAW, rerank=False, graph=True),
            ],
            k=1,
            concurrency=1,
            client=client,
            embedding_provider=_HashingEmbedder(),
            dump_dir=tmp_path,
            link_threshold=5.0,  # invalid: MemoryLinker construction raises
        )
    # baseline arm: attempted and kept
    assert summaries["raw"].total_cases == 1
    assert summaries["raw"].skipped_cases == 0
    base = _json.loads((tmp_path / "raw.jsonl").read_text().splitlines()[0])
    assert "skipped" not in base
    # graph arm: skipped, loudly
    assert summaries["raw+graph"].skipped_cases == 1
    graph = _json.loads((tmp_path / "raw+graph.jsonl").read_text().splitlines()[0])
    assert graph["skipped"] is True
    assert any("graph store block failed" in r.message for r in caplog.records)


def test_select_arms_composition():
    from genesis.eval.longmemeval.runner import select_arms

    assert {a.label for a in select_arms()} == {
        "raw",
        "raw+rerank",
        "keyword",
        "keyword+rerank",
    }
    assert {a.label for a in select_arms(no_rerank=True, graph=True)} == {
        "raw",
        "keyword",
        "raw+graph",
        "keyword+graph",
    }
    assert len(select_arms(graph=True)) == 8


def test_filter_arms_selects_by_label_preserving_order():
    from genesis.eval.longmemeval.runner import filter_arms, select_arms

    arms = select_arms(no_rerank=True, graph=True)
    picked = filter_arms(arms, "raw,raw+graph")
    assert [a.label for a in picked] == ["raw", "raw+graph"]
    # whitespace tolerated; output keeps the arm-universe order, not CSV order
    assert [a.label for a in filter_arms(arms, " keyword , raw ")] == ["raw", "keyword"]


def test_filter_arms_rejects_unknown_and_empty():
    from genesis.eval.longmemeval.runner import filter_arms, select_arms

    arms = select_arms(no_rerank=True)  # raw, keyword only
    # unknown label names the selectable universe (raw+graph needs --graph)
    with pytest.raises(ValueError, match="raw\\+graph"):
        filter_arms(arms, "raw+graph")
    with pytest.raises(ValueError, match="no arms selected"):
        filter_arms(arms, " , ")


@pytest.mark.asyncio
async def test_execute_empty_arms_string_raises_not_widens(tmp_path):
    """`--arms ""` (e.g. an unset shell var) must raise, NEVER silently fall
    back to the full unfiltered universe — that would widen paid spend."""
    import json as _json

    from genesis.eval.longmemeval.cli import execute

    dataset = tmp_path / "oracle.json"
    dataset.write_text(
        _json.dumps(
            [
                {
                    "question_id": "q1",
                    "question_type": "single-session-user",
                    "question": "q?",
                    "answer": "a",
                    "haystack_sessions": [],
                },
            ],
        ),
    )
    with pytest.raises(ValueError, match="no arms selected"):
        await execute(
            dataset_path=dataset,
            no_rerank=True,
            persist=False,
            arms_only="",
        )


@pytest.mark.asyncio
async def test_duplicate_arm_labels_rejected():
    with pytest.raises(ValueError, match="duplicate arm labels"):
        await run_longmemeval(
            [],
            db=None,
            arms=[Arm(QueryArm.RAW, rerank=False), Arm(QueryArm.RAW, rerank=False)],
            client=object(),
        )


@pytest.mark.asyncio
async def test_graph_arm_warns_on_zero_links(tmp_path, caplog):
    """No-silent-caps parity: a graph arm whose store formed zero links must
    say so loudly (it would otherwise silently equal its baseline twin)."""
    instances = [_instance("q1")]  # single turn — nothing to link to
    client = _PromptRecordingClient()
    with caplog.at_level("WARNING", logger="genesis.eval.longmemeval"):
        await run_longmemeval(
            instances,
            db=None,
            arms=[Arm(QueryArm.RAW, rerank=False, graph=True)],
            k=5,
            concurrency=1,
            client=client,
            embedding_provider=_HashingEmbedder(),
            dump_dir=tmp_path,
        )
    assert any("zero links" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_run_longmemeval_runs_questions_concurrently():
    instances = [_instance(f"q{i}") for i in range(4)]
    client = _ConcurrencyTrackingClient()
    # one arm keeps the LLM-call count low; concurrency=3 across 4 questions
    summaries = await run_longmemeval(
        instances,
        db=None,
        arms=[Arm(QueryArm.RAW, rerank=False)],
        k=5,
        concurrency=3,
        client=client,
        embedding_provider=_HashingEmbedder(),
    )
    # sanity: all four questions produced a result in the single arm
    assert summaries["raw"].total_cases == 4
    # the offload lets multiple blocking LLM calls overlap; a blocking-on-loop
    # implementation would serialize to max_active == 1
    assert client.max_active >= 2


# ── WS2-0: retrieval-config variant seam ─────────────────────────────────────


@pytest.fixture
def _variants_clean():
    """Snapshot/restore the module-global VARIANTS registry so a test that
    registers a variant never leaks into another test."""
    from genesis.eval.longmemeval import runner

    saved = dict(runner.VARIANTS)
    try:
        yield
    finally:
        runner.VARIANTS.clear()
        runner.VARIANTS.update(saved)


def test_select_arms_variants_double_without_variant_of_variant(_variants_clean):
    from genesis.eval.longmemeval.runner import select_arms

    arms = select_arms(no_rerank=True, variants=["scope"])
    assert {a.label for a in arms} == {"raw", "keyword", "raw+scope", "keyword+scope"}
    # two variants each double the BASE arms only — never variant-of-variant
    labels = {a.label for a in select_arms(no_rerank=True, variants=["scope", "budget"])}
    assert labels == {
        "raw", "keyword",
        "raw+scope", "keyword+scope",
        "raw+budget", "keyword+budget",
    }
    assert not any("scope" in x and "budget" in x for x in labels)


def test_validate_variants_rejects_unknown_variant():
    from genesis.eval.longmemeval.query import QueryArm
    from genesis.eval.longmemeval.runner import Arm, validate_variants

    with pytest.raises(ValueError, match="unknown arm variant"):
        validate_variants([Arm(QueryArm.RAW, rerank=False, variant="nope")])


def test_validate_variants_rejects_unknown_recall_kwarg(_variants_clean):
    from genesis.eval.longmemeval.query import QueryArm
    from genesis.eval.longmemeval.runner import (
        Arm,
        ArmVariant,
        register_variant,
        validate_variants,
    )

    register_variant(ArmVariant(name="bad", recall_kwarg_names=("not_a_real_kwarg",)))
    with pytest.raises(ValueError, match="unknown recall kwargs"):
        validate_variants([Arm(QueryArm.RAW, rerank=False, variant="bad")])


def test_validate_variants_passes_registered_valid_kwargs(_variants_clean):
    from genesis.eval.longmemeval.query import QueryArm
    from genesis.eval.longmemeval.runner import (
        Arm,
        ArmVariant,
        register_variant,
        validate_variants,
    )

    register_variant(ArmVariant(name="ok", recall_kwarg_names=("wing",)))
    # 'wing' is a real recall() kwarg; baseline (no-variant) arms always pass
    validate_variants([
        Arm(QueryArm.RAW, rerank=False),
        Arm(QueryArm.RAW, rerank=False, variant="ok"),
    ])


class _RecordAllClient:
    """Sync client recording the full concatenated prompt of every call."""

    def __init__(self):
        self.prompts: list[str] = []
        self._lock = threading.Lock()
        chat = type("Chat", (), {})()
        completions = type("Completions", (), {})()

        def create(**kwargs):
            with self._lock:
                self.prompts.append(
                    " ".join(m.get("content", "") for m in kwargs["messages"]),
                )
            return _FakeCompletion("yes Business Administration")

        completions.create = create
        chat.completions = completions
        self.chat = chat


@pytest.mark.asyncio
async def test_run_arm_applies_variant_recall_kwargs_and_post_recall(_variants_clean):
    """A variant's recall_kwargs reach recall(), and its post_recall transforms
    the content list actually fed to the reader — proven directly via spies."""
    from genesis.eval.longmemeval.query import QueryArm
    from genesis.eval.longmemeval.runner import (
        Arm,
        ArmVariant,
        _run_arm,
        register_variant,
    )

    seen_recall_kwargs: dict = {}
    seen_post_recall: list = []

    class _Hit:
        def __init__(self, mid, content):
            self.memory_id = mid
            self.content = content

    class _Retriever:
        async def recall(self, query, *, source, limit, rerank, **extra):
            seen_recall_kwargs.update(extra)
            return [_Hit("m1", "alpha_memory"), _Hit("m2", "beta_memory")]

    class _ES:
        retriever = _Retriever()
        db = None

    class _Ingest:
        evidence_memory_ids = {"m1"}

    def _derive(instance, arm):
        return {"wing": "infrastructure"}

    def _trim(memories):
        seen_post_recall.append(list(memories))
        return memories[:1]  # keep only the top memory

    register_variant(
        ArmVariant(
            name="probe",
            recall_kwarg_names=("wing",),
            recall_kwargs=_derive,
            post_recall=_trim,
        ),
    )

    client = _RecordAllClient()
    arm = Arm(QueryArm.RAW, rerank=False, variant="probe")
    result = await _run_arm(_ES(), _Ingest(), _instance("q1"), arm, k=10, client=client)

    assert seen_recall_kwargs == {"wing": "infrastructure"}     # kwargs reached recall()
    assert seen_post_recall == [["alpha_memory", "beta_memory"]]  # got the recalled set
    # the trimmed list (only alpha) reached the reader; beta was dropped
    joined = " ".join(client.prompts)
    assert "alpha_memory" in joined
    assert "beta_memory" not in joined
    assert result.arm_label == "raw+probe"
