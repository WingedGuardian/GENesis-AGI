"""Output quality rubric for the eval harness (Verified Autonomy L3).

An eval-harness scorer: grades an output on three dimensions — coherence
(internal consistency), relevance (addresses the intent), and completeness
(covers necessary points). Used by the eval/calibration harness to grade
autonomous outputs against a versioned rubric.

NOT a gate on the live ego proposal pipeline — the Opus realist is the sole
proposal gate. (A quality-gate use in the ego pipeline was removed; it was
redundant with the realist and fragile under judge-provider outages.)

Reference: doi.org/10.5281/zenodo.19096229, Section 6
"""

from __future__ import annotations

from genesis.eval.rubrics import Rubric, register_rubric

_PROMPT = """Score this AI-generated output on three quality dimensions.

OUTPUT:
{actual}

CONTEXT (what this output is meant to address):
{expected}

Score each dimension from 0.0 to 1.0:

1. **coherence** (weight 40%): Is the output internally consistent? Does it
   contradict itself? Are the claims logically connected? Does it make sense
   as a whole? Score 0.0 for self-contradictory, 1.0 for fully coherent.

2. **relevance** (weight 40%): Does the output address what was intended?
   Is it on-topic or does it drift into unrelated territory? Score 0.0 for
   completely off-topic, 1.0 for directly relevant.

3. **completeness** (weight 20%): Does the output cover the necessary points?
   Are there obvious gaps or missing considerations? Score 0.0 for severely
   incomplete, 1.0 for thorough.

Compute the final score as: 0.4 * coherence + 0.4 * relevance + 0.2 * completeness

Return ONLY a JSON object:
{{"coherence": <float>, "relevance": <float>, "completeness": <float>, "score": <float>, "rationale": "<brief explanation>"}}
"""

OUTPUT_QUALITY = Rubric(
    name="output_quality",
    version="1.0.0",
    description=(
        "Grades autonomous output on coherence (40%), relevance (40%), "
        "and completeness (20%). Outputs below threshold are held for review."
    ),
    prompt_template=_PROMPT,
    pass_threshold=0.6,
)

register_rubric(OUTPUT_QUALITY)
