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
