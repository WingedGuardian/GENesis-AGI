"""Owner-labelled reference corpus: one calibration-format JSONL file per contract.

``<corpus>/<contract>.jsonl``. Rubric files are calibration golden sets, checked
by the same strict validator ``run_calibration`` uses. Every case carries
``reference_provenance {label_source: "human", reviewer, rubric_version}``: the
owner labels and approves, so a row counts only once it holds a human label.
Drafts without one belong outside this directory.
"""

from __future__ import annotations

import math
from pathlib import Path

from genesis.eval.calibration import _validate_references
from genesis.eval.j9_batch import _RELEVANCE_PROMPT, _RELEVANCE_PROMPT_VERSION
from genesis.eval.qualification.evidence import Incomplete, digest, load_json
from genesis.eval.rubrics import get_rubric, list_rubrics
from genesis.learning.procedural.embedding import EMBEDDING_DIM

RELEVANCE = "j9_relevance"
NOVELTY = "procedure_novelty"
# Minimum cases per class: 25 + 25 (50 cases) per rubric and relevance;
# novelty needs 300 distinct (False) and 100 redundant (True).
FLOORS = {NOVELTY: {False: 300, True: 100}}
DEFAULT_FLOOR = {False: 25, True: 25}


def versions() -> dict[str, str]:
    return {
        **{r.name: r.version for r in list_rubrics()},
        RELEVANCE: _RELEVANCE_PROMPT_VERSION,
        NOVELTY: "novelty-v1",  # bump when the novelty contract or its labels change
    }


def route(contract: str) -> str:
    return "novelty" if contract == NOVELTY else "relevance" if contract == RELEVANCE else "judge"


def label(contract: str, case: dict) -> bool:
    """The class a case belongs to: redundant for novelty, user_passed otherwise."""
    if contract == NOVELTY:
        return case["expected_target"] is not None
    return case["user_passed"]


def expected(contract: str, case: dict):
    return case["expected_target"] if contract == NOVELTY else case["user_passed"]


def load(directory: Path) -> dict[str, list[dict]]:
    if not directory.is_dir():
        raise Incomplete("corpus directory not found")
    known = versions()
    stray = [
        p.name for p in directory.iterdir() if p.suffix != ".jsonl" and not p.name.startswith(".")
    ]
    if stray:
        raise Incomplete(f"corpus directory holds non-JSONL files: {sorted(stray)}")
    corpus = {}
    for path in sorted(directory.glob("*.jsonl")):
        if path.stem not in known:
            raise Incomplete(f"unknown contract file {path.name}")
        cases = []
        for number, raw in enumerate(path.read_text().splitlines(), 1):
            if not raw.strip() or raw.lstrip().startswith("#"):
                continue
            try:
                case = load_json(raw)
            except ValueError as exc:
                raise Incomplete(f"{path.name}:{number}: invalid JSON") from exc
            if not isinstance(case, dict):
                raise Incomplete(f"{path.name}:{number}: case must be a JSON object")
            cases.append(case)
        validate(path.stem, cases)
        corpus[path.stem] = cases
    if not corpus:
        raise Incomplete("corpus directory holds no contract files")
    return corpus


def validate(contract: str, cases: list[dict]):
    if not cases:
        raise Incomplete(f"{contract}: no cases")
    if contract not in (RELEVANCE, NOVELTY):
        try:
            _validate_references(cases, get_rubric(contract))
        except (ValueError, KeyError, TypeError) as exc:
            raise Incomplete(f"{contract}: {exc}") from exc
        return
    seen, questions = set(), set()
    for case in cases:
        if not isinstance(case.get("id"), str) or not case["id"].strip() or case["id"] in seen:
            raise Incomplete(f"{contract}: missing or duplicate case id")
        seen.add(case["id"])
        provenance = case.get("reference_provenance")
        if (
            not isinstance(provenance, dict)
            or provenance.get("label_source") != "human"
            or not isinstance(provenance.get("reviewer"), str)
            or not provenance["reviewer"].strip()
            or provenance.get("rubric_version") != versions()[contract]
        ):
            raise Incomplete(f"{contract}: case {case['id']!r} lacks an owner human label")
        if contract == RELEVANCE:
            if type(case.get("user_passed")) is not bool:
                raise Incomplete(f"{contract}: user_passed must be a boolean")
            for key in ("query", "memory_content"):
                if not isinstance(case.get(key), str) or not case[key].strip():
                    raise Incomplete(f"{contract}: missing {key}")
            question = _RELEVANCE_PROMPT.format(
                query=case["query"][:500], memory_content=case["memory_content"][:1000]
            )
        else:
            validate_novelty(case)
            question = digest([case["new"], case["existing"]])
        if question in questions:
            raise Incomplete(f"{contract}: duplicate grading question")
        questions.add(question)


def validate_novelty(case: dict):
    if "expected_target" not in case:
        raise Incomplete("novelty requires an explicit target or null reference")
    candidate, existing = case.get("new"), case.get("existing")
    if not isinstance(candidate, dict) or not isinstance(existing, list) or not existing:
        raise Incomplete("novelty requires new and existing procedures")
    names, ids, principles = set(), set(), set()
    for row in [candidate, *existing]:
        if not isinstance(row, dict):
            raise Incomplete("procedure must be a JSON object")
        for key in ("task_type", "principle"):
            value = row.get(key)
            if not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value:
                raise Incomplete("invalid procedure text")
        # Task types map rendered candidates back to ids; principles map embeddings.
        if row["task_type"] in names or row["principle"] in principles:
            raise Incomplete("task types and principles must be unique within a case")
        names.add(row["task_type"])
        principles.add(row["principle"])
        steps = row.get("steps")
        if (
            not isinstance(steps, list)
            or not steps
            or any(not isinstance(s, str) or not s.strip() for s in steps)
        ):
            raise Incomplete("invalid procedure steps")
        vector = row.get("embedding")
        if (
            not isinstance(vector, list)
            or not 1 <= len(vector) <= EMBEDDING_DIM
            or any(not float32(v) for v in vector)
            or not any(vector)
        ):
            raise Incomplete("invalid deterministic embedding")
    for row in existing:
        if not isinstance(row.get("id"), str) or not row["id"] or row["id"] in ids:
            raise Incomplete("invalid procedure id")
        ids.add(row["id"])
    target = case["expected_target"]
    if target is not None and (not isinstance(target, str) or target not in ids):
        raise Incomplete("reference target absent from candidate population")


def float32(value) -> bool:
    # SQLite stores float32 embeddings; finite float64 alone is insufficient.
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and abs(value) <= 3.4028234663852886e38
    except OverflowError:
        return False


def counts(corpus: dict[str, list[dict]]) -> dict:
    return {
        name: {
            "cases": len(cases),
            "true": sum(label(name, c) for c in cases),
            "false": sum(not label(name, c) for c in cases),
        }
        for name, cases in corpus.items()
    }


def coverage(corpus: dict[str, list[dict]], *, complete=False) -> list[str]:
    """Floor issues for every contract present; with ``complete``, absent ones too."""
    issues = [f"{name}: no corpus file" for name in versions() if complete and name not in corpus]
    for name, cases in corpus.items():
        floor = FLOORS.get(name, DEFAULT_FLOOR)
        classes = {cls: sum(label(name, c) is cls for c in cases) for cls in (False, True)}
        if any(classes[cls] < floor[cls] for cls in floor):
            issues.append(
                f"{name}: needs at least {floor[False]} false and {floor[True]} true cases"
                f" (has {classes[False]} and {classes[True]})"
            )
    return issues
