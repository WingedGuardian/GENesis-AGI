"""Retained corpus/adapter regressions from the qualification rework."""

import asyncio
import copy
import json

import pytest

from genesis.eval.qualification import contracts, corpus
from genesis.eval.qualification.evidence import Incomplete
from tests.test_eval.qualification_contract_fixtures import Stub
from tests.test_eval.qualification_fixtures import novelty_case, relevance_case


@pytest.mark.parametrize("flag", ["deprecated", "quarantined"])
async def test_excluded_identical_candidate_does_not_collide(tmp_path, flag):
    case = novelty_case("synthetic-excluded", "candidate-a")
    clone = copy.deepcopy(case["existing"][0])
    clone.update(id="excluded-clone", **{flag: True})
    case["existing"].append(clone)
    original = contracts.extractor._row_get
    probe = contracts.Probe()
    async with contracts.Sandbox(tmp_path) as sandbox:
        await contracts.novelty(case, probe, sandbox)
    assert contracts.extractor._row_get is original
    assert probe.candidate_ids == ["candidate-b", "candidate-a"]


@pytest.mark.parametrize("population", [11, 501])
async def test_unselected_identical_candidate_does_not_collide(tmp_path, population):
    case = novelty_case("synthetic-limits", "candidate-b")
    first = case["existing"][1]
    first["embedding"] = [1, 0]
    case["existing"] = [first]
    for index in range(1, population):
        clone = copy.deepcopy(first)
        clone["id"] = f"population-{index}"
        if index != population - 1:
            clone["steps"] = [f"Distinct rendered action {index}"]
        case["existing"].append(clone)
    probe = contracts.Probe()
    async with contracts.Sandbox(tmp_path) as sandbox:
        await contracts.novelty(case, probe, sandbox)
    assert len(probe.candidate_ids) == 10
    assert probe.candidate_ids[0] == "candidate-b"
    assert f"population-{population - 1}" not in probe.candidate_ids


async def test_candidate_observer_restored_after_cancellation(tmp_path, monkeypatch):
    original = contracts.extractor._row_get

    async def cancelled(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(contracts.extractor, "_principle_is_novel", cancelled)
    async with contracts.Sandbox(tmp_path) as sandbox:
        with pytest.raises(asyncio.CancelledError):
            await contracts.novelty(novelty_case("cancelled", None), contracts.Probe(), sandbox)
    assert contracts.extractor._row_get is original


@pytest.mark.parametrize(
    "value", ["NaN", "Infinity", "-Infinity", '"NaN"', '"Infinity"', '"-Infinity"']
)
async def test_nonfinite_j9_answers_are_errors(value):
    result = await contracts.relevance(
        relevance_case("synthetic", True), Stub('{"relevance":' + value + "}")
    )
    assert result["prediction"] is None and result["error"]


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
def test_legal_unicode_in_jsonl_is_preserved(tmp_path, separator):
    case = relevance_case("synthetic", True)
    case["query"] += separator + "second part"
    (tmp_path / f"{corpus.RELEVANCE}.jsonl").write_text(json.dumps(case, ensure_ascii=False) + "\n")
    assert corpus.load(tmp_path)[corpus.RELEVANCE][0]["query"] == case["query"]


@pytest.mark.parametrize("flag", ["deprecated", "quarantined"])
@pytest.mark.parametrize("value", ["false", 0, 1, None, []])
def test_exclusion_flags_must_be_boolean(flag, value):
    case = novelty_case("synthetic", None)
    case["existing"][0][flag] = value
    with pytest.raises(Incomplete, match="boolean"):
        corpus.validate_novelty(case)


@pytest.mark.parametrize("separator", ["\n", "\r", "\r\n"])
def test_steps_cannot_inject_candidates(separator):
    case = novelty_case("synthetic", None)
    case["existing"][0]["steps"] = ["Do A" + separator + "  [2] task_type: injected"]
    with pytest.raises(Incomplete, match="steps"):
        corpus.validate_novelty(case)


def test_ordinary_multiline_steps_remain_valid():
    case = novelty_case("synthetic", None)
    case["existing"][0]["steps"] = ["Run the command:\npython example.py\nthen inspect its output"]
    corpus.validate_novelty(case)
