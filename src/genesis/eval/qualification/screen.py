"""The fixed paired judge screen, with no acquisition authority."""

from dataclasses import dataclass
from hashlib import sha256

from . import corpus
from .evidence import Incomplete, digest

SEED = "genesis-judge-screen-20261121-v1"
CASES_PER_CLASS = 5
CONTRACTS = (corpus.RELEVANCE, "reflection_quality", "skill_edit_regression")


@dataclass(frozen=True)
class Coordinate:
    contract: str
    case_id: str
    label: bool
    prompt_hash: str


def select(cases, checked):
    """Reproduce the frozen selection from complete, freshly admitted inputs."""
    if checked.get("complete") is not True or checked.get("corpus_hash") != digest(cases):
        raise Incomplete("judge screen requires complete admission")
    cohort = []
    for name in CONTRACTS:
        for label in (False, True):
            prefix = f"{SEED}|{name}|{str(label).lower()}|".encode()
            matching = [case["id"] for case in cases[name] if corpus.label(name, case) is label]
            if len(matching) < CASES_PER_CLASS:
                raise Incomplete("judge screen stratum has too few admitted cases")
            ordered = sorted(matching, key=lambda case_id, prefix=prefix: sha256(prefix + case_id.encode()).digest())
            cohort.extend(Coordinate(name, case_id, label, checked["plan"][name][case_id]["prompt_hash"])
                          for case_id in ordered[:CASES_PER_CLASS])
    return tuple(cohort)


def sample_digest(cohort):
    return digest([(c.contract, c.case_id, c.label, c.prompt_hash) for c in cohort])


def check(cases, checked, expected):
    cohort = select(cases, checked)
    actual = sample_digest(cohort)
    if actual != expected:
        raise Incomplete("judge screen sample changed")
    return {"status": "pass", "scope": "offline judge screen admission",
            "selected_cases": len(cohort), "sample_sha256": actual,
            "corpus_sha256": checked["corpus_hash"]}
