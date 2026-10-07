"""Corpus validation and production contract adapters, offline."""

import inspect
import json
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
