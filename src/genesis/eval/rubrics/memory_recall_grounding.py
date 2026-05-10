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
You are grading the *topical relevance* of a recalled memory to a query.
You are NOT grading the memory's truthfulness, its overall quality, its
freshness, or whether it should exist — only whether it is topically
related enough to the query that a downstream consumer (a human or
another LLM) COULD draw on it while responding.

Three rules, applied in order:

1. **Topic overlap is the bar, not keyword overlap.** A memory that
   shares the query's topic counts as relevant even if the wording
   differs. A memory that shares only surface keywords (same noun, same
   tool name) without addressing the query's actual subject is NOT
   relevant.

2. **You do not assess truth.** If the memory makes a claim that is
   wrong, biased, outdated, or self-contradictory, that does NOT make
   it irrelevant. A wrong-but-on-topic memory still scores as relevant.
   Memory hygiene is a separate concern handled elsewhere.

3. **Procedural / investigation / decision memories count as relevant**
   if they record activity or reasoning about the query's topic. They
   do not need to *answer* the query to be relevant; they need to
   provide on-topic context.

Query the user asked:
{query}

Memory that was recalled:
{actual}

Reference label (FYI only — describes the memory's recall position and
collection; do not let this bias your judgment):
{expected}

Score the topical relevance from 0.0 (off-topic / unrelated) through
0.5 (tangential but on-topic) to 1.0 (directly on-topic, clearly
related). Brief rationale required.

Respond with ONLY a JSON object, no markdown fences, no prose:
{{"score": <float 0.0-1.0>, "rationale": "<one sentence>"}}"""


MEMORY_RECALL_GROUNDING = Rubric(
    name="memory_recall_grounding",
    version="1.1.0",
    description=(
        "Grades topical relevance of a recalled memory to a query. "
        "Foundation rubric for Phase 2 CRAG borderline grading. "
        "v1.1.0: tightened to relevance-only after 1.0.0 calibration "
        "exposed user grading on truth+usefulness while the rubric only "
        "asked about relevance."
    ),
    prompt_template=_PROMPT,
    pass_threshold=0.6,
    extra_placeholders=("query",),
)


register_rubric(MEMORY_RECALL_GROUNDING)
