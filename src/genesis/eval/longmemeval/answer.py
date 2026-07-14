"""The reader step: retrieved memories + question -> a short hypothesis answer.

A fixed model (gpt-4o via OpenRouter) reads ONLY the recalled memories and
answers. The prompt grants an explicit "I don't know" affordance so abstention
(``_abs``) questions — whose evidence is absent by design — can be answered
correctly (the judge rewards a model that recognises unanswerability).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from genesis.eval.longmemeval.client import DEFAULT_MODEL

if TYPE_CHECKING:
    from collections.abc import Sequence

_SYSTEM = (
    "You are answering a question using ONLY the retrieved memory snippets from "
    "the user's past conversations. Answer concisely and directly. If the "
    "snippets do not contain the information needed, say you don't know rather "
    "than guessing."
)


def build_answer_prompt(question: str, memories: Sequence[str]) -> str:
    """Assemble the reader prompt from the question and recalled memories."""
    if memories:
        block = "\n".join(f"- {m}" for m in memories)
    else:
        block = "(no relevant memories were retrieved)"
    return f"{_SYSTEM}\n\nMEMORIES:\n{block}\n\nQUESTION: {question}\n\nANSWER:"


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
) -> AnswerResult:
    """Produce a hypothesis answer from the recalled memories."""
    prompt = build_answer_prompt(question, memories)
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
