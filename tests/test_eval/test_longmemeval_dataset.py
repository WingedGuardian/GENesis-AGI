"""Tests for the LongMemEval dataset loader (WS-1 A4)."""

from __future__ import annotations

import json

import pytest

from genesis.eval.longmemeval.dataset import (
    LongMemEvalInstance,
    load_oracle,
)

# Tiny synthetic fixture mirroring the real oracle schema (no private data).
_SYNTHETIC = [
    {
        "question_id": "abc123",
        "question_type": "single-session-user",
        "question": "What degree did I graduate with?",
        "answer": "Business Administration",
        "question_date": "2023/05/23 (Tue) 19:11",
        "haystack_dates": ["2023/05/01 (Mon) 10:00"],
        "haystack_session_ids": ["sess_1"],
        "haystack_sessions": [
            [
                {"role": "user", "content": "Hi there", "has_answer": False},
                {
                    "role": "user",
                    "content": "I graduated with a degree in Business Administration.",
                    "has_answer": True,
                },
                {"role": "assistant", "content": "Congrats!", "has_answer": True},
            ],
        ],
        "answer_session_ids": ["answer_sess_1"],
    },
    {
        "question_id": "def456_abs",
        "question_type": "single-session-preference",
        "question": "What is my favorite color?",
        "answer": "The user never stated a favorite color.",
        "question_date": "2023/06/01 (Thu) 08:00",
        "haystack_dates": ["2023/05/20 (Sat) 09:00"],
        "haystack_session_ids": ["sess_2"],
        "haystack_sessions": [
            [{"role": "user", "content": "Nice weather.", "has_answer": False}],
        ],
        "answer_session_ids": [],
    },
]


@pytest.fixture
def oracle_file(tmp_path):
    p = tmp_path / "oracle.json"
    p.write_text(json.dumps(_SYNTHETIC))
    return p


def test_load_oracle_returns_instances(oracle_file):
    instances = load_oracle(oracle_file)
    assert len(instances) == 2
    assert all(isinstance(i, LongMemEvalInstance) for i in instances)


def test_instance_fields_parsed(oracle_file):
    inst = load_oracle(oracle_file)[0]
    assert inst.question_id == "abc123"
    assert inst.question_type == "single-session-user"
    assert inst.question == "What degree did I graduate with?"
    assert inst.answer == "Business Administration"
    assert inst.question_date == "2023/05/23 (Tue) 19:11"
    assert inst.haystack_session_ids == ["sess_1"]
    assert inst.haystack_dates == ["2023/05/01 (Mon) 10:00"]
    assert len(inst.haystack_sessions) == 1


def test_abstention_detected_from_suffix(oracle_file):
    a, b = load_oracle(oracle_file)
    assert a.is_abstention is False
    assert b.is_abstention is True  # "def456_abs"


def test_evidence_turns_extracted(oracle_file):
    inst = load_oracle(oracle_file)[0]
    ev = inst.evidence_turns()
    assert len(ev) == 2  # the two has_answer=True turns
    assert all(t.has_answer for t in ev)
    assert "Business Administration" in ev[0].content


def test_iter_turns_pairs_session_and_date(oracle_file):
    inst = load_oracle(oracle_file)[0]
    turns = list(inst.iter_turns())
    # 3 turns in the one session; each carries its session's date
    assert len(turns) == 3
    session_idx, turn, date = turns[0]
    assert session_idx == 0
    assert date == "2023/05/01 (Mon) 10:00"
    assert turn.role == "user"


def test_empty_content_turns_are_still_iterated(oracle_file):
    # loader does not filter; ingest decides what to skip
    inst = load_oracle(oracle_file)[1]
    assert len(list(inst.iter_turns())) == 1
