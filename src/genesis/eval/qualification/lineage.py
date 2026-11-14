"""Read immutable physical evidence without changing its accounting or answers."""

from __future__ import annotations

import copy
import os
from pathlib import Path

from . import accounting, execution, pinned
from .evidence import Campaign, Incomplete, canonical, digest


def is_hash(value, length=64):
    return (isinstance(value, str) and len(value) == length
            and all(char in "0123456789abcdef" for char in value))


class Frozen(execution.Input):
    """Retain a private byte input; reuse Input's boundary and final checks."""

    def __init__(self, path, expected):
        if not is_hash(expected):
            raise Incomplete("invalid frozen evidence hash")
        super().__init__(path, decode=bytes)
        try:
            self.raw = self.value
            if self.hash != expected:
                raise Incomplete("frozen evidence changed")
            self.check()
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def close(self):
        descriptor, self.fd = self.fd, None
        if descriptor is not None:
            os.close(descriptor)


class History(Frozen):
    """A hash-bound snapshot, with the original full row and incident history."""

    def __init__(self, reference, *, owners=None):
        if (not isinstance(reference, dict)
                or set(reference) != {"directory", "journal_sha256", "manifest_hash", "closure"}
                or not isinstance(reference["directory"], str) or not reference["directory"].strip()
                or not is_hash(reference["manifest_hash"])):
            raise Incomplete("invalid history reference")
        self.directory = Path(reference["directory"]).absolute()
        self.owner, self.closure_safe = None, True
        super().__init__(self.directory / "answers.jsonl", reference["journal_sha256"])
        try:
            snapshot = Campaign.read(self.directory)
            if snapshot.torn_tail:
                raise Incomplete("history has an uncertain tail")
            self._lines = snapshot.lines
            self._state = accounting.reconstruct(self._lines)
            if (not isinstance(self._state.manifest, dict)
                    or self._state.manifest.get("version") != 2
                    or digest(self._state.manifest) != reference["manifest_hash"]):
                raise Incomplete("history manifest changed")
            self.reference = copy.deepcopy(reference)
            self.semantic_manifest = self._state.manifest
            if "study" in self._state.manifest["binding"] or reference["closure"] is not None:
                if owners is None:
                    raise Incomplete("study_child_requires_controller_reader")
                owners.resolve(self, reference["closure"])
            Frozen.check(self, final=True)
        except BaseException:
            self.close()
            raise

    def check(self, *, final=False, owner=True):
        super().check(final=final)
        if owner and self.owner is not None:
            self.owner.check(final=final)

    @property
    def state(self):
        return copy.deepcopy(self._state)

    @property
    def lines(self):
        return copy.deepcopy(self._lines)

def compatible(prior, current):
    """Compare admitted corpus/sample identity; transport matching stays separate."""
    left, right = prior["binding"], current["binding"]
    for field in ("baseline_sample",):
        if ((field in left) != (field in right)
                or canonical(left.get(field)) != canonical(right.get(field))):
            return False
    def remove_runner(value):
        return {k: v for k, v in value.items() if k != "runner_sources"}

    return canonical(remove_runner(left["admission"])) == canonical(remove_runner(right["admission"]))


def _generation(row):
    values = []
    for item in row["observations"] + row["receipts"] + row["lookups"]:
        for field in ("generation_id", "body_generation_id", "header_generation_id",
                      "response_generation_id"):
            value = item.get(field)
            if value is not None:
                if not isinstance(value, str) or not value.strip():
                    return None
                values.append(value)
    return values[0] if values and len(set(values)) == 1 else None


def _receipt(receipt, spec, generation):
    if receipt.get("http_status") != 200 or receipt.get("error"):
        raise Incomplete("unverified generation receipt")
    envelope = pinned.financial_json(receipt["raw_body"].encode(), (("data", "total_cost"),))
    data = envelope["data"]
    if ("error" in envelope or data.get("id") != generation
            or data.get("model") != pinned.model_identities(spec)[2]
            or data.get("provider_name") != spec["receipt_provider"]
            or receipt.get("response_generation_id") != generation
            or receipt.get("response_model") != data["model"]
            or receipt.get("response_provider") != data["provider_name"]
            or accounting.currency(data["total_cost"]) != accounting.currency(receipt["charge"])):
        raise Incomplete("raw generation receipt contradicts its identity or charge")


def eligible(history, identity):
    """Return a verified physical row regardless of whether its judgment agrees."""
    history.check()
    if not history.closure_safe:
        return None
    return eligible_row(history._state, identity, history.semantic_manifest)


def eligible_row(state, identity, semantic_manifest):
    """Verify a real physical state against its owner's full logical admission."""
    row = state.attempts[identity]
    incidents = {f"{name}:{identity}" for name in ("billing", "reservation", "answer", "failure", "identity")}
    if identity not in state.answers or state.blockers & incidents or len(row["observations"]) != 1:
        return None
    try:
        spec, observed, generation = row["spec"], row["observations"][0], _generation(row)
        if generation is None:
            return None
        binding = semantic_manifest["binding"]["transports"][spec["alias"]]
        prepared = semantic_manifest["binding"]["admission"]["plan"][spec["contract"]][spec["case_id"]]
        if (pinned.model_identities(binding) != pinned.model_identities(spec)
                or pinned.frozen_credential_mode(binding) != pinned.frozen_credential_mode(spec)
                or pinned.frozen_receipt_policy(binding) != pinned.frozen_receipt_policy(spec)
                or digest(pinned.request_body(spec["model"], binding["params"], spec["contract"],
                                              prepared["messages"])) != spec["request_hash"]):
            return None
        pinned.verify_routing(observed, spec)
        if (observed.get("http_status") != 200 or observed.get("error")
                or not isinstance(observed.get("content"), str)
                or any(observed.get(key) != spec[target] for key, target in (
                    ("model", "model"), ("response_model", "model"),
                    ("upstream", "upstream"), ("response_provider", "receipt_provider")))):
            return None
        raw = pinned.financial_json(observed["raw_body"].encode(), (("usage", "cost"),))
        if (raw.get("id") != generation or raw.get("model") != spec["model"]
                or raw["choices"][0]["message"]["content"] != observed["content"]
                or accounting.currency(raw["usage"]["cost"])
                   != accounting.currency(observed["usage"]["cost"])):
            return None
        for receipt in row["receipts"]:
            _receipt(receipt, spec, generation)
    except (ValueError, KeyError, TypeError, IndexError):
        return None
    return copy.deepcopy(row)


def rejected(history, identity):
    """Recognize only the explicit upstream-pool rejection; never settle its cost."""
    history.check()
    if not history.closure_safe:
        return False
    row = history._state.attempts[identity]
    failures = [event["error"] for event in history._lines
                if event["kind"] == "failure" and event["attempt"] == identity]
    return rejected_row(row, failures)


def rejected_row(row, failures):
    """The same closed-evidence predicate serves inherited and just-closed requests."""
    if not row["dispatched"] or len(row["observations"]) != 1 or _generation(row) is None:
        return False
    spec, observed = row["spec"], row["observations"][0]
    normalization = "invalid financial evidence: 'usage'"
    derivative = {"operating_comparison_stopped:LocalFailure", "operating_comparison_stopped:Incomplete"}
    if (not failures or failures[0] != normalization or any(value not in derivative for value in failures[1:])
            or observed.get("generation_id") != _generation(row)
            or observed.get("http_status") != 429 or observed.get("usage")
            or observed.get("error") != normalization
            or any(observed.get(field) is not None for field in (
                "content", "model", "response_model", "response_provider", "upstream"))
            or any(item.get("http_status") != 404 for item in row["lookups"])):
        return False
    try:
        raw = pinned.external_json(observed["raw_body"])
        error, route = raw["error"], raw["openrouter_metadata"]
        metadata = error["metadata"]
        endpoints = route["endpoints"]["available"]
        headers = observed.get("response_headers", {})
        if (type(error.get("code")) is not int or error["code"] != 429
                or metadata.get("provider_name") != spec["receipt_provider"]
                or metadata.get("provider_error_code") != "engine_overloaded"
                or metadata.get("limit_source") != "upstream_provider_shared_pool"
                or metadata.get("is_byok") is not False or route.get("is_byok") is not False
                or route.get("requested") != spec["model"] or route.get("strategy") != "direct"
                or type(route.get("attempt")) is not int or route["attempt"] != 1
                or raw.get("choices") or raw.get("usage") or route.get("pipeline") or raw.get("pipeline")
                or raw.get("id") not in (None, _generation(row))
                or not isinstance(headers, dict)
                or headers.get("x-generation-id") not in (None, _generation(row))
                or route.get("attempts")
                or not isinstance(endpoints, list) or not endpoints
                or any(item.get("selected") is not False for item in endpoints)
                or not any(item.get("provider") == spec["receipt_provider"]
                           and item.get("model") == pinned.model_identities(spec)[1] for item in endpoints)):
            return False
        for receipt in row["receipts"]:
            _receipt(receipt, spec, _generation(row))
    except (ValueError, KeyError, TypeError, AttributeError):
        return False
    return True
