"""The self-reported final status: parse, strip, and map to an outcome."""

from __future__ import annotations

import json

import pytest

from genesis.cc import final_status
from genesis.cc.final_status import FinalStatus, outcome_of, split


def _block(body: str) -> str:
    return f"```final_status\n{body}\n```"


def _report(status: str, reason: str = "why") -> str:
    return _block(json.dumps({"status": status, "reason": reason}))


@pytest.mark.parametrize("status", final_status.REPORTED)
def test_a_trailing_block_is_split_off_and_parsed(status):
    text, report = split("All done.\n\n" + _report(status, "it went fine"))
    assert text == "All done."
    assert report == FinalStatus(status, "it went fine")


def test_text_without_a_block_is_returned_unchanged():
    raw = 'Here is the answer.\n```json\n{"a": 1}\n```\n'
    assert split(raw) == (raw, None)


def test_empty_text_has_no_report():
    assert split("") == ("", None)


def test_a_block_followed_by_more_text_is_left_in_place():
    raw = _report("completed") + "\n\nOne more thing."
    assert split(raw) == (raw, None)


def test_trailing_whitespace_after_the_block_is_allowed():
    text, report = split("ok\n" + _report("blocked") + "\n\n  \n")
    assert text == "ok"
    assert report.status == "blocked"


def test_only_the_last_block_is_read():
    raw = "quoted:\n" + _report("failed", "old") + "\nthen\n" + _report("completed", "new")
    text, report = split(raw)
    assert report == FinalStatus("completed", "new")
    assert text == "quoted:\n" + _report("failed", "old") + "\nthen"


@pytest.mark.parametrize(
    "body",
    ["not json", "[1, 2]", '{"status": "done"}', '{"reason": "no status"}', '{"status": 3}'],
)
def test_a_malformed_trailing_block_is_still_removed(body):
    text, report = split("answer\n" + _block(body))
    assert text == "answer"
    assert report == FinalStatus(final_status.MALFORMED)


def test_json_output_is_byte_identical_after_the_split():
    """A JSON-output caller's parser must see exactly what it saw before."""
    payload = json.dumps({"proposals": [], "focus_summary": "x"}, indent=2)
    for raw in (payload, f"```json\n{payload}\n```"):
        text, report = split(raw + "\n\n" + _report("completed"))
        assert text == raw
        assert report.status == "completed"


def test_a_tag_in_the_middle_of_a_line_is_not_the_block():
    raw = 'see ```final_status\n{"status": "completed"}\n```'
    assert split(raw) == (raw, None)


def test_a_pathologically_nested_block_is_malformed_not_a_crash():
    _, report = split(_block("[" * 100_000))
    assert report == FinalStatus(final_status.MALFORMED)


def test_a_long_reason_is_cut_to_the_limit():
    _, report = split(_report("blocked", "x" * 2000))
    assert len(report.reason) == final_status.REASON_MAX_CHARS


def test_a_non_string_reason_becomes_empty():
    _, report = split(_block('{"status": "failed", "reason": {"nested": 1}}'))
    assert report == FinalStatus("failed", "")


def test_the_contract_example_parses():
    example = final_status.CONTRACT.split("```final_status\n", 1)[1].split("\n```", 1)[0]
    assert json.loads(example)["status"] in final_status.REPORTED


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        (None, (None, None)),
        (FinalStatus("completed", "fine"), ("finished", "fine")),
        (FinalStatus("blocked", "denied a tool"), ("blocked", "denied a tool")),
        (FinalStatus("failed", ""), ("failed", None)),
        (FinalStatus(final_status.MALFORMED), ("unreported", "malformed_final_status")),
    ],
)
def test_outcome_of_maps_each_report(report, expected):
    assert outcome_of(report) == expected


@pytest.mark.parametrize("junk", [object(), "completed", FinalStatus("done")])
def test_anything_else_counts_as_no_report(junk):
    assert outcome_of(junk) == (None, None)


def test_every_mapped_outcome_is_in_the_recorder_vocabulary():
    from genesis.db.crud.cc_sessions import OUTCOMES

    for status in (*final_status.REPORTED, final_status.MALFORMED):
        assert outcome_of(FinalStatus(status))[0] in OUTCOMES
