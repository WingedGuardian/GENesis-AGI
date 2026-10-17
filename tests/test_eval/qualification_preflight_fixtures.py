"""Offline-only explicit parameters for actual qualification call sites."""

import hashlib

from genesis.eval.qualification import corpus, references
from genesis.eval.qualification.evidence import digest
from tests.test_eval.qualification_fixtures import novelty_case, small_corpus


def params():
    return {
        "upstream": "Synthetic", "max_price": {"prompt": 1, "completion": 2},
        "judge": {"temperature": 0.0, "max_tokens": 400},
        "relevance": {"temperature": 0.0, "max_tokens": 150},
        "novelty": {"max_tokens": 150},
    }


def admitted(tmp_path):
    """750 synthetic labels/actors; never substitutes for real owner approval."""
    source = tmp_path / "synthetic-source.txt"
    source.write_text("Synthetic supporting evidence only")
    sources = {str(source): hashlib.sha256(source.read_bytes()).hexdigest()}
    cases = small_corpus(25, novelty=False)
    cases[corpus.NOVELTY] = [novelty_case(f"d{i}", None) for i in range(300)] + [
        novelty_case(f"r{i}", "candidate-b") for i in range(100)
    ]
    policy = {
        "version": references.VERSION, "confidence_threshold": 90,
        "approved_models": ["synthetic-frontier"], "guidance": "Synthetic guidance",
        "feedback": [],
    }
    for name, rows in cases.items():
        for case in rows:
            case["source"] = {"snapshot": str(source), "snapshot_sha256": sources[str(source)]}
            case["reference_review"] = {
                "label_source": "human", "reviewer": "synthetic-feedback-human",
                "evidence_complete": True, "uncertainties": [],
                "policy_hash": digest(policy), "case_hash": references.case_hash(name, case),
                "feedback_applicability": {},
            }
    approval = {
        "label_source": "human", "reviewer": "synthetic-final-human",
        "approved": True, "independent": True, "evidence": "Synthetic sign-off only",
        "policy_hash": digest(policy), "corpus_hash": digest(cases),
    }
    return cases, {
        "reference_policy": policy, "current_policy": policy,
        "reference_approval": approval, "source_files": sources,
        "candidates": {"openrouter-mimo": params(), "openrouter-deepseek-flash": params()},
    }
