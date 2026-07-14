"""Tests for the LongMemEval reader/answer step (WS-1 A4)."""

from __future__ import annotations

from genesis.eval.longmemeval.answer import answer_question, build_answer_prompt


class _FakeChoice:
    def __init__(self, content):
        self.message = type("M", (), {"content": content})()


class _FakeCompletion:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]
        self.usage = type("U", (), {"prompt_tokens": 120, "completion_tokens": 8})()
        self.model = "openai/gpt-4o-2024-08-06"


class _FakeClient:
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


def test_build_answer_prompt_includes_question_and_memories():
    p = build_answer_prompt(
        "What degree did I graduate with?",
        ["[user] I graduated with a degree in Business Administration."],
    )
    assert "What degree did I graduate with?" in p
    assert "Business Administration" in p
    # abstention affordance: reader is told it may say it doesn't know
    assert "don't know" in p.lower() or "do not know" in p.lower()


def test_answer_question_returns_hypothesis_and_tokens():
    client = _FakeClient("You graduated with a degree in Business Administration.")
    result = answer_question(
        "What degree did I graduate with?",
        ["[user] I graduated with a degree in Business Administration."],
        client=client,
    )
    assert "Business Administration" in result.hypothesis
    assert result.input_tokens == 120
    assert result.output_tokens == 8
    (call,) = client.calls
    assert call["temperature"] == 0
    assert call["model"] == "openai/gpt-4o-2024-08-06"


def test_answer_question_handles_empty_memories():
    client = _FakeClient("I don't know based on the available information.")
    result = answer_question("What is my cat's name?", [], client=client)
    assert result.hypothesis
    # still one call; prompt notes there are no memories
    assert len(client.calls) == 1


def test_default_prompt_keeps_dont_know_affordance():
    p = build_answer_prompt("What degree?", ["m"], question_type="single-session-user")
    assert "don't know" in p.lower() or "do not know" in p.lower()


def test_preference_prompt_personalizes_and_drops_abstention():
    p = build_answer_prompt(
        "Can you recommend some video editing resources?",
        ["[user] I use Adobe Premiere Pro."],
        question_type="single-session-preference",
    )
    low = p.lower()
    # preference: recommend + tailor to the user's known preferences
    assert "recommend" in low or "tailor" in low or "personal" in low or "preference" in low
    # must NOT tell the reader to abstain — that was the bug that scored ~0.05
    assert "don't know" not in low
    assert "do not know" not in low


def test_temporal_prompt_has_stepwise_reasoning():
    p = build_answer_prompt(
        "How many days between my two trips?",
        ["[2023/05/01] [user] first trip"],
        question_type="temporal-reasoning",
    )
    assert "step" in p.lower()


def test_answer_question_passes_question_type_through():
    client = _FakeClient("Try Premiere Pro tutorials on the official Adobe site.")
    answer_question(
        "Can you recommend video editing resources?",
        ["[user] I use Adobe Premiere Pro."],
        client=client,
        question_type="single-session-preference",
    )
    (call,) = client.calls
    prompt = call["messages"][0]["content"].lower()
    assert "don't know" not in prompt
