"""Synthetic corpus factories; no transport or credentials dependencies."""

import json
from pathlib import Path

from genesis.eval.qualification import corpus
from genesis.eval.rubrics import list_rubrics
from tests.test_eval.qualification_reference_fixtures import provenance, relevance_case, rubric_case


def novelty_case(index, target):
    """Two cross-type candidates; the rendered order is [b, a] (b is most similar)."""
    return {
        "id": f"novelty-{index}",
        "expected_target": target,
        "new": {
            "task_type": f"new-task-{index}",
            "principle": f"Synthetic new playbook {index}",
            "steps": ["Run synthetic command"],
            "embedding": [1, 0],
        },
        "existing": [
            {
                "id": "candidate-a",
                "task_type": f"task-a-{index}",
                "principle": f"Synthetic weaker match {index}",
                "steps": ["Do A"],
                "embedding": [0.7, 0.7],
            },
            {
                "id": "candidate-b",
                "task_type": f"task-b-{index}",
                "principle": f"Synthetic stronger match {index}",
                "steps": ["Do B"],
                "embedding": [1, 0],
            },
        ],
        "reference_provenance": provenance(corpus.NOVELTY),
    }


def small_corpus(per_class=1, novelty=True):
    """Every contract, ``per_class`` cases in each class (coverage incomplete)."""
    cases = {
        r.name: [rubric_case(r, f"{c}{i}", c) for c in (False, True) for i in range(per_class)]
        for r in list_rubrics()
    }
    cases[corpus.RELEVANCE] = [
        relevance_case(f"{c}{i}", c) for c in (False, True) for i in range(per_class)
    ]
    if novelty:
        cases[corpus.NOVELTY] = [novelty_case(f"d{i}", None) for i in range(per_class)] + [
            novelty_case(f"r{i}", "candidate-b") for i in range(per_class)
        ]
    return cases


def write_corpus(directory: Path, cases: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, rows in cases.items():
        (directory / f"{name}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return directory
