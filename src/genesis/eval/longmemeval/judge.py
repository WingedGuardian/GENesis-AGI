"""LongMemEval LLM-as-judge — a verbatim port of upstream ``evaluate_qa.py``.

Comparability to the published LongMemEval numbers requires the exact judge
prompts and grading rule. The five templates below and ``grade_label`` are
copied character-for-character from the reference implementation
(``get_anscheck_prompt`` + ``label = 'yes' in eval_response.lower()``), with
call params (``temperature=0``, ``max_tokens=10``, model
``gpt-4o-2024-08-06``) matched.

The judge is deliberately INDEPENDENT of Genesis's cognitive routing: it calls
a plain OpenAI-compatible client (OpenRouter → the same ``gpt-4o-2024-08-06``
weights) so the metric never depends on Genesis's own model choices.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping

#: The standard LongMemEval judge model (upstream asserts on this exact id).
JUDGE_MODEL = "openai/gpt-4o-2024-08-06"

# --- Verbatim upstream templates (do NOT reword — comparability depends on it) ---

_QA_TEMPLATE = (
    "I will give you a question, a correct answer, and a response from a model. "
    "Please answer yes if the response contains the correct answer. Otherwise, "
    "answer no. If the response is equivalent to the correct answer or contains "
    "all the intermediate steps to get the correct answer, you should also answer "
    "yes. If the response only contains a subset of the information required by "
    "the answer, answer no. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel "
    "Response: {}\n\nIs the model response correct? Answer yes or no only."
)

_TEMPORAL_TEMPLATE = (
    "I will give you a question, a correct answer, and a response from a model. "
    "Please answer yes if the response contains the correct answer. Otherwise, "
    "answer no. If the response is equivalent to the correct answer or contains "
    "all the intermediate steps to get the correct answer, you should also answer "
    "yes. If the response only contains a subset of the information required by "
    "the answer, answer no. In addition, do not penalize off-by-one errors for "
    "the number of days. If the question asks for the number of days/weeks/months, "
    "etc., and the model makes off-by-one errors (e.g., predicting 19 days when "
    "the answer is 18), the model's response is still correct. \n\nQuestion: {}"
    "\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response "
    "correct? Answer yes or no only."
)

_KNOWLEDGE_UPDATE_TEMPLATE = (
    "I will give you a question, a correct answer, and a response from a model. "
    "Please answer yes if the response contains the correct answer. Otherwise, "
    "answer no. If the response contains some previous information along with an "
    "updated answer, the response should be considered as correct as long as the "
    "updated answer is the required answer.\n\nQuestion: {}\n\nCorrect Answer: {}"
    "\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
)

_PREFERENCE_TEMPLATE = (
    "I will give you a question, a rubric for desired personalized response, and "
    "a response from a model. Please answer yes if the response satisfies the "
    "desired response. Otherwise, answer no. The model does not need to reflect "
    "all the points in the rubric. The response is correct as long as it recalls "
    "and utilizes the user's personal information correctly.\n\nQuestion: {}\n\n"
    "Rubric: {}\n\nModel Response: {}\n\nIs the model response correct? Answer "
    "yes or no only."
)

_ABSTENTION_TEMPLATE = (
    "I will give you an unanswerable question, an explanation, and a response "
    "from a model. Please answer yes if the model correctly identifies the "
    "question as unanswerable. The model could say that the information is "
    "incomplete, or some other information is given but the asked information is "
    "not.\n\nQuestion: {}\n\nExplanation: {}\n\nModel Response: {}\n\nDoes the "
    "model correctly identify the question as unanswerable? Answer yes or no only."
)

_QA_TYPES = frozenset(
    {"single-session-user", "single-session-assistant", "multi-session"},
)


def anscheck_prompt(
    task: str,
    question: str,
    answer: str,
    response: str,
    *,
    abstention: bool,
) -> str:
    """Build the judge prompt for a question type (verbatim upstream logic)."""
    if abstention:
        return _ABSTENTION_TEMPLATE.format(question, answer, response)
    if task in _QA_TYPES:
        return _QA_TEMPLATE.format(question, answer, response)
    if task == "temporal-reasoning":
        return _TEMPORAL_TEMPLATE.format(question, answer, response)
    if task == "knowledge-update":
        return _KNOWLEDGE_UPDATE_TEMPLATE.format(question, answer, response)
    if task == "single-session-preference":
        return _PREFERENCE_TEMPLATE.format(question, answer, response)
    msg = f"Unsupported question type: {task!r}"
    raise NotImplementedError(msg)


def grade_label(raw_response: str) -> bool:
    """Upstream grading rule: correct iff ``'yes'`` appears in the verdict."""
    return "yes" in raw_response.strip().lower()


@dataclass(frozen=True)
class JudgeResult:
    label: bool
    model: str
    raw: str
    input_tokens: int = 0
    output_tokens: int = 0


class _OpenAILike(Protocol):
    chat: object  # .chat.completions.create(**kwargs)


def judge_answer(
    task: str,
    question: str,
    answer: str,
    response: str,
    *,
    abstention: bool,
    client: _OpenAILike,
    model: str = JUDGE_MODEL,
    extra_params: Mapping[str, object] | None = None,
) -> JudgeResult:
    """Grade one hypothesis with the gpt-4o judge via an OpenAI-compatible client."""
    prompt = anscheck_prompt(task, question, answer, response, abstention=abstention)
    kwargs: dict[str, object] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 10,
    }
    if extra_params:
        kwargs.update(extra_params)
    completion = client.chat.completions.create(**kwargs)
    raw = (completion.choices[0].message.content or "").strip()
    usage = getattr(completion, "usage", None)
    return JudgeResult(
        label=grade_label(raw),
        model=getattr(completion, "model", model),
        raw=raw,
        input_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
        output_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
    )
