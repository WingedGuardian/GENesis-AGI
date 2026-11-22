"""Bounded continuation controls; all credentials, HTTP and evidence are synthetic."""

import copy
import importlib
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from genesis.eval.qualification import accounting, evidence, pinned, study_report
from tests.test_eval.qualification_lineage_fixtures import primitive_data, reject_one  # noqa: F401
from tests.test_eval.qualification_transport_fixtures import Gateway
from tests.test_eval.test_qualification_study import plan_for


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".genesis").mkdir(mode=0o700)


def api():
    assert importlib.util.find_spec("genesis.eval.qualification.continuation"), "controller missing"
    return importlib.import_module("genesis.eval.qualification.continuation")


def setup(data, *, replacements=1):
    binding = data.manifest["binding"]["transports"][data.alias]
    binding["credential_mode"] = "shared_existing"
    data.manifest["attempts"][0]["credential_mode"] = "shared_existing"
    plan, checked = plan_for(data, [])
    plan.update(max_total_replacements=replacements, max_new_attempts=3 + replacements)
    consent = {"version": 2, "protocol": study_report.PROTOCOL, "plan_hash": evidence.digest(plan),
               "max_new_attempts": plan["max_new_attempts"], "author": "owner",
               "decision_evidence": "Synthetic explicit test authorization only"}
    return plan, checked, consent


@pytest.mark.parametrize("change", [
    {"version": 1}, {"max_new_attempts": 99}, {"max_new_attempts": True},
    {"plan_hash": "f" * 64}, {"author": "model"}, {"decision_evidence": ""}, {"extra": 0},
])
def test_exact_new_consent_required(data, change):
    plan, _, consent = setup(data)
    consent.update(change)
    with pytest.raises(evidence.Incomplete):
        api().validate_consent(plan, consent)
    assert not Path(plan["study_root"]).exists()


def test_physical_identity_and_lean_manifest(data):
    plan, _, _ = setup(data)
    module = api()
    logical = plan["models"][data.alias]["manifest"]["attempts"][0]
    first = module.child_manifest(plan, data.alias, logical["id"], 0)
    second = module.child_manifest(plan, data.alias, logical["id"], 1)
    accounting.validate_manifest(first)
    assert first["attempts"][0]["id"] != second["attempts"][0]["id"]
    assert first["attempts"][0]["request_hash"] == logical["request_hash"]
    assert "admission" not in first["binding"]
    assert first["binding"]["study"]["admission_hash"] == evidence.digest(
        plan["models"][data.alias]["manifest"]["binding"]["admission"])
    assert len(evidence.canonical(first)) < 5000


@pytest.mark.asyncio
async def test_real_delegate_acquisition_single_attempt_and_closed_reader(data):
    module = api()
    plan, checked, consent = setup(data, replacements=0)
    gateway = Gateway(answer='{"score": 0}')
    with study_report.Study(plan, checked, data.cases) as study:
        controller = module.Controller(study, consent, inputs=[])
        with controller:
            spec = study.manifests[data.alias]["attempts"][0]
            expected = pinned.request_body(spec["model"], data.params, spec["contract"],
                                           checked["plan"][spec["contract"]][spec["case_id"]]["messages"])
            answer = await controller.acquire(data.alias, spec["id"], expected,
                                              transport=httpx.MockTransport(gateway))
            assert answer["content"] == '{"score": 0}'
        result = controller.finish(admission_current=True)
        assert result["writers_closed"] is True
    assert len(gateway.requests) == gateway.receipt_reads == 1
    assert gateway.key_reads == 0
    assert result["status"] == "incomplete"  # Two logical slots were never acquired.
    assert len(result["children"]) == 1
    with (pytest.raises(evidence.Incomplete, match="exists"),
          study_report.Study(plan, checked, data.cases) as study,
          module.Controller(study, consent, inputs=[])):
        pytest.fail("existing root relaunched")


@pytest.mark.asyncio
async def test_missing_billing_stops_without_replacement(data):
    module = api()
    plan, checked, consent = setup(data)
    gateway = Gateway(answer='{"score": 0}')

    def response(request):
        return httpx.Response(404, json={"error": "synthetic not found"}) if request.method == "GET" else gateway(request)

    with study_report.Study(plan, checked, data.cases) as study:
        controller = module.Controller(study, consent, inputs=[])
        with controller:
            spec = study.manifests[data.alias]["attempts"][0]
            body = pinned.request_body(spec["model"], data.params, spec["contract"],
                                       checked["plan"][spec["contract"]][spec["case_id"]]["messages"])
            with pytest.raises(pinned.LocalFailure):
                await controller.acquire(data.alias, spec["id"], body, transport=httpx.MockTransport(response))
        terminal = controller.finish(admission_current=True)
    assert len(gateway.requests) == 1
    assert len(terminal["children"]) == 1
    assert terminal["status"] == "incomplete"
    child = evidence.Campaign.read(Path(plan["study_root"]) / "attempts" / terminal["children"][0]["id"])
    state = accounting.reconstruct(child.lines)
    assert state.reserved == accounting.currency("0.1")


def test_consent_is_validated_before_credential_or_root(data, monkeypatch):
    module = api()
    plan, checked, consent = setup(data)
    consent["plan_hash"] = "f" * 64
    monkeypatch.setattr(pinned, "credential", lambda *_: pytest.fail("credential read"))
    with (study_report.Study(plan, checked, data.cases) as study,
          pytest.raises(evidence.Incomplete),
          module.Controller(study, consent, inputs=[])):
        pytest.fail("invalid consent entered")
    assert not Path(plan["study_root"]).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("reserved,allowed", [("4.9", True), ("4.900000000001", False)])
async def test_dispatch_funding_exact_decimal_boundary(data, reserved, allowed):
    module = api()
    plan, checked, consent = setup(data)
    spec = plan["models"][data.alias]["manifest"]["attempts"][0]
    adapter = SimpleNamespace(fingerprint=plan["credential_fingerprint"],
                              campaign=SimpleNamespace(attempt=lambda _identity: {"spec": spec}))
    with study_report.Study(plan, checked, data.cases) as study:
        controller = module.Controller(study, consent, inputs=[])
        controller.states["synthetic-funded-request"] = SimpleNamespace(
            settled=accounting.currency("0"), reserved=accounting.currency(reserved))
        if allowed:
            await controller.admission_hook(adapter, spec["id"], {"credential_mode": "shared_existing"})
        else:
            with pytest.raises(evidence.Incomplete, match="allowance"):
                await controller.admission_hook(adapter, spec["id"], {"credential_mode": "shared_existing"})


def test_terminal_refuses_extra_or_missing_sibling(data):
    module = api()
    plan, checked, consent = setup(data, replacements=0)
    with study_report.Study(plan, checked, data.cases) as study:
        controller = module.Controller(study, consent, inputs=[])
        with controller:
            pass
        terminal = controller.finish(admission_current=True)
    root = Path(plan["study_root"])
    extra = root / "attempts" / ("f" * 64)
    extra.mkdir(mode=0o700)
    with pytest.raises(evidence.Incomplete):
        module.read_root(root)
    assert terminal["children"] == []


@pytest.mark.asyncio
async def test_optional_scoring_stop_retains_error_and_legacy_default(data, monkeypatch):
    from genesis.eval.qualification import run

    calls = []
    spec = data.manifest["attempts"][0]
    schedule = {(spec["contract"], spec["version"], spec["case_id"], rep, spec["prompt_hash"]): str(rep)
                for rep in (1, 2, 3)}
    router = SimpleNamespace(schedule=schedule, dispatch=True,
                             campaign=SimpleNamespace(ready=lambda: None),
                             calls=[([], '{"score": 0}')],
                             bind=lambda *_a, **_kw: None,
                             stop_processing=lambda *_a: calls.append("stop"))

    async def malformed(*_args):
        calls.append("grade")
        return {"prediction": None, "error": "model_output_invalid"}

    monkeypatch.setattr(run, "_case", malformed)
    records, _ = await run.score(data.cases, router, data.temp, stop_on_error=True)
    assert calls == ["grade", "stop"]
    assert records[spec["contract"]][1][spec["case_id"]]["error"] == "model_output_invalid"
    assert records[spec["contract"]][2][spec["case_id"]]["local"] == "processing_stopped"
    calls.clear()
    await run.score(data.cases, router, data.temp)
    assert calls == ["grade", "grade", "grade"]


@pytest.mark.asyncio
async def test_cancelled_dispatch_is_not_replaced(data):
    import asyncio

    module = api()
    plan, checked, consent = setup(data)
    calls = []

    async def response(request):
        calls.append(request)
        raise asyncio.CancelledError

    with study_report.Study(plan, checked, data.cases) as study:
        controller = module.Controller(study, consent, inputs=[])
        with controller:
            spec = study.manifests[data.alias]["attempts"][0]
            body = pinned.request_body(spec["model"], data.params, spec["contract"],
                                       checked["plan"][spec["contract"]][spec["case_id"]]["messages"])
            with pytest.raises(asyncio.CancelledError):
                await controller.acquire(data.alias, spec["id"], body, transport=httpx.MockTransport(response))
        terminal = controller.finish(admission_current=True)
    assert len(calls) == 1
    assert len(terminal["children"]) == 1
    assert terminal["status"] == "incomplete"


@pytest.mark.asyncio
async def test_input_drift_before_dispatch_is_zero_post(data):
    module = api()
    plan, checked, consent = setup(data)
    changed = False

    def check(**_kwargs):
        if changed:
            raise evidence.Incomplete("synthetic input changed")

    gateway = Gateway()
    with study_report.Study(plan, checked, data.cases) as study:
        controller = module.Controller(study, consent, inputs=[SimpleNamespace(
            check=check, path=data.temp / "synthetic-input", hash="a" * 64)])
        with controller:
            changed = True
            spec = study.manifests[data.alias]["attempts"][0]
            body = pinned.request_body(spec["model"], data.params, spec["contract"],
                                       checked["plan"][spec["contract"]][spec["case_id"]]["messages"])
            with pytest.raises(evidence.Incomplete):
                await controller.acquire(data.alias, spec["id"], body, transport=gateway.transport)
        terminal = controller.finish(admission_current=False)
    assert gateway.requests == []
    assert terminal["children"] == []
    assert terminal["admission_current"] is False


async def retain_children(data, *, unsafe=False):
    module = api()
    plan, checked, consent = setup(data, replacements=0)
    gateway = Gateway(answer='{"score": 0}')
    with study_report.Study(plan, checked, data.cases) as study:
        controller = module.Controller(study, consent, inputs=[])
        with controller:
            for spec in study.manifests[data.alias]["attempts"][:2]:
                body = pinned.request_body(spec["model"], data.params, spec["contract"],
                                           checked["plan"][spec["contract"]][spec["case_id"]]["messages"])
                await controller.acquire(data.alias, spec["id"], body, transport=gateway.transport)
            if unsafe:
                controller.fail(evidence.Incomplete("synthetic late callback failure"),
                                physical=controller.children[-1])
        terminal = controller.finish(admission_current=True)
    root = module.read_root(Path(plan["study_root"]))
    try:
        refs = root.references()
    finally:
        root.close()
    new = copy.deepcopy(plan)
    new["study_root"] = str(data.temp / "future-study")
    new["histories"] = refs
    new["reuse"] = {data.alias: {}}
    for ref in refs:
        snapshot = evidence.Campaign.read(Path(ref["directory"]))
        marker = snapshot.lines[0]["manifest"]["binding"]["study"]
        child = next(c for c in terminal["children"] if c["id"] == Path(ref["directory"]).name)
        if child["safe"]:
            new["reuse"][data.alias][marker["logical"]] = {
                "journal_sha256": ref["journal_sha256"], "attempt": child["id"]}
    new["max_new_attempts"] = 3 - len(new["reuse"][data.alias])
    return new, checked, refs


@pytest.mark.asyncio
async def test_full_closed_child_census_reuses_answers_and_refuses_omitted_sibling(data):
    plan, checked, refs = await retain_children(data)
    with study_report.Study(plan, checked, data.cases) as study:
        assert len(study.rows[data.alias]) == 2
        assert study.financial()["observed_receipt_cost"] == "0.0002"
    plan["histories"] = refs[:1]
    with pytest.raises(evidence.Incomplete, match="sibling_omitted"):
        study_report.Study(plan, checked, data.cases)


@pytest.mark.asyncio
async def test_unsafe_sibling_is_still_in_financial_census(data):
    plan, checked, refs = await retain_children(data, unsafe=True)
    with study_report.Study(plan, checked, data.cases) as study:
        assert len(study.rows[data.alias]) == 1
        assert study.preview()["status"] == "incomplete"
        assert study.financial()["observed_receipt_cost"] == "0.0002"
        assert len(study.histories) == len(refs) == 2


@pytest.mark.asyncio
async def test_missing_terminal_has_visible_cost_but_no_automatic_reuse(data):
    plan, checked, refs = await retain_children(data)
    original = Path(refs[0]["closure"]["directory"])
    terminal = original / "terminal.json"
    terminal.rename(original / "retained-terminal-for-negative-control.json")
    for ref in refs:
        ref["closure"]["terminal_sha256"] = None
    plan["reuse"] = {data.alias: {}}
    plan["max_new_attempts"] = 3
    with study_report.Study(plan, checked, data.cases) as study:
        assert not study.rows[data.alias]
        assert study.financial()["observed_receipt_cost"] == "0.0002"
        assert study.preview()["status"] == "incomplete"


@pytest.mark.asyncio
async def test_partial_terminal_keeps_physical_costs_visible(data):
    plan, checked, refs = await retain_children(data)
    root = Path(refs[0]["closure"]["directory"])
    (root / "terminal.json").write_bytes(b'{"version":')
    original = evidence.Campaign.read(root).lines[0]["manifest"]["plan"]
    with study_report.Study(original, checked, data.cases) as study:
        study_report.include_current(study)
        assert study.financial()["observed_receipt_cost"] == "0.0002"
        assert len(study.histories) == 2
        assert not study.rows[data.alias]


@pytest.mark.asyncio
async def test_started_root_preview_censuses_child_costs(data):
    plan, checked, refs = await retain_children(data)
    root = Path(refs[0]["closure"]["directory"])
    original = evidence.Campaign.read(root).lines[0]["manifest"]["plan"]
    with study_report.Study(original, checked, data.cases) as study:
        preview = study.preview()
        assert preview["financial"]["observed_receipt_cost"] == "0.0002"
        assert preview["financial"]["settled_plus_reserved"] == "0.0002"


@pytest.mark.asyncio
async def test_sibling_census_is_validated_once_per_owner(data, monkeypatch):
    from genesis.eval.qualification import study_state

    plan, checked, _ = await retain_children(data)
    actual, calls = study_state.Root.references, []

    def count(self):
        calls.append(True)
        return actual(self)

    monkeypatch.setattr(study_state.Root, "references", count)
    with study_report.Study(plan, checked, data.cases):
        pass
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_malformed_grade_remains_a_measured_error_after_quality_stop(data):
    from genesis.eval.qualification import corpus, preflight

    module = api()
    rendered = await preflight.prepare(data.cases, data.temp)
    spec = data.manifest["attempts"][0]
    prepared = rendered[spec["contract"]][spec["case_id"]]
    data.params[corpus.route(spec["contract"])].update({
        k: v for k, v in prepared["call_parameters"].items() if k in pinned.ROUTE_KEYS})
    data.manifest["binding"]["admission"]["plan"] = rendered
    data.checked["plan"] = rendered
    spec.update(prompt_hash=prepared["prompt_hash"], request_hash=evidence.digest(
        pinned.request_body(spec["model"], data.params, spec["contract"], prepared["messages"])))
    plan, checked, consent = setup(data, replacements=0)
    gateway = Gateway(answer="malformed synthetic grade")
    with study_report.Study(plan, checked, data.cases) as study:
        controller = module.Controller(study, consent, inputs=[])
        with controller:
            view = module.LiveView(study, data.alias, controller)
            router = module.Router(view, alias=data.alias, params=data.params, transport=gateway.transport)
            from genesis.eval.qualification import run

            records, _ = await run.score(data.cases, router, data.temp, stop_on_error=True)
            assert records[spec["contract"]][1][spec["case_id"]]["error"] == "model_output_invalid"
        controller.finish(admission_current=True)
        detached = await study_report.report(study, data.cases, data.temp)
    repetition = detached["models"][data.alias]["contracts"][spec["contract"]]["repetitions"][0]
    assert repetition["errors"] == 1
    assert repetition["unscored"] == 0
    assert len(gateway.requests) == 1


@pytest.mark.asyncio
async def test_read_only_owner_accepts_relocated_exact_copy(data):
    import shutil

    module = api()
    plan, checked, refs = await retain_children(data)
    original = Path(refs[0]["closure"]["directory"])
    copied = data.temp / "restored-exact-owner"
    shutil.copytree(original, copied)
    root = module.read_root(copied)
    try:
        plan["histories"] = root.references()
    finally:
        root.close()
    with study_report.Study(plan, checked, data.cases) as study:
        assert len(study.rows[data.alias]) == 2
        assert study.financial()["observed_receipt_cost"] == "0.0002"


@pytest.mark.asyncio
async def test_closed_child_change_stops_before_next_post(data):
    module = api()
    plan, checked, consent = setup(data, replacements=0)
    gateway = Gateway(answer='{"score": 0}')
    with study_report.Study(plan, checked, data.cases) as study:
        controller = module.Controller(study, consent, inputs=[])
        with controller:
            for index, spec in enumerate(study.manifests[data.alias]["attempts"][:2]):
                body = pinned.request_body(spec["model"], data.params, spec["contract"],
                                           checked["plan"][spec["contract"]][spec["case_id"]]["messages"])
                if index == 0:
                    await controller.acquire(data.alias, spec["id"], body, transport=gateway.transport)
                    child = controller.children[0]
                    path = Path(plan["study_root"]) / "attempts" / child["id"] / "answers.jsonl"
                    with path.open("ab") as stream:
                        stream.write(b'{"kind":"failure"')
                else:
                    with pytest.raises(evidence.Incomplete):
                        await controller.acquire(data.alias, spec["id"], body, transport=gateway.transport)
        terminal = controller.finish(admission_current=True)
    assert len(gateway.requests) == 1
    assert terminal["admission_current"] is False


@pytest.mark.asyncio
async def test_bulk_final_checks_owner_once_and_standalone_checks_owner(data, monkeypatch):
    from genesis.eval.qualification import study_state

    plan, checked, _ = await retain_children(data)
    actual, calls = study_state.Root.check, []

    def count(self, **kwargs):
        calls.append(kwargs)
        return actual(self, **kwargs)

    monkeypatch.setattr(study_state.Root, "check", count)
    with study_report.Study(plan, checked, data.cases) as study:
        calls.clear()
        study.check(final=True)
        assert len(calls) == 1
        calls.clear()
        next(iter(study.histories.values())).check(final=True)
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_explicit_missing_copy_never_falls_back_to_original(data):
    _, checked, refs = await retain_children(data)
    root = Path(refs[0]["closure"]["directory"])
    original = evidence.Campaign.read(root).lines[0]["manifest"]["plan"]
    with study_report.Study(original, checked, data.cases) as study, pytest.raises(evidence.Incomplete):
        study_report.include_current(study, root_path=data.temp / "missing-explicit-copy")


@pytest.mark.asyncio
async def test_finalization_failure_preserves_initiating_exception(data, monkeypatch):
    """Unit seam for finalization, not complete-reference public acceptance."""
    import os

    from genesis.eval.qualification import run

    module = api()
    plan, checked, consent = setup(data, replacements=0)
    files = []
    for name, value in (("plan", plan), ("consent", consent)):
        path = data.temp / (name + ".json")
        path.write_bytes(evidence.canonical(value))
        path.chmod(0o600)
        files.append(path)
    original = RuntimeError("synthetic initiating failure")
    actual_sync = evidence.os.fsync
    gateway = Gateway(answer='{"score": 0}')

    async def admission(*_args, **_kwargs):
        return checked

    async def score(_cases, router, _temp, **_kwargs):
        spec = router.manifest["attempts"][0]
        router.bind(spec["contract"], spec["repetition"], case_id=spec["case_id"])
        await router.route_call("judge", checked["plan"][spec["contract"]][spec["case_id"]]["messages"])
        raise original

    def sync(fd):
        if os.readlink(f"/proc/self/fd/{fd}").endswith("/terminal.json"):
            raise OSError("synthetic terminal publication failure")
        actual_sync(fd)

    monkeypatch.setattr(module.preflight, "check", admission)
    monkeypatch.setattr(module, "source_inputs", lambda *_: [])
    monkeypatch.setattr(run, "score", score)
    monkeypatch.setattr(evidence.os, "fsync", sync)
    with pytest.raises(RuntimeError) as caught:
        await module.execute(*files, data.cases, data.temp, candidates={data.alias: data.params},
                             options={k: None for k in ("reference_policy", "current_reference_policy",
                                                       "reference_approval", "source_files")},
                             transport=gateway.transport)
    assert caught.value is original
    assert any("OSError" in note for note in original.__notes__)
    assert len(gateway.requests) == 1


def test_source_final_hash_rechecks_metadata_after_read(data, monkeypatch):
    module = api()
    path = data.temp / "mutable-source.txt"
    path.write_text("synthetic generation one")
    stamp = module.SourceStamp(path, module.preflight.file_hash(path))
    actual = module.preflight.file_hash
    replacement = data.temp / "replacement-source.txt"
    replacement.write_text("synthetic generation two")

    def changed_after_read(target):
        result = actual(target)
        # A distinct inode makes the mutation independent of timestamp ticks.
        replacement.replace(path)
        assert module.execution.identity(path.lstat()) != stamp.stamp
        return result

    monkeypatch.setattr(module.preflight, "file_hash", changed_after_read)
    with pytest.raises(evidence.Incomplete):
        stamp.check(final=True)


@pytest.mark.asyncio
async def test_pre_response_scoring_failure_stops_entire_controller(data, monkeypatch):
    from genesis.eval.qualification import run

    module = api()
    plan, checked, consent = setup(data, replacements=0)

    async def before_response(*_args):
        raise OSError("synthetic database preparation failure")

    monkeypatch.setattr(run, "_case", before_response)
    with study_report.Study(plan, checked, data.cases) as study:
        controller = module.Controller(study, consent, inputs=[])
        with controller:
            router = module.Router(module.LiveView(study, data.alias, controller),
                                   alias=data.alias, params=data.params)
            records, _ = await run.score(data.cases, router, data.temp, stop_on_error=True)
            spec = study.manifests[data.alias]["attempts"][0]
            assert records[spec["contract"]][1][spec["case_id"]]["local"] == "processing_failed"
            assert controller.stopped
        controller.finish(admission_current=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 429])
@pytest.mark.parametrize("cancel", [False, True])
async def test_rejection_with_stream_cleanup_failure_never_replaces(data, status, cancel):
    import asyncio

    module = api()
    plan, checked, consent = setup(data)
    staged = copy.copy(data)
    staged.path = data.temp / "stream-envelope"
    reject_one(staged)
    snapshot = evidence.Campaign.read(staged.path)
    raw = next(e["evidence"]["raw_body"] for e in snapshot.lines if e["kind"] == "observation")

    class ClosingFailure(httpx.AsyncByteStream):
        def __init__(self, body):
            self.body = body

        async def __aiter__(self):
            yield self.body

        async def aclose(self):
            if cancel:
                raise asyncio.CancelledError
            raise RuntimeError("synthetic stream cleanup failure")

    gateway, posts = Gateway(answer='{"score": 0}'), []

    def response(request):
        if request.method == "POST":
            posts.append(request)
            if len(posts) == 1:
                body = gateway(request).content if status == 200 else raw.encode()
                return httpx.Response(status, stream=ClosingFailure(body),
                                      headers={"x-generation-id": "gen-1" if status == 200 else "stream-rejection"})
        return gateway(request)

    with study_report.Study(plan, checked, data.cases) as study:
        controller = module.Controller(study, consent, inputs=[])
        with controller:
            spec = study.manifests[data.alias]["attempts"][0]
            body = pinned.request_body(spec["model"], data.params, spec["contract"],
                                       checked["plan"][spec["contract"]][spec["case_id"]]["messages"])
            with pytest.raises(asyncio.CancelledError if cancel else pinned.LocalFailure):
                await controller.acquire(data.alias, spec["id"], body, transport=httpx.MockTransport(response))
        terminal = controller.finish(admission_current=True)
    assert len(posts) == 1
    assert len(terminal["children"]) == 1
    assert controller.stopped


@pytest.mark.parametrize("cancel", [False, True])
def test_root_close_failure_preserves_initiating_exception(data, monkeypatch, cancel):
    import asyncio

    module = api()
    plan, checked, consent = setup(data, replacements=0)
    original = asyncio.CancelledError() if cancel else RuntimeError("synthetic primary failure")
    actual = evidence.Campaign.__exit__

    def close(self, *args):
        actual(self, *args)
        raise OSError("synthetic root close failure")

    with study_report.Study(plan, checked, data.cases) as study:
        controller = module.Controller(study, consent, inputs=[])
        monkeypatch.setattr(evidence.Campaign, "__exit__", close)
        with pytest.raises(type(original)) as caught, controller:
            raise original
        assert caught.value is original
        assert any("OSError" in note for note in original.__notes__)
        assert not controller.closed
        assert not (Path(plan["study_root"]) / "terminal.json").exists()


def test_study_run_cli_requires_explicit_consent():
    from genesis.eval.qualification.__main__ import parser

    with pytest.raises(SystemExit):
        parser().parse_args(["study-run", "--corpus", "synthetic", "--temp-root", "synthetic",
                             "--study-plan", "synthetic"])
    args = parser().parse_args(["study-run", "--corpus", "synthetic", "--temp-root", "synthetic",
                                "--study-plan", "synthetic", "--spending-approval", "synthetic"])
    assert args.command == "study-run"


@pytest.mark.asyncio
@pytest.mark.parametrize("second_reject", [False, True])
async def test_precise_pool_rejection_allows_only_one_replacement(data, second_reject):
    module = api()
    # Leave total capacity available so only the per-coordinate fence stops a
    # repeated rejection at this coordinate.
    plan, checked, consent = setup(data, replacements=2)
    staged = copy.copy(data)
    staged.path = data.temp / "synthetic-envelope"
    ref, spec = reject_one(staged)
    snapshot = evidence.Campaign.read(staged.path)
    raw = next(e["evidence"]["raw_body"] for e in snapshot.lines if e["kind"] == "observation")
    gateway, posts = Gateway(answer='{"score": 0}'), []

    def response(request):
        if request.method == "POST":
            posts.append(request)
            if len(posts) == 1 or second_reject:
                return httpx.Response(429, content=raw, headers={"x-generation-id": f"reject-{len(posts)}"})
        return gateway(request)

    with study_report.Study(plan, checked, data.cases) as study:
        controller = module.Controller(study, consent, inputs=[])
        with controller:
            logical = study.manifests[data.alias]["attempts"][0]
            body = pinned.request_body(logical["model"], data.params, logical["contract"],
                                       checked["plan"][logical["contract"]][logical["case_id"]]["messages"])
            if second_reject:
                with pytest.raises(pinned.LocalFailure):
                    await controller.acquire(data.alias, logical["id"], body, transport=httpx.MockTransport(response))
            else:
                answer = await controller.acquire(data.alias, logical["id"], body, transport=httpx.MockTransport(response))
                assert answer["content"] == '{"score": 0}'
        terminal = controller.finish(admission_current=True)
    assert len(posts) == 2
    assert len(terminal["children"]) == 2
    assert sum(accounting.reconstruct(evidence.Campaign.read(
        Path(plan["study_root"]) / "attempts" / child["id"]).lines).reserved
               for child in terminal["children"]) >= accounting.currency("0.1")


@pytest.mark.asyncio
async def test_zero_replacement_cap_stops_on_first_precise_rejection(data):
    module = api()
    plan, checked, consent = setup(data, replacements=0)
    staged = copy.copy(data)
    staged.path = data.temp / "zero-cap-envelope"
    reject_one(staged)
    raw = next(e["evidence"]["raw_body"] for e in evidence.Campaign.read(staged.path).lines
               if e["kind"] == "observation")
    posts = []

    def response(request):
        posts.append(request)
        return httpx.Response(429, content=raw, headers={"x-generation-id": "zero-cap-rejection"})

    with study_report.Study(plan, checked, data.cases) as study:
        controller = module.Controller(study, consent, inputs=[])
        with controller:
            spec = study.manifests[data.alias]["attempts"][0]
            body = pinned.request_body(spec["model"], data.params, spec["contract"],
                                       checked["plan"][spec["contract"]][spec["case_id"]]["messages"])
            with pytest.raises(pinned.LocalFailure):
                await controller.acquire(data.alias, spec["id"], body, transport=httpx.MockTransport(response))
        terminal = controller.finish(admission_current=True)
    assert len(posts) == len(terminal["children"]) == 1
    assert controller.stopped
