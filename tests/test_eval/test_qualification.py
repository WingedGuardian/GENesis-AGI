"""Corpus validation and production contract adapters, offline."""

import inspect
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from genesis.eval import calibration, j9_aggregator, j9_batch
from genesis.eval.qualification import contracts, corpus
from genesis.eval.qualification.evidence import Incomplete
from genesis.eval.rubrics import list_rubrics
from genesis.learning.procedural import extractor
from tests.test_eval.qualification_contract_fixtures import Stub
from tests.test_eval.qualification_fixtures import (
    novelty_case,
    relevance_case,
    rubric_case,
    small_corpus,
    write_corpus,
)


def test_corpus_loads_every_contract_and_counts(tmp_path):
    loaded = corpus.load(write_corpus(tmp_path / "corpus", small_corpus(per_class=2)))
    assert set(loaded) == set(corpus.versions())
    assert all(v == {"cases": 4, "true": 2, "false": 2} for v in corpus.counts(loaded).values())
    assert len(corpus.coverage(loaded)) == len(loaded)


def _mutations():
    rubric = list_rubrics()[0]
    yield (
        "draft label",
        rubric.name,
        lambda c: c["reference_provenance"].update(label_source="draft"),
    )
    yield (
        "stale rubric version",
        rubric.name,
        lambda c: c["reference_provenance"].update(rubric_version="0.0.0"),
    )
    for contract in (corpus.RELEVANCE, corpus.NOVELTY):
        yield (
            "draft label",
            contract,
            lambda c: c["reference_provenance"].update(label_source="draft"),
        )
    yield (
        "blank reviewer",
        corpus.RELEVANCE,
        lambda c: c["reference_provenance"].update(reviewer=" "),
    )
    yield "unlabelled rubric draft", rubric.name, lambda c: c.pop("user_passed")
    yield "non-bool label", corpus.RELEVANCE, lambda c: c.update(user_passed=1)
    yield "missing query", corpus.RELEVANCE, lambda c: c.pop("query")
    yield "absent target", corpus.NOVELTY, lambda c: c.update(expected_target="candidate-z")
    yield "nan embedding", corpus.NOVELTY, lambda c: c["new"].update(embedding=[float("nan")])
    yield "float64-only embedding", corpus.NOVELTY, lambda c: c["new"].update(embedding=[1e39])
    yield (
        "inconsistent repeated principle embedding",
        corpus.NOVELTY,
        lambda c: c["existing"][0].update(principle=c["existing"][1]["principle"]),
    )


@pytest.mark.parametrize(("why", "contract", "mutate"), list(_mutations()))
def test_corpus_rejects_unlabelled_or_invalid_cases(tmp_path, why, contract, mutate):
    cases = small_corpus()
    mutate(cases[contract][0])
    with pytest.raises(Incomplete):
        corpus.load(write_corpus(tmp_path / "corpus", cases))


@pytest.mark.parametrize("contract", [list_rubrics()[0].name, corpus.RELEVANCE, corpus.NOVELTY])
def test_corpus_rejects_duplicate_ids_and_questions(tmp_path, contract):
    cases = small_corpus()
    duplicate = dict(cases[contract][0])
    cases[contract].append(duplicate)
    with pytest.raises(Incomplete, match="duplicate"):
        corpus.load(write_corpus(tmp_path / "a", cases))
    cases[contract][-1] = {**duplicate, "id": "renamed"}
    with pytest.raises(Incomplete, match="duplicate"):
        corpus.load(write_corpus(tmp_path / "b", cases))


def test_unknown_contract_file_and_empty_directory_rejected(tmp_path):
    with pytest.raises(Incomplete):
        corpus.load(write_corpus(tmp_path / "empty", {}))
    stray = write_corpus(tmp_path / "stray", small_corpus())
    (stray / f"{corpus.RELEVANCE}.json").write_text("[]")  # misnamed: never silently skipped
    with pytest.raises(Incomplete, match="non-JSONL"):
        corpus.load(stray)
    with pytest.raises(Incomplete, match="unknown contract"):
        corpus.load(write_corpus(tmp_path / "odd", {"not_a_contract": [{"id": "x"}]}))


@pytest.mark.parametrize(
    ("false", "true", "ok"), [(25, 25, True), (24, 26, False), (26, 24, False)]
)
def test_rubric_and_relevance_coverage_floor(false, true, ok):
    rubric = list_rubrics()[0]
    cases = {
        rubric.name: [rubric_case(rubric, f"f{i}", False) for i in range(false)]
        + [rubric_case(rubric, f"t{i}", True) for i in range(true)],
        corpus.RELEVANCE: [relevance_case(f"f{i}", False) for i in range(false)]
        + [relevance_case(f"t{i}", True) for i in range(true)],
    }
    assert (corpus.coverage(cases) == []) is ok


@pytest.mark.parametrize(("distinct", "redundant", "ok"), [(300, 100, True), (299, 100, False)])
def test_novelty_coverage_floor(distinct, redundant, ok):
    cases = {
        corpus.NOVELTY: [novelty_case(f"d{i}", None) for i in range(distinct)]
        + [novelty_case(f"r{i}", "candidate-b") for i in range(redundant)]
    }
    assert (corpus.coverage(cases) == []) is ok


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        '{"redundant_with": 0}',
        '{"redundant_with": 3}',
        '{"redundant_with": true}',
        '{"redundant_with": 1.0}',
        '{"other": 1}',
        '{"redundant_with": 1, "redundant_with": 2}',
    ],
)
def test_invalid_raw_novelty_target_is_a_model_error(content):
    with pytest.raises(contracts.MalformedJudgment):
        contracts.raw_target(content, ["candidate-b", "candidate-a"])


def test_raw_novelty_target_reads_production_json_block():
    content = 'Answer:\n```json\n{"redundant_with": 2}\n```'
    assert contracts.raw_target(content, ["candidate-b", "candidate-a"]) == "candidate-a"
    assert contracts.raw_target('{"redundant_with": null}', ["candidate-b"]) is None


async def test_novelty_adapter_maps_rendered_candidates(tmp_path):
    case = novelty_case("r0", "candidate-b")
    async with contracts.Sandbox(tmp_path / "t") as sandbox:
        result = await contracts.novelty(case, Stub('{"redundant_with": 1}'), sandbox)
        broken = await contracts.novelty(case, Stub("prose"), sandbox)
    assert result == {"prediction": "candidate-b", "error": None}
    assert broken["prediction"] is None and broken["error"]


@pytest.mark.parametrize("score", [0, 0.499999, 0.5, 0.7, 1])
async def test_j9_decision_matches_production_aggregation(score):
    result = await contracts.relevance(
        relevance_case("x", True), Stub(json.dumps({"relevance": score}))
    )
    event = {
        "metrics": {
            "recall_event_id": "synthetic-recall",
            "memory_id": "synthetic-memory",
            "relevance": score,
            "rank": 0,
        }
    }
    with (
        patch.object(j9_aggregator, "_recall_entrenchment", AsyncMock(return_value={})),
        patch.object(j9_aggregator, "_pool_counts_safe", AsyncMock(return_value={})),
        patch.object(j9_aggregator.j9_eval, "get_events", AsyncMock(side_effect=[[event], []])),
    ):
        metrics, _ = await j9_aggregator._compute_memory_quality(None, "start", "end")
    assert result["prediction"] is (metrics["precision_at_5"] == 1)
    assert result["prediction"] is (metrics["hit_rate"] == 1)


def test_private_production_signatures_this_tool_depends_on():
    """The tool drives private production functions; a signature change must fail here."""
    assert list(inspect.signature(calibration._validate_references).parameters) == [
        "cases",
        "rubric",
    ]
    assert {"rubric", "golden_set_path", "router", "strict_references"} <= set(
        inspect.signature(calibration.run_calibration).parameters
    )
    assert list(inspect.signature(j9_batch.J9EvalBatchExecutor._judge_relevance).parameters)[
        :3
    ] == ["self", "query", "memory_content"]
    assert {"task_type", "new_principle", "new_steps", "embedder", "router"} <= set(
        inspect.signature(extractor._principle_is_novel).parameters
    )
    # candidate_mapping parses the rendered candidate list; pin its line shape.
    assert "  [{i}] task_type: {" in inspect.getsource(extractor._cross_type_duplicate)


@pytest.mark.parametrize("value", ["0.7", -1, 2, True, False, None, [], {}, 10**400])
async def test_relevance_rejects_coercible_or_out_of_domain_raw_values(value):
    result = await contracts.relevance(
        relevance_case("invalid-domain", True), Stub(json.dumps({"relevance": value}))
    )
    assert result["prediction"] is None and result["error"]


@pytest.mark.parametrize("adapter", ["relevance", "novelty"])
async def test_adapters_accept_reused_production_router(tmp_path, adapter):
    from genesis.routing.router import Router
    from genesis.routing.types import RoutingResult

    router = Router.__new__(Router)
    content = '{"relevance": 0.7}' if adapter == "relevance" else '{"redundant_with": 1}'
    router._route_call_inner = AsyncMock(
        return_value=RoutingResult(success=True, content=content, call_site_id="judge")
    )
    assert not hasattr(router, "calls")
    async with contracts.Sandbox(tmp_path) as sandbox:
        for index in range(2):
            if adapter == "relevance":
                result = await contracts.relevance(relevance_case(str(index), True), router)
                assert result == {"prediction": True, "error": None}
            else:
                result = await contracts.novelty(
                    novelty_case(str(index), "candidate-b"), router, sandbox
                )
                assert result == {"prediction": "candidate-b", "error": None}
    assert router._route_call_inner.await_count == 2


async def test_novelty_recording_router_can_be_reused(tmp_path):
    router = Stub('{"redundant_with": 1}')
    async with contracts.Sandbox(tmp_path) as sandbox:
        for index in range(2):
            result = await contracts.novelty(
                novelty_case(str(index), "candidate-b"), router, sandbox
            )
            assert result == {"prediction": "candidate-b", "error": None}
    assert len(router.calls) == 2


@pytest.mark.parametrize("position", ["new", "first", "second"])
@pytest.mark.parametrize("vector", [[1e-46], [-1e-46, 1e-46]])
def test_corpus_rejects_embeddings_that_pack_to_zero(position, vector):
    case = novelty_case("underflow", None)
    row = case["new"] if position == "new" else case["existing"][position == "second"]
    row["embedding"] = vector
    with pytest.raises(Incomplete, match="embedding"):
        corpus.validate_novelty(case)


@pytest.mark.parametrize("vector", [[1e-45], [1e-46, 1], [3.4028234663852886e38]])
def test_corpus_accepts_nonzero_float32_boundary_vectors(vector):
    case = novelty_case("float32-boundary", None)
    case["new"]["embedding"] = vector
    corpus.validate_novelty(case)


def test_invalid_utf8_corpus_is_incomplete_with_filename(tmp_path):
    path = tmp_path / f"{corpus.RELEVANCE}.jsonl"
    path.write_bytes(b"\xff\n")
    with pytest.raises(Incomplete, match=f"{corpus.RELEVANCE}.jsonl: invalid UTF-8"):
        corpus.load(tmp_path)


def test_utf8_corpus_loads_under_ascii_locale(tmp_path):
    case = relevance_case("utf8", True)
    case["query"] = "caf\u00e9\u0085\u2028\u2029 second part"
    (tmp_path / f"{corpus.RELEVANCE}.jsonl").write_text(
        json.dumps(case, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    program = """
import json, locale, sys
from pathlib import Path
from genesis.eval.qualification import corpus
assert locale.getencoding() in ('ANSI_X3.4-1968', 'ASCII', 'US-ASCII'), locale.getencoding()
print(json.dumps(corpus.load(Path(sys.argv[1]))['j9_relevance'][0]['query']))
"""
    environment = dict(
        os.environ,
        LC_ALL="C",
        PYTHONUTF8="0",
        PYTHONCOERCECLOCALE="0",
        LITELLM_LOCAL_MODEL_COST_MAP="True",
    )
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    child = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path)],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert json.loads(child.stdout) == case["query"]


async def test_novelty_preserves_production_circuit_gating_and_clears_stale_mapping(tmp_path):
    router = Stub('{"redundant_with": null}')
    router.candidate_ids = ["previous-case"]
    router.breakers = SimpleNamespace(chain_has_available=lambda chain: False)
    router.config = SimpleNamespace(
        call_sites={extractor._NOVELTY_CALL_SITE: SimpleNamespace(chain=["synthetic"])}
    )
    async with contracts.Sandbox(tmp_path) as sandbox:
        with pytest.raises(Incomplete, match="exactly one"):
            await contracts.novelty(novelty_case("circuit", None), router, sandbox)
    assert not router.calls and router.candidate_ids == []


async def test_failed_novelty_response_is_not_a_valid_null(tmp_path):
    from genesis.routing.types import RoutingResult

    router = Stub('{"redundant_with": null}')
    router.route_call = AsyncMock(
        return_value=RoutingResult(
            success=False, content=router.content, call_site_id="novelty", error="synthetic failure"
        )
    )
    async with contracts.Sandbox(tmp_path) as sandbox:
        result = await contracts.novelty(novelty_case("failed", None), router, sandbox)
    assert result["prediction"] is None and result["error"]
    assert router.candidate_ids == []


async def test_swallowed_novelty_transport_exception_remains_incomplete(tmp_path):
    router = Stub('{"redundant_with": null}')
    router.route_call = AsyncMock(side_effect=RuntimeError("synthetic transport failure"))
    async with contracts.Sandbox(tmp_path) as sandbox:
        with pytest.raises(Incomplete, match="exactly one"):
            await contracts.novelty(novelty_case("exception", None), router, sandbox)
    assert router.candidate_ids == []
