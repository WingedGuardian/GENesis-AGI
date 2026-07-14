"""The reader step: retrieved memories + question -> a short hypothesis answer.

A fixed model (gpt-4o via OpenRouter) reads ONLY the recalled memories and
answers. The reader prompt is question-type-aware:

* default (factual QA + abstention): a concise answer with an explicit "I don't
  know" affordance so abstention (``_abs``) questions — whose evidence is absent
  by design — are answered correctly.
* ``single-session-preference``: these ask for a RECOMMENDATION tailored to the
  user's recalled preferences, so the reader is told to USE those preferences
  and make a concrete suggestion — NOT to abstain. (A generic QA prompt made the
  reader answer "I don't know" and tanked this category to ~0.05.)
* ``temporal-reasoning``: add a step-by-step-using-the-dated-memories nudge so
  interval/date arithmetic is worked out rather than guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from genesis.eval.longmemeval.client import DEFAULT_MODEL

if TYPE_CHECKING:
    from collections.abc import Sequence

_DEFAULT_SYSTEM = (
    "You are answering a question using ONLY the retrieved memory snippets from "
    "the user's past conversations. Answer concisely and directly. If the "
    "snippets do not contain the information needed, say you don't know rather "
    "than guessing."
)

_PREFERENCE_SYSTEM = (
    "The user is asking for a recommendation or suggestion. Using the retrieved "
    "memory snippets about their past conversations, personal details, and stated "
    "preferences, give a concrete, tailored recommendation that reflects what you "
    "know about them. Always make a specific suggestion informed by their "
    "preferences; never decline or claim you lack enough information."
)

_TEMPORAL_SUFFIX = (
    " Each memory is prefixed with its date. For questions about durations, "
    "intervals, or when something happened, reason step by step using those "
    "dates, then state the final answer."
)


def _system_for(question_type: str | None) -> str:
    if question_type == "single-session-preference":
        return _PREFERENCE_SYSTEM
    if question_type == "temporal-reasoning":
        return _DEFAULT_SYSTEM + _TEMPORAL_SUFFIX
    return _DEFAULT_SYSTEM


def build_answer_prompt(
    question: str,
    memories: Sequence[str],
    *,
    question_type: str | None = None,
) -> str:
    """Assemble the reader prompt from the question and recalled memories."""
    block = (
        "\n".join(f"- {m}" for m in memories)
        if memories
        else ("(no relevant memories were retrieved)")
    )
    system = _system_for(question_type)
    return f"{system}\n\nMEMORIES:\n{block}\n\nQUESTION: {question}\n\nANSWER:"


@dataclass(frozen=True)
class AnswerResult:
    hypothesis: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


def answer_question(
    question: str,
    memories: Sequence[str],
    *,
    client: object,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 256,
    question_type: str | None = None,
) -> AnswerResult:
    """Produce a hypothesis answer from the recalled memories."""
    prompt = build_answer_prompt(question, memories, question_type=question_type)
    completion = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        max_tokens=max_tokens,
    )
    usage = getattr(completion, "usage", None)
    return AnswerResult(
        hypothesis=(completion.choices[0].message.content or "").strip(),
        model=getattr(completion, "model", model),
        input_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
        output_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
    )
