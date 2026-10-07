"""Synthetic reference factories without corpus, transport or runner dependencies."""

from genesis.eval.j9_batch import _RELEVANCE_PROMPT_VERSION
from genesis.eval.rubrics import list_rubrics

RELEVANCE = "j9_relevance"
NOVELTY = "procedure_novelty"


def versions():
    # Registered rubric/J9 versions, plus the frozen novelty-v1 label contract.
    return {
        **{r.name: r.version for r in list_rubrics()},
        RELEVANCE: _RELEVANCE_PROMPT_VERSION,
        NOVELTY: "novelty-v1",
    }


def provenance(contract):
    return {
        "label_source": "human",
        "reviewer": "synthetic-owner",
        "rubric_version": versions()[contract],
    }


def rubric_case(rubric, index, passed):
    return {
        "id": f"{rubric.name}-{index}",
        "actual": f"Synthetic observation {index}",
        "expected": "Synthetic expected response",
        "user_passed": passed,
        "scorer_config": {
            "rubric_name": rubric.name,
            **{k: "Synthetic context" for k in rubric.extra_placeholders},
        },
        "reference_provenance": provenance(rubric.name),
    }


def relevance_case(index, passed):
    return {
        "id": f"relevance-{index}",
        "query": f"Synthetic query {index}",
        "memory_content": "Synthetic memory",
        "user_passed": passed,
        "reference_provenance": provenance(RELEVANCE),
    }


def small_corpus(per_class=1, novelty=False):
    """Rubric/J9 cases only; novelty factories land with corpus validation."""
    if novelty:
        raise ValueError("novelty fixtures require the corpus replacement")
    cases = {
        r.name: [rubric_case(r, f"{c}{i}", c) for c in (False, True) for i in range(per_class)]
        for r in list_rubrics()
    }
    cases[RELEVANCE] = [
        relevance_case(f"{c}{i}", c) for c in (False, True) for i in range(per_class)
    ]
    return cases
