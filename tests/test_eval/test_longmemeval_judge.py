"""Tests for the LongMemEval judge (WS-1 A4).

The judge is a verbatim port of upstream ``evaluate_qa.py::get_anscheck_prompt``
plus its grading rule (``label = 'yes' in response.lower()``). Comparability to
the published numbers depends on the prompts matching exactly, so these tests
pin the exact template text.
"""

from __future__ import annotations

import pytest

from genesis.eval.longmemeval.judge import (
    anscheck_prompt,
    grade_label,
    judge_answer,
)


def test_single_session_and_multi_share_the_qa_template():
    for qtype in ("single-session-user", "single-session-assistant", "multi-session"):
        p = anscheck_prompt(qtype, "Q?", "A", "R", abstention=False)
        assert p.startswith(
            "I will give you a question, a correct answer, and a response",
        )
        assert "Question: Q?" in p
        assert "Correct Answer: A" in p
        assert "Model Response: R" in p
        assert p.rstrip().endswith("Answer yes or no only.")


def test_temporal_reasoning_has_offby_one_clause():
    p = anscheck_prompt("temporal-reasoning", "Q?", "A", "R", abstention=False)
    assert "off-by-one" in p


def test_knowledge_update_has_updated_answer_clause():
    p = anscheck_prompt("knowledge-update", "Q?", "A", "R", abstention=False)
    assert "updated answer" in p


def test_preference_uses_rubric_wording():
    p = anscheck_prompt("single-session-preference", "Q?", "RUBRIC", "R", abstention=False)
    assert "Rubric: RUBRIC" in p
    assert "rubric for desired personalized response" in p


def test_abstention_template_used_regardless_of_type():
    p = anscheck_prompt("single-session-user", "Q?", "EXPL", "R", abstention=True)
    assert "unanswerable" in p
    assert "Explanation: EXPL" in p


def test_unknown_type_raises():
    with pytest.raises(NotImplementedError):
        anscheck_prompt("no-such-type", "Q?", "A", "R", abstention=False)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("yes", True),
        ("Yes.", True),
        ("YES", True),
        ("no", False),
        ("No, incorrect", False),
        ("", False),
    ],
)
def test_grade_label(raw, expected):
    assert grade_label(raw) is expected


class _FakeChoice:
    def __init__(self, content):
        self.message = type("M", (), {"content": content})()


class _FakeCompletion:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]
        self.usage = type("U", (), {"prompt_tokens": 20, "completion_tokens": 1})()
        self.model = "openai/gpt-4o-2024-08-06"


class _FakeClient:
    """Mimics the OpenAI client surface the judge uses."""

    def __init__(self, content):
        self._content = content
        self.calls = []

        chat = type("Chat", (), {})()
        completions = type("Completions", (), {})()

        def create(**kwargs):
            self.calls.append(kwargs)
            return _FakeCompletion(self._content)

        completions.create = create
        chat.completions = completions
        self.chat = chat


def test_judge_answer_returns_label_and_uses_gpt4o(monkeypatch):
    client = _FakeClient("yes")
    result = judge_answer(
        "single-session-user",
        "Q?",
        "A",
        "R",
        abstention=False,
        client=client,
    )
    assert result.label is True
    assert result.model == "openai/gpt-4o-2024-08-06"
    # upstream call params: temperature 0, max_tokens 10, gpt-4o model id
    (call,) = client.calls
    assert call["temperature"] == 0
    assert call["max_tokens"] == 10
    assert call["model"] == "openai/gpt-4o-2024-08-06"


def test_judge_answer_no_verdict():
    client = _FakeClient("no")
    result = judge_answer(
        "temporal-reasoning",
        "Q?",
        "A",
        "R",
        abstention=False,
        client=client,
    )
    assert result.label is False
