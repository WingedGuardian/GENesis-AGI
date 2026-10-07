"""Subset runner instruments; no complete, historical or financial admission claim."""

import hashlib
import json
import socket
from types import SimpleNamespace

import pytest

from genesis.eval.qualification import corpus, pinned, preflight, references, run
from genesis.eval.qualification.evidence import digest
from tests.test_eval.qualification_fixtures import small_corpus, write_corpus
from tests.test_eval.qualification_preflight_fixtures import params
from tests.test_eval.qualification_transport_fixtures import Gateway


@pytest.fixture(name="data")
async def runner_data(tmp_path, monkeypatch):
    """Subset instrument for runner primitives; NOT complete or financial admission."""
    monkeypatch.setenv(pinned.KEY_ENV, "synthetic-only-key")
    secrets = tmp_path / "secrets.env"
    secrets.write_text("")
    monkeypatch.setenv("SECRETS_PATH", str(secrets))
    for name in pinned.PRODUCTION_KEY_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("physical network forbidden"))
    name, cases = next(iter(small_corpus(novelty=False).items()))
    return await instrument(tmp_path, {name: cases})


async def instrument(tmp_path, cases, *, policy=None, alias="openrouter-mimo"):
    """Freeze actual subset rendering to exercise scheduling/scoring, not admission."""
    configured = params()
    source = tmp_path / "synthetic-source.txt"
    source.write_text("Synthetic subset instrument only")
    sources = {str(source): preflight.file_hash(source)}
    for name, rows in cases.items():
        for case in rows:
            case["source"] = {"path": str(source), "sha256": sources[str(source)]}
            # This helper creates only synthetic subset inputs. Bind their
            # existing synthetic actor receipt to the new synthetic source.
            if "reference_review" in case:
                case["reference_review"]["case_hash"] = references.case_hash(name, case)
    plan = await preflight.prepare(cases, tmp_path, reference_policy=policy)
    checked = {
        "corpus_hash": digest(cases), "policy_hash": "a" * 64, "approval_hash": "b" * 64,
        "versions": corpus.versions(), "rubrics": [], "reference_sources": sources,
        "production_sources": {}, "plan": plan,
        "models": {alias: pinned.resolve(alias).model_id}, "parameters": {alias: configured},
    }
    identity = {
        "model": checked["models"][alias], "params": configured, "endpoint": pinned.ENDPOINT,
        "generation_namespace": "openrouter",
        "credential_fingerprint": hashlib.sha256(b"synthetic-only-key").hexdigest(),
        "receipt_provider": "Synthetic Gateway Display",
        "provider_identity_evidence": "synthetic mapping only",
    }
    attempts = []
    for name, rows in plan.items():
        for case_id, row in rows.items():
            for rep in run.REPETITIONS:
                attempts.append({
                "id": f"attempt-{len(attempts)}", "alias": alias, "model": identity["model"],
                "upstream": configured["upstream"], "endpoint": pinned.ENDPOINT + "/chat/completions",
                "request_hash": digest(pinned.request_body(identity["model"], configured, name, row["messages"])),
                "max_charge": "0.1", "generation_namespace": "openrouter",
                "contract": name, "version": corpus.versions()[name], "case_id": case_id,
                "repetition": rep, "prompt_hash": row["prompt_hash"],
                **{key: identity[key] for key in (
                    "credential_fingerprint", "receipt_provider", "provider_identity_evidence"
                )},
                })
    # Exactly one tenth per row, encoded without a float or ambient Decimal context.
    manifest = {"version": 2, "budget": f"{len(attempts) // 10}.{len(attempts) % 10}",
                "binding": {"admission": run.admission_identity(checked), "transports": {alias: identity}},
                "attempts": attempts}
    return SimpleNamespace(cases=cases, checked=checked, params=configured, alias=alias,
                           manifest=manifest, path=tmp_path / "campaign", temp=tmp_path,
                           policy=policy, source_files=sources, gateway=Gateway())


def make_router(data, campaign, dispatch):
    return pinned.PinnedRouter(campaign, alias=data.alias, params=data.params,
                               dispatch=dispatch, transport=data.gateway.transport)


class LabelGateway(Gateway):
    """Actual wire transport with deterministic reference answers; no quality claim."""

    def __init__(self, data):
        super().__init__()
        self.limit = self.remaining = len(data.manifest["attempts"]) + 1
        self.answers = {}
        for name, cases in data.cases.items():
            for case in cases:
                row = data.checked["plan"][name][case["id"]]
                if name == corpus.NOVELTY:
                    target = case["expected_target"]
                    verdict = {"redundant_with": row["candidate_mapping"].index(target) + 1 if target else None}
                else:
                    verdict = {"relevance" if name == corpus.RELEVANCE else "score": int(corpus.label(name, case))}
                self.answers[row["prompt_hash"]] = json.dumps(verdict)

    def __call__(self, request):
        if request.method == "POST":
            self.answer = self.answers[digest(json.loads(request.content)["messages"])]
        return super().__call__(request)


def report_cli(data, monkeypatch, *extra):
    """CLI plumbing instrument. Complete admission is exercised separately, not mocked proof."""
    from genesis.eval.qualification.__main__ import main

    async def current(cases, _temp, *, candidates, **_options):
        # Re-render so this fixture cannot accidentally bless changed prompts.
        plan = await preflight.prepare(cases, data.temp, reference_policy=data.policy)
        for alias, configured in candidates.items():
            preflight.parameters(alias, configured, plan)
        return {**data.checked, "corpus_hash": digest(cases), "plan": plan,
                "models": {a: pinned.resolve(a).model_id for a in candidates}, "parameters": candidates}

    monkeypatch.setattr(preflight, "check", current)
    # This subset fixture measures CLI plumbing, never complete admission.
    monkeypatch.setattr(corpus, "coverage", lambda *_args, **_kwargs: [])
    inputs = {"params": {a: data.params for a in ("openrouter-mimo", "openrouter-deepseek-flash")}, "reference-policy": data.policy,
              "current-reference-policy": data.policy, "reference-approval": None,
              "source-files": data.source_files}
    arguments = []
    for name, value in inputs.items():
        path = data.temp / (name + ".json")
        path.write_text(json.dumps(value))
        arguments += ["--" + name, str(path)]
    directory = write_corpus(data.temp / "corpus", data.cases)
    return main(["report", "--alias", data.alias, "--campaign", str(data.path),
                 "--corpus", str(directory), "--temp-root", str(data.temp), *arguments, *extra])
