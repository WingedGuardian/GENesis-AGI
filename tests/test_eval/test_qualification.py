"""Offline qualification: corpus, gate, contract adapters and the check CLI."""

from __future__ import annotations

import inspect
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from genesis.eval import calibration, j9_aggregator, j9_batch
from genesis.eval.qualification import contracts, corpus, run
from genesis.eval.qualification.__main__ import main
from genesis.eval.qualification.evidence import Incomplete
from genesis.eval.rubrics import list_rubrics
from genesis.learning.procedural import extractor
from genesis.routing.types import RoutingResult
from tests.test_eval.qualification_fixtures import (
    novelty_case,
    relevance_case,
    rubric_case,
    small_corpus,
    write_corpus,
)


@pytest.fixture
def no_http(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("an offline command attempted HTTP")

    monkeypatch.setattr(httpx.AsyncClient, "send", refuse)
    monkeypatch.setattr(httpx.Client, "send", refuse)


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


def test_check_cli_is_offline_and_reports_counts(tmp_path, capsys, no_http):
    directory = write_corpus(tmp_path / "corpus", small_corpus())
    code = main(["check", "--corpus", str(directory), "--temp-root", str(tmp_path / "t")])
    result = json.loads(capsys.readouterr().out)
    assert code == 2 and result["status"] == "incomplete"
    assert result["corpus"][corpus.NOVELTY] == {"cases": 2, "true": 1, "false": 1}
    assert len(result["coverage_issues"]) == len(corpus.versions())


def test_check_reports_missing_contract_files(tmp_path, capsys, no_http):
    cases = {k: v for k, v in small_corpus(per_class=25).items() if k == corpus.RELEVANCE}
    directory = write_corpus(tmp_path / "corpus", cases)
    assert main(["check", "--corpus", str(directory), "--temp-root", str(tmp_path / "t")]) == 2
    issues = json.loads(capsys.readouterr().out)["coverage_issues"]
    assert len(issues) == len(corpus.versions()) - 1
    assert all("no corpus file" in issue for issue in issues)


def test_check_rejects_novelty_case_that_never_reaches_the_judge(tmp_path, capsys, no_http):
    cases = {corpus.NOVELTY: [novelty_case("d0", None)]}
    for row in cases[corpus.NOVELTY][0]["existing"]:
        row["embedding"] = [0, 1]  # below the cross-type prefilter: no LLM call
    directory = write_corpus(tmp_path / "corpus", cases)
    assert main(["check", "--corpus", str(directory), "--temp-root", str(tmp_path / "t")]) == 2
    assert "exactly one judge request" in json.loads(capsys.readouterr().out)["reason"]


def rows(name, outcomes):
    """``outcomes``: (label, prediction, error, local) per case."""
    cases, result = [], {}
    for index, (label, prediction, error, local) in enumerate(outcomes):
        key = "expected_target" if name == corpus.NOVELTY else "user_passed"
        value = ("candidate-b" if label else None) if name == corpus.NOVELTY else label
        cases.append({"id": str(index), key: value})
        result[str(index)] = run._record(
            name, cases[-1], {"prediction": prediction, "error": error}, local
        )
    return cases, result


def judge_rows(right_false=25, right_true=25, n=25, error=0, local=0):
    outcomes = [(False, i >= right_false, None, None) for i in range(n)]
    outcomes += [(True, i < right_true, None, None) for i in range(n)]
    for i in range(error):
        outcomes[i] = (False, None, "judge_parse_fail", None)
    for i in range(local):
        outcomes[-1 - i] = (True, None, None, "unanswered")
    return outcomes


@pytest.mark.parametrize(
    ("outcomes", "status"),
    [
        (judge_rows(), "pass"),
        (judge_rows(right_false=20, right_true=20), "pass"),  # exactly 0.8 per class
        (judge_rows(right_false=19), "fail"),
        (judge_rows(n=100, right_false=79, right_true=100), "fail"),  # 0.79 < 0.8
        (judge_rows(error=1), "fail"),
        (judge_rows(local=1), "incomplete"),
        (judge_rows(n=24), "incomplete"),
    ],
)
def test_judge_gate_boundaries(outcomes, status):
    name = list_rubrics()[0].name
    assert run.gate(name, *rows(name, outcomes))["status"] == status


def novelty_rows(false_merge=False, wrong_target=False, misses=0, local=False):
    outcomes = [(False, None, None, None) for _ in range(300)]
    outcomes += [(True, "candidate-b", None, None) for _ in range(100)]
    if false_merge:
        outcomes[0] = (False, "candidate-b", None, None)
    if wrong_target:
        outcomes[-1] = (True, "candidate-a", None, None)
    for i in range(misses):
        outcomes[300 + i] = (True, None, None, None)
    if local:
        outcomes[1] = (False, None, None, "unanswered")
    return outcomes


@pytest.mark.parametrize(
    ("outcomes", "status"),
    [
        (novelty_rows(), "pass"),
        (novelty_rows(misses=20), "pass"),
        (novelty_rows(misses=21), "fail"),
        (novelty_rows(false_merge=True), "fail"),
        (novelty_rows(wrong_target=True), "fail"),
        (novelty_rows(local=True), "incomplete"),
        (novelty_rows(local=True, wrong_target=True), "fail"),  # zero tolerance wins
    ],
)
def test_novelty_gate_boundaries(outcomes, status):
    assert run.gate(corpus.NOVELTY, *rows(corpus.NOVELTY, outcomes))["status"] == status


def test_worst_status_order():
    """A failed repetition is decided, so fail outranks incomplete."""
    assert run.worst(["pass", "fail"]) == "fail"
    assert run.worst(["fail", "incomplete", "pass"]) == "fail"
    assert run.worst(["incomplete", "pass"]) == "incomplete"
    assert run.worst([]) == "incomplete"
    # Across independent routes or aliases, remaining work is what the top level says.
    assert run.worst(["fail", "incomplete"], run.OVERALL) == "incomplete"
    assert run.worst(["fail", "pass"], run.OVERALL) == "fail"


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


class Stub:
    def __init__(self, content):
        self.content, self.calls = content, []

    async def route_call(self, call_site_id, messages, **kwargs):
        self.calls.append((messages, self.content))
        return RoutingResult(success=True, call_site_id=call_site_id, content=self.content)


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


@pytest.mark.parametrize(
    ("content", "clamped"), [('{"score": 1.5}', True), ('{"score": 1}', False)]
)
def test_clamped_raw_scores_are_counted(content, clamped):
    assert run._clamped(list_rubrics()[0].name, content) is clamped
    relevance = content.replace("score", "relevance")
    assert run._clamped(corpus.RELEVANCE, relevance) is clamped


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
