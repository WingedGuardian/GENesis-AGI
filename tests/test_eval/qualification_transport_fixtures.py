"""Synthetic frozen transport manifests and a GET/POST-only mock gateway."""

import hashlib
import json
from types import SimpleNamespace

import httpx

from genesis.eval.qualification import corpus, pinned
from genesis.eval.qualification.accounting import Journal
from genesis.eval.qualification.evidence import digest
from genesis.eval.qualification.pinned import ENDPOINT


def routing_metadata(model, provider):
    return {
        "requested": model, "strategy": "direct", "attempt": 1, "is_byok": False,
        "endpoints": {"total": 1, "available": [
            {"model": model, "provider": provider, "selected": True}
        ]},
    }


def delegate_campaign(tmp_path):
    alias = "openrouter-mimo"
    model = pinned.resolve(alias).model_id
    contract = next(
        name for name in corpus.versions() if name not in (corpus.RELEVANCE, corpus.NOVELTY)
    )
    params = {
        "upstream": "Synthetic",
        "max_price": {"prompt": 1, "completion": 2},
        **{name: {"temperature": 0.0, "max_tokens": 400} for name in pinned.ROUTES},
    }
    messages = [{"role": "user", "content": "Synthetic question"}]
    request = {
        "model": model,
        "messages": messages,
        **params["judge"],
        "provider": {
            "only": ["Synthetic"],
            "allow_fallbacks": False,
            "require_parameters": True,
            "max_price": params["max_price"],
        },
        "usage": {"include": True},
    }
    manifest = {
        "version": 2,
        "budget": "0.1",
        "binding": {
            "transports": {
                alias: {
                    "model": model,
                    "params": params,
                    "endpoint": ENDPOINT,
                    "generation_namespace": "openrouter",
                    "credential_fingerprint": hashlib.sha256(b"synthetic-only-key").hexdigest(),
                }
            }
        },
        "attempts": [
            {
                "id": "case-0",
                "alias": alias,
                "model": model,
                "upstream": "Synthetic",
                "endpoint": ENDPOINT + "/chat/completions",
                "request_hash": digest(request),
                "max_charge": "0.1",
                "generation_namespace": "openrouter",
                "contract": contract,
                "version": corpus.versions()[contract],
                "case_id": "case-0",
                "repetition": 1,
                "prompt_hash": digest(messages),
                "receipt_provider": "Synthetic Gateway Display",
                "provider_identity_evidence": "synthetic frozen display mapping",
                "credential_fingerprint": hashlib.sha256(b"synthetic-only-key").hexdigest(),
            }
        ],
    }
    return Journal(tmp_path / "delegate-campaign", manifest), alias, params, contract, messages


def bundle(tmp_path, *, configured=None):
    campaign, alias, params, contract, messages = delegate_campaign(tmp_path)
    manifest = campaign.expected_manifest
    if configured is not None:
        params = configured
        manifest["binding"]["transports"][alias]["params"] = params
        body = {
            "model": manifest["attempts"][0]["model"],
            "messages": messages,
            **params["judge"],
            "provider": {
                "only": [params["upstream"]],
                "allow_fallbacks": False,
                "require_parameters": True,
                "max_price": params["max_price"],
            },
            "usage": {"include": True},
        }
        manifest["attempts"][0]["request_hash"] = digest(body)
    manifest["budget"] = "0.3"
    first = manifest["attempts"][0]
    manifest["attempts"] = [{**first, "id": f"case-{i}", "case_id": f"case-{i}"} for i in (1, 2, 3)]
    return SimpleNamespace(
        campaign=campaign, alias=alias, params=params, contract=contract, messages=messages
    )


class Gateway:
    def __init__(self, *, status=200, provider="Synthetic Gateway Display", answer='{"score": 1}'):
        self.status, self.provider, self.answer = status, provider, answer
        self.limit, self.remaining, self.reset = 1, 1, None
        self.requests, self.key_reads, self.receipt_reads = [], 0, 0
        self.generations = {}

    @property
    def transport(self):
        return httpx.MockTransport(self)

    def __call__(self, request):
        if request.url.path == "/api/v1/key":
            self.key_reads += 1
            return httpx.Response(
                200,
                json={
                    "data": {
                        "limit": self.limit,
                        "limit_remaining": self.remaining,
                        "limit_reset": self.reset,
                        "usage": 0,
                    }
                },
            )
        if request.url.path == "/api/v1/generation":
            self.receipt_reads += 1
            generation = request.url.params["id"]
            return httpx.Response(200, json={"data": self.generations[generation]})
        assert request.method == "POST" and request.url.path == "/api/v1/chat/completions"
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(
                self.status, json={"error": {"message": "synthetic failure", "code": self.status}}
            )
        model = json.loads(request.content)["model"]
        generation = f"gen-{len(self.requests)}"
        self.generations[generation] = {
            "id": generation,
            "model": model,
            "provider_name": self.provider,
            "total_cost": 0.0001,
        }
        return httpx.Response(
            200,
            json={
                "id": generation,
                "object": "chat.completion",
                "created": 1,
                "model": model,
                "openrouter_metadata": routing_metadata(model, self.provider),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": self.answer},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 5,
                    "total_tokens": 25,
                    "cost": 0.0001,
                },
            },
        )


def router(data, gateway, **kwargs):
    result = pinned.PinnedRouter(
        data.campaign,
        alias=data.alias,
        params=data.params,
        dispatch=kwargs.pop("dispatch", True),
        transport=kwargs.pop("transport", gateway.transport),
        **kwargs,
    )
    result.bind(data.contract, 1, case_id="case-1")
    return result


async def ask(result, messages, case_id="case-1", **kwargs):
    result.bind(result.contract, 1, case_id=case_id)
    return await result.route_call("judge", messages, temperature=0.0, **kwargs)
