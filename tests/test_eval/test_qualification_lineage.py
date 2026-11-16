"""Immutable prior-answer validation, without acquisition or approval claims."""

import copy
import hashlib
import json

import httpx
import pytest

from genesis.eval.qualification import corpus
from genesis.eval.qualification.accounting import Journal
from genesis.eval.qualification.evidence import Incomplete
from tests.test_eval.qualification_lineage_fixtures import (
    api,
    primitive_data,  # noqa: F401
    reference,
    reject_one,
    retain_one,
)


def test_only_runner_implementation_may_differ(data):
    lineage = api()
    current = copy.deepcopy(data.manifest)
    prior = copy.deepcopy(current)
    prior["binding"]["admission"]["runner_sources"]["run.py"] = "c" * 64
    assert lineage.compatible(prior, current)
    assert prior != current


@pytest.mark.parametrize("field", [
    "corpus_hash", "policy_hash", "approval_hash", "versions", "rubrics",
    "reference_sources", "production_sources", "plan",
])
def test_other_admission_changes_cannot_inherit(data, field):
    lineage = api()
    prior = copy.deepcopy(data.manifest)
    prior["binding"]["admission"][field] = {"changed": True}
    assert not lineage.compatible(prior, data.manifest)


@pytest.mark.asyncio
async def test_actual_saved_wrong_grade_is_still_the_retained_answer(data):
    lineage = api()
    first = data.manifest["attempts"][0]
    case = next(row for row in data.cases[first["contract"]] if row["id"] == first["case_id"])
    wrong = int(not corpus.label(first["contract"], case))
    ref, spec, gateway = await retain_one(data, answer=json.dumps({"score": wrong}))
    path = data.path / "answers.jsonl"
    before = path.read_bytes()
    with lineage.History(ref) as history:
        row = lineage.eligible(history, spec["id"])
        assert row["spec"] == spec
        assert json.loads(row["observations"][0]["content"]) == {"score": wrong}
        assert not history.state.blockers
        assert not lineage.rejected(history, spec["id"])
    assert path.read_bytes() == before
    assert len(gateway.requests) == 1


@pytest.mark.asyncio
async def test_history_hash_or_manifest_mismatch_refuses_before_replay(data):
    lineage = api()
    ref, _spec, gateway = await retain_one(data)
    for field in ("journal_sha256", "manifest_hash"):
        changed = {**ref, field: "f" * 64}
        with pytest.raises(Incomplete):
            lineage.History(changed)
    assert len(gateway.requests) == 1


@pytest.mark.asyncio
async def test_later_incident_invalidates_a_saved_answer(data):
    lineage = api()
    ref, spec, _gateway = await retain_one(data)
    with Journal(data.path, data.manifest) as journal:
        journal.fail(spec["id"], "synthetic callback failure after billing")
    ref["journal_sha256"] = hashlib.sha256((data.path / "answers.jsonl").read_bytes()).hexdigest()
    with lineage.History(ref) as history:
        assert lineage.eligible(history, spec["id"]) is None
        assert history.state.reserved == 0
        assert history.state.blockers


@pytest.mark.asyncio
async def test_changed_or_torn_history_is_never_silently_repaired(data):
    lineage = api()
    ref, _spec, _gateway = await retain_one(data)
    path = data.path / "answers.jsonl"
    with lineage.History(ref) as history:
        with path.open("ab") as stream:
            stream.write(b'{"kind":"failure"')
        changed = path.read_bytes()
        with pytest.raises(Incomplete):
            history.check()
    ref["journal_sha256"] = hashlib.sha256(changed).hexdigest()
    with pytest.raises(Incomplete):
        lineage.History(ref)
    assert path.read_bytes() == changed


def test_precise_rejection_keeps_its_unresolved_charge(data):
    lineage = api()
    ref, spec = reject_one(data)
    before = (data.path / "answers.jsonl").read_bytes()
    with lineage.History(ref) as history:
        assert lineage.rejected(history, spec["id"])
        assert lineage.eligible(history, spec["id"]) is None
        assert str(history.state.reserved) == spec["max_charge"]
        assert history.state.blockers
    assert (data.path / "answers.jsonl").read_bytes() == before


@pytest.mark.parametrize("path,value", [
    (("error", "code"), 503),
    (("error", "code"), True),
    (("error", "metadata", "provider_name"), "Other synthetic provider"),
    (("error", "metadata", "provider_error_code"), "another_error"),
    (("error", "metadata", "limit_source"), "account_limit"),
    (("error", "metadata", "is_byok"), True),
    (("openrouter_metadata", "requested"), "another-synthetic-model"),
    (("openrouter_metadata", "strategy"), "fallback"),
    (("openrouter_metadata", "attempt"), 2),
    (("openrouter_metadata", "attempt"), True),
    (("openrouter_metadata", "is_byok"), True),
    (("openrouter_metadata", "endpoints", "available", 0, "selected"), True),
    (("openrouter_metadata", "endpoints", "available", 0, "model"), "another-model"),
    (("choices",), [{"message": {"content": "partial synthetic output"}}]),
    (("usage",), {"prompt_tokens": 1}),
])
def test_other_raw_envelopes_are_not_replaceable(data, path, value):
    lineage = api()

    def change(body, _observation):
        target = body
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    ref, spec = reject_one(data, alter=change)
    with lineage.History(ref) as history:
        assert not lineage.rejected(history, spec["id"])
        assert history.state.reserved > 0


@pytest.mark.parametrize("field,value", [
    ("http_status", 200), ("generation_id", None),
    ("header_generation_id", "another-generation"),
    ("body_generation_id", "another-generation"),
    ("content", "partial output"), ("model", "another-model"),
    ("response_model", "another-model"), ("response_provider", "another-provider"),
    ("usage", {"cost": "0.01"}),
])
def test_contrary_observation_is_not_a_replaceable_rejection(data, field, value):
    lineage = api()
    ref, spec = reject_one(data, alter=lambda _body, observed: observed.update({field: value}))
    with lineage.History(ref) as history:
        assert not lineage.rejected(history, spec["id"])


@pytest.mark.parametrize("failure", [
    "CancelledError", "synthetic callback failure", "synthetic cleanup failure", "LocalFailure",
])
def test_unexplained_later_failure_cannot_be_hidden_by_a_rejection(data, failure):
    lineage = api()
    ref, spec = reject_one(data, additional_failure=failure)
    with lineage.History(ref) as history:
        assert not lineage.rejected(history, spec["id"])


@pytest.mark.asyncio
async def test_actual_delegate_429_failure_order_and_single_physical_request(data):
    from genesis.eval.qualification import pinned

    lineage = api()
    # Use the same synthetic envelope but exercise actual SDK/observer failure
    # ordering independently of the manually appended predicate fixture.
    staged = copy.copy(data)
    staged.path = data.temp / "envelope-instrument"
    ref, spec = reject_one(staged)
    with lineage.History(ref) as history:
        body = history.state.attempts[spec["id"]]["observations"][0]["raw_body"]
    requests = []

    def respond(request):
        requests.append(request)
        assert request.method == "POST"
        return httpx.Response(429, content=body, headers={"x-generation-id": "synthetic-rejection"})

    data.manifest["binding"]["transports"][data.alias]["credential_mode"] = "shared_existing"
    spec["credential_mode"] = "shared_existing"

    async def admission(_router, _attempt, _info):
        return None

    with Journal(data.path, data.manifest) as journal:
        router = pinned.PinnedRouter(journal, alias=data.alias, params=data.params, dispatch=True,
                                     credential_mode="shared_existing", dispatch_admission=admission,
                                     transport=httpx.MockTransport(respond))
        router.bind(spec["contract"], 1, case_id=spec["case_id"])
        with pytest.raises(pinned.LocalFailure):
            await router.route_call("judge", data.checked["plan"][spec["contract"]][spec["case_id"]]["messages"])
    with lineage.History(reference(data)) as history:
        assert lineage.rejected(history, spec["id"])
        assert history.state.reserved > 0
        failures = [e["error"] for e in history.lines if e["kind"] == "failure"]
        assert failures == ["invalid financial evidence: 'usage'"]
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_bounded_child_marker_is_rejected_until_controller_reader_exists(data):
    lineage = api()
    # Presence is meaningful even when malformed/null: it cannot fall through
    # to legacy handling and erase the owning study's sibling liabilities.
    data.manifest["binding"]["study"] = None
    ref, _spec, _gateway = await retain_one(data)
    with pytest.raises(Incomplete, match="controller_reader"):
        lineage.History(ref)
