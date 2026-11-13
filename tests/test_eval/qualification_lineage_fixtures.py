"""Synthetic lineage instruments; these do not establish complete admission."""

import copy
import hashlib
import importlib
import json
import socket
from types import SimpleNamespace

import httpx
import pytest

from genesis.eval.qualification import pinned
from genesis.eval.qualification.accounting import Journal
from genesis.eval.qualification.evidence import digest
from tests.test_eval.qualification_fixtures import small_corpus
from tests.test_eval.qualification_transport_fixtures import Gateway, delegate_campaign


@pytest.fixture(name="data")
def primitive_data(tmp_path, monkeypatch):
    """One transport coordinate and declared synthetic metadata, without admission."""
    monkeypatch.setenv(pinned.KEY_ENV, "synthetic-only-key")
    for name in pinned.PRODUCTION_KEY_NAMES:
        monkeypatch.delenv(name, raising=False)
    empty = tmp_path / "empty-input.env"
    empty.write_text("")
    monkeypatch.setenv("SECRETS_PATH", str(empty))
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("physical network forbidden"))
    journal, alias, params, contract, messages = delegate_campaign(tmp_path)
    manifest = journal.expected_manifest
    spec = manifest["attempts"][0]
    binding = manifest["binding"]["transports"].pop(alias)
    alias = "openrouter-deepseek-flash"
    binding["model"] = pinned.resolve(alias).model_id
    manifest["binding"]["transports"][alias] = binding
    spec.update(alias=alias, model=binding["model"], request_hash=digest(
        pinned.request_body(binding["model"], params, contract, messages)))
    for field in ("receipt_provider", "provider_identity_evidence"):
        binding[field] = spec[field]
    case = copy.deepcopy(small_corpus(novelty=False)[contract][0])
    case["id"] = spec["case_id"]
    plan = {contract: {case["id"]: {"messages": messages, "prompt_hash": digest(messages)}}}
    manifest["binding"]["admission"] = {
        "corpus_hash": "a" * 64, "policy_hash": "b" * 64, "approval_hash": "c" * 64,
        "versions": {contract: spec["version"]}, "rubrics": [],
        "reference_sources": {}, "production_sources": {}, "plan": plan,
        "runner_sources": {"run.py": "d" * 64},
    }
    return SimpleNamespace(
        manifest=manifest, path=journal.directory, alias=alias, params=params,
        cases={contract: [case]}, checked={"plan": plan}, temp=tmp_path,
    )


def api():
    assert importlib.util.find_spec("genesis.eval.qualification.lineage") is not None, (
        "bounded lineage reader is not implemented"
    )
    return importlib.import_module("genesis.eval.qualification.lineage")


def reference(data):
    return {
        "directory": str(data.path),
        "journal_sha256": hashlib.sha256((data.path / "answers.jsonl").read_bytes()).hexdigest(),
        "manifest_hash": digest(data.manifest),
        "closure": None,
    }


async def retain_one(data, *, answer='{"score": 0}', generation_prefix=""):
    """Capture one actual delegate/mock-HTTP response in a synthetic campaign."""
    data.manifest = copy.deepcopy(data.manifest)
    binding = data.manifest["binding"]["transports"][data.alias]
    binding["credential_mode"] = "shared_existing"
    for spec in data.manifest["attempts"]:
        spec["credential_mode"] = "shared_existing"
    gateway = Gateway(answer=answer)

    def respond(request):
        response = gateway(request)
        if generation_prefix and request.method == "POST":
            body = response.json()
            previous = body["id"]
            body["id"] = generation_prefix + previous
            gateway.generations[body["id"]] = {**gateway.generations.pop(previous), "id": body["id"]}
            return httpx.Response(200, json=body)
        return response

    async def synthetic_admission(_router, _attempt, info):
        assert info == {"credential_mode": "shared_existing"}

    spec = data.manifest["attempts"][0]
    messages = data.checked["plan"][spec["contract"]][spec["case_id"]]["messages"]
    with Journal(data.path, data.manifest) as journal:
        router = pinned.PinnedRouter(
            journal, alias=data.alias, params=data.params, dispatch=True,
            transport=httpx.MockTransport(respond), credential_mode="shared_existing",
            dispatch_admission=synthetic_admission,
        )
        router.bind(spec["contract"], spec["repetition"], case_id=spec["case_id"])
        await router.route_call("judge", messages)
    assert len(gateway.requests) == gateway.receipt_reads == 1
    assert gateway.key_reads == 0
    return reference(data), spec, gateway


def reject_one(data, *, alter=None, additional_failure=None):
    """Publish a synthetic complete rejection, not an SDK or billing proof."""
    spec = data.manifest["attempts"][0]
    body = {
        "error": {"code": 429, "message": "Synthetic rejection", "metadata": {
            "provider_name": spec["receipt_provider"], "is_byok": False,
            "provider_error_code": "engine_overloaded",
            "limit_source": "upstream_provider_shared_pool",
        }},
        "openrouter_metadata": {
            "requested": spec["model"], "strategy": "direct", "attempt": 1,
            "is_byok": False, "endpoints": {"total": 1, "available": [{
                "provider": spec["receipt_provider"],
                "model": spec.get("backend_model", spec["model"]), "selected": False,
            }]},
        },
    }
    observation = {
        "http_status": 429, "generation_id": "synthetic-rejection",
        "header_generation_id": "synthetic-rejection", "body_generation_id": None,
        "model": None, "upstream": None, "response_model": None, "response_provider": None,
        "content": None, "usage": {}, "error": "invalid financial evidence: 'usage'",
    }
    if alter:
        alter(body, observation)
    observation["raw_body"] = json.dumps(body)
    with Journal(data.path, data.manifest) as journal:
        journal.reserve(spec["id"])
        journal.dispatch(spec["id"])
        journal.observe(spec["id"], observation)
        journal.fail(spec["id"], "invalid financial evidence: 'usage'")
        if additional_failure:
            journal.fail(spec["id"], additional_failure)
    return reference(data), spec
