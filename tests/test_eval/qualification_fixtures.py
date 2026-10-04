"""Synthetic qualification corpus and a mocked OpenRouter: no credentials, no egress."""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx

from genesis.eval.qualification import corpus, pinned
from genesis.eval.rubrics import list_rubrics

KEY = "synthetic-dedicated-qualification-key"
MODELS = {
    "openrouter-mimo": "xiaomi/mimo-v2.6-pro",
    "openrouter-deepseek-flash": "deepseek/deepseek-v4.1-flash",
}


def provenance(contract):
    return {
        "label_source": "human",
        "reviewer": "synthetic-owner",
        "rubric_version": corpus.versions()[contract],
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
        "reference_provenance": provenance(corpus.RELEVANCE),
    }


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


def params(upstream="Synthetic"):
    return {
        "upstream": upstream,
        "max_price": {"prompt": 1, "completion": 2},
        "judge": {"max_tokens": 400, "temperature": 0.0},
        "relevance": {"max_tokens": 150, "temperature": 0.0},
        "novelty": {"max_tokens": 300},
    }


def correct_answer(prompt: str) -> str:
    """The right verdict for a synthetic case, read from its rendered prompt."""
    if "task_type:" in prompt:
        redundant = re.search(r"new-task-r", prompt) is not None
        return json.dumps({"redundant_with": 1 if redundant else None})
    passed = re.search(r"(Synthetic query|Synthetic observation) True", prompt) is not None
    if "judging whether a recalled memory is relevant" in prompt:
        return json.dumps({"relevance": 1 if passed else 0})
    return json.dumps({"score": 1 if passed else 0, "rationale": "synthetic"})


class OpenRouter:
    """MockTransport for ``/key`` and ``/chat/completions``; records every request."""

    def __init__(self, answer=correct_answer, *, limit=5, status=200, provider="Synthetic"):
        self.answer, self.limit, self.status, self.provider = answer, limit, status, provider
        self.requests = []
        self.key_reads = 0

    def __call__(self, request: httpx.Request):
        if request.url.path.endswith("/key"):
            self.key_reads += 1
            return httpx.Response(
                200, json={"data": {"limit": self.limit, "limit_remaining": self.limit, "usage": 0}}
            )
        body = json.loads(request.content)
        self.requests.append({"body": body, "headers": dict(request.headers)})
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": "synthetic"}})
        return httpx.Response(
            200,
            json={
                "id": f"gen-{len(self.requests)}",
                "model": body["model"],
                "provider": self.provider,
                "object": "chat.completion",
                "created": 1,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": self.answer(body["messages"][0]["content"]),
                        },
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 5, "cost": 0.0001},
            },
        )

    @property
    def transport(self):
        return httpx.MockTransport(self)


def isolate_credentials(monkeypatch, tmp_path, key=KEY):
    """Only the dedicated key is visible; secrets.env points at an empty file."""
    for name in pinned.PRODUCTION_KEY_NAMES:
        monkeypatch.delenv(name, raising=False)
    secrets = tmp_path / "secrets.env"
    secrets.write_text("")
    monkeypatch.setenv("SECRETS_PATH", str(secrets))
    if key is None:
        monkeypatch.delenv(pinned.KEY_ENV, raising=False)
    else:
        monkeypatch.setenv(pinned.KEY_ENV, key)
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
