"""Graded reference corpus: one calibration-format JSONL file per contract.

``<corpus>/<contract>.jsonl``. Rubric files are calibration golden sets, checked
by the same strict validator ``run_calibration`` uses. Every case declares
its label source, reviewer and contract version in ``reference_provenance``.
Human labels use the strict legacy path; frontier-assisted labels require
an explicit reference policy and current review receipts. Ungraded drafts
belong outside this directory.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

from genesis.eval.calibration import _validate_references
from genesis.eval.j9_batch import _RELEVANCE_PROMPT, _RELEVANCE_PROMPT_VERSION
from genesis.eval.qualification import references
from genesis.eval.qualification.evidence import Incomplete, digest, load_json
from genesis.eval.rubrics import get_rubric, list_rubrics
from genesis.eval.scorers import render_rubric_prompt
from genesis.learning.procedural.embedding import EMBEDDING_DIM, pack_embedding, unpack_embedding

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
    return references.passed(case)


def expected(contract: str, case: dict):
    return case["expected_target"] if contract == NOVELTY else references.passed(case)


def load(directory: Path, *, reference_policy=None) -> dict[str, list[dict]]:
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
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise Incomplete(f"{path.name}: invalid UTF-8") from exc
        for number, raw in enumerate(text.split("\n"), 1):
            if not raw.strip() or raw.lstrip().startswith("#"):
                continue
            try:
                case = load_json(raw)
            except ValueError as exc:
                raise Incomplete(f"{path.name}:{number}: invalid JSON") from exc
            if not isinstance(case, dict):
                raise Incomplete(f"{path.name}:{number}: case must be a JSON object")
            cases.append(case)
        validate(path.stem, cases, reference_policy=reference_policy)
        corpus[path.stem] = cases
    if not corpus:
        raise Incomplete("corpus directory holds no contract files")
    return corpus


def validate(contract: str, cases: list[dict], *, reference_policy=None):
    if not cases:
        raise Incomplete(f"{contract}: no cases")
    if contract not in versions():
        raise Incomplete("unknown reference contract")
    if any(not isinstance(c, dict) for c in cases):
        raise Incomplete("reference case must be an object")
    # Every string a case or the policy carries can reach SQLite or a judge request,
    # whichever contract reads it, so check them all once here rather than per field.
    if not references.all_scalar(cases) or not references.all_scalar(reference_policy):
        raise Incomplete(f"{contract}: text holds a lone surrogate (not Unicode scalar values)")
    if reference_policy is not None:
        references.validate_policy(reference_policy, versions())
        references.admit(
            contract, cases, reference_policy, versions()[contract], contracts=versions()
        )
    if contract not in (RELEVANCE, NOVELTY) and reference_policy is None:
        try:
            _validate_references(cases, get_rubric(contract))
            for case in cases:
                references.passed(case)
        except (ValueError, KeyError, TypeError) as exc:
            raise Incomplete(f"{contract}: {exc}") from exc
        return
    seen, questions = set(), set()
    for case in cases:
        if not isinstance(case.get("id"), str) or not case["id"].strip() or case["id"] in seen:
            raise Incomplete(f"{contract}: missing or duplicate case id")
        seen.add(case["id"])
        _validate_provenance(contract, case, reference_policy)
        if contract not in (RELEVANCE, NOVELTY):
            question = _rubric_question(contract, case)
        elif contract == RELEVANCE:
            references.passed(case)
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


def _validate_provenance(contract, case, policy):
    provenance = case.get("reference_provenance")
    sources = ("human", references.FRONTIER) if policy is not None else ("human",)
    if (
        not isinstance(provenance, dict)
        or provenance.get("label_source") not in sources
        or not references.nonblank(provenance.get("reviewer"))
        or provenance.get("rubric_version") != versions()[contract]
    ):
        raise Incomplete(f"{contract}: invalid reference provenance")


def _rubric_question(contract, case):
    rubric = get_rubric(contract)
    config = case.get("scorer_config")
    if (
        not references.nonblank(case.get("actual"))
        or not isinstance(case.get("expected", ""), str)
        or not isinstance(config, dict)
        or config.get("rubric_name") != contract
        or any(not references.nonblank(config.get(k)) for k in rubric.extra_placeholders)
    ):
        raise Incomplete("invalid rubric reference inputs")
    references.passed(case)
    return render_rubric_prompt(rubric, case["actual"], case.get("expected", ""), config)


def validate_novelty(case: dict):
    if "expected_target" not in case:
        raise Incomplete("novelty requires an explicit target or null reference")
    candidate, existing = case.get("new"), case.get("existing")
    if not isinstance(candidate, dict) or not isinstance(existing, list) or not existing:
        raise Incomplete("novelty requires new and existing procedures")
    ids, embeddings = set(), {}
    for row in [candidate, *existing]:
        if not isinstance(row, dict):
            raise Incomplete("procedure must be a JSON object")
        for key in ("task_type", "principle"):
            value = row.get(key)
            if (
                not isinstance(value, str)
                or not value.strip()
                or "\n" in value
                or "\r" in value
                or not references.scalar_text(value)
            ):
                raise Incomplete("invalid procedure text")
        steps = row.get("steps")
        if (
            not isinstance(steps, list)
            or not steps
            or any(
                not isinstance(s, str) or not s.strip() or not references.scalar_text(s)
                for s in steps
            )
            or any(
                re.search(r"(?:^|[\r\n])  \[\d+\] task_type:", s)
                for s in steps
                if isinstance(s, str)
            )
        ):
            raise Incomplete("invalid procedure steps")
        for flag in ("deprecated", "quarantined"):
            if flag in row and type(row[flag]) is not bool:
                raise Incomplete(f"procedure {flag} must be a boolean")
        vector = row.get("embedding")
        if (
            not isinstance(vector, list)
            or not 1 <= len(vector) <= EMBEDDING_DIM
            or any(not float32(v) for v in vector)
            or not any(vector)
        ):
            raise Incomplete("invalid deterministic embedding")
        stored = unpack_embedding(pack_embedding(vector + [0.0] * (EMBEDDING_DIM - len(vector))))
        if not any(stored):
            raise Incomplete("invalid deterministic embedding: float32 zero vector")
        vector_key = tuple(vector) + (0.0,) * (EMBEDDING_DIM - len(vector))
        if row["principle"] in embeddings and embeddings[row["principle"]] != vector_key:
            raise Incomplete("same principle must have a consistent deterministic embedding")
        embeddings[row["principle"]] = vector_key
    for row in existing:
        if (
            not isinstance(row.get("id"), str)
            or not row["id"]
            or not references.scalar_text(row["id"])
            or row["id"] in ids
        ):
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
