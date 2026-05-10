"""memory_recall_grounding rubric.

Grades whether a recalled memory actually grounds the answer to a query —
i.e. would a downstream consumer (the user, or another LLM synthesising
context) get useful, on-topic information from this memory given the
query?

This rubric is the foundation for Phase 2 (CRAG retrieval evaluator). It
is judged against memory + query pairs the user has hand-graded as
``relevant`` / ``not_relevant`` in
``tests/eval/golden/memory_recall_grounding.jsonl``.

The rubric is loaded with the package via ``eval.rubrics.__init__``.
"""

from __future__ import annotations

from genesis.eval.rubrics import Rubric, register_rubric

_PROMPT = """\
You are grading whether a recalled memory grounds the answer to a query.

A memory grounds an answer when it provides on-topic, useful information
that a downstream consumer could weave into their response — facts,
context, relevant background, prior decisions. A memory does NOT ground
an answer when it is off-topic, redundant with what the query already
states, generic boilerplate, or merely shares keywords without
substantive overlap.

Query the user asked:
{query}

Memory that was recalled:
{actual}

User's hand-graded label (for reference only — your job is to judge the
memory against the query, not to agree with the label):
{expected}

Score the memory's grounding from 0.0 (irrelevant / unhelpful) to 1.0
(directly grounds the answer). Brief rationale required.

Respond with ONLY a JSON object, no markdown fences, no prose:
{{"score": <float 0.0-1.0>, "rationale": "<one sentence>"}}"""


MEMORY_RECALL_GROUNDING = Rubric(
    name="memory_recall_grounding",
    version="1.0.0",
    description=(
        "Grades whether a recalled memory grounds the answer to a query. "
        "Foundation rubric for Phase 2 CRAG borderline grading."
    ),
    prompt_template=_PROMPT,
    pass_threshold=0.6,
    extra_placeholders=("query",),
)


register_rubric(MEMORY_RECALL_GROUNDING)
