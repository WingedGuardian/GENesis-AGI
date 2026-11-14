"""Explicitly consented, finite acquisition; never restart an existing study."""

import asyncio
import copy
import hashlib
import os
import stat
import sys
from contextlib import ExitStack
from pathlib import Path

from . import accounting, execution, lineage, pinned, preflight, run, sampling, study_report
from .evidence import Campaign, Incomplete, canonical, digest, private_open, sync_directory
from .study_state import child_manifest
from .study_state import read_root as read_root


def validate_consent(plan, consent):
    if (not isinstance(consent, dict) or set(consent) != {
        "version", "protocol", "plan_hash", "max_new_attempts", "author", "decision_evidence"
    } or type(consent["version"]) is not int or consent["version"] != 2
            or consent["protocol"] != study_report.PROTOCOL or consent["plan_hash"] != digest(plan)
            or type(consent["max_new_attempts"]) is not int
            or consent["max_new_attempts"] != plan["max_new_attempts"]
            or consent["author"] != "owner" or not isinstance(consent["decision_evidence"], str)
            or not consent["decision_evidence"].strip()):
        raise Incomplete("study_spending_approval_missing_or_changed")


def written_hash(journal):
    """Campaign.append writes precisely canonical(line) plus one LF per record."""
    result = hashlib.sha256()
    for line in journal.lines:
        result.update(canonical(line) + b"\n")
    return result.hexdigest()


class SourceStamp:
    """Metadata between requests; complete byte hashes at admission/final boundaries."""

    def __init__(self, path, expected):
        self.path, self.hash = Path(path), expected
        info = self.path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise Incomplete("study_source_not_regular")
        self.stamp = execution.identity(info)
        self.check(final=True)

    def check(self, *, final=False):
        if execution.identity(self.path.lstat()) != self.stamp:
            raise Incomplete("study_source_input_changed")
        if final and preflight.file_hash(self.path) != self.hash:
            raise Incomplete("study_source_bytes_changed")
        if final:
            SourceStamp.check(self)


def source_inputs(checked, options):
    root = Path(__file__).resolve().parents[4]
    result = [SourceStamp(root / p, h) for p, h in checked["production_sources"].items()]
    retention = options["reference_approval"]["source_retention"]
    for kind in ("primary_generation", "restored_backup_generation"):
        generation = Path(retention[kind])
        manifest = generation / "manifest.json"
        result.append(SourceStamp(manifest, digest({"version": 1, "sources": options["source_files"]})))
        result.extend(SourceStamp(generation / "blobs" / h, h)
                      for h in set(checked["reference_sources"].values()))
    return result


class Controller:
    """One invocation owns serial real Journals and an exclusive root writer."""

    def __init__(self, study, consent, *, inputs):
        self.study, self.plan, self.consent = study, study.plan, copy.deepcopy(consent)
        self.inputs, self.stack, self.lock = list(inputs), ExitStack(), ExitStack()
        self.child_inputs, self.child_stack = [], ExitStack()
        self.children, self.states, self.stopped, self.closed = [], {}, False, False
        self.root, self.error, self.active = None, None, None
        self.replaced = set(study.replaced)

    def __enter__(self):
        try:
            validate_consent(self.plan, self.consent)
            self.check(final=True)
            if self.study.execution_issues:
                raise Incomplete("study_history_nonreplaceable")
            self.lock.enter_context(execution.comparison_lock(self.plan["credential_fingerprint"]))
            directory = Path(self.plan["study_root"])
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                raise Incomplete("study_root_exists_never_relaunch") from None
            sync_directory(directory.parent)
            attempts = directory / "attempts"
            attempts.mkdir(mode=0o700)
            sync_directory(attempts)
            sync_directory(directory)
            self.root = self.stack.enter_context(Campaign(directory))
            self.root.append("manifest", manifest={
                "version": 1, "kind": "bounded_study", "plan": self.plan,
                "input_hashes": {str(i.path): i.hash for i in self.inputs},
            })
            return self
        except BaseException:
            self.stack.close()
            self.lock.close()
            self.child_stack.close()
            raise

    def __exit__(self, _kind=None, error=None, _trace=None):
        try:
            if error is not None:
                self.fail(error)
        finally:
            try:
                self.stack.close()
            except BaseException as closing:
                self.lock.close()
                self.child_stack.close()
                if error is None or not isinstance(closing, Exception):
                    raise
                error.add_note("study writer close failed: " + type(closing).__name__)
            else:
                self.closed = True

    def check(self, *, final=False):
        validate_consent(self.plan, self.consent)
        self._inputs_check(final=final)
        if self.stopped:
            raise Incomplete("study_processing_stopped")

    def _inputs_check(self, *, final=False):
        self.study.check(final=final)
        for item in [*self.inputs, *self.child_inputs]:
            item.check(final=final)
        if self.root is not None:
            names = {p.name for p in (self.root.directory / "attempts").iterdir()}
            if names != {c["id"] for c in self.children}:
                raise Incomplete("study_owned_child_census_changed")

    def fail(self, error, *, physical=None):
        self.stopped, self.error = True, type(error).__name__
        if physical is not None:
            physical["safe"] = False
        if self.root is not None and not self.closed:
            try:
                self.root.append("failure", error="study_stopped:" + type(error).__name__)
            except BaseException as append_error:
                error.add_note("study failure append failed: " + type(append_error).__name__)

    def liability(self):
        return accounting.total([self.study.liability, *(accounting.total([s.settled, s.reserved])
                                                       for s in self.states.values())])

    async def admission_hook(self, adapter, attempt, info):
        self.check()
        spec = adapter.campaign.attempt(attempt)["spec"]
        if (adapter.fingerprint != self.plan["credential_fingerprint"]
                or info != {"credential_mode": "shared_existing"}
                or accounting.total([self.liability(), accounting.currency(spec["max_charge"])])
                   > accounting.currency(self.plan["combined_allowance"])):
            raise Incomplete("study_allowance_or_credential_changed")

    async def acquire(self, alias, logical, expected, *, transport=None, config=None):
        self.check()
        if self.root is None or self.closed or logical in self.study.rows[alias]:
            raise Incomplete("study_coordinate_not_available")
        spec = next(r for r in self.study.manifests[alias]["attempts"] if r["id"] == logical)
        ordinal = int((alias, logical) in self.replaced)
        while True:
            self.check()
            if len(self.children) >= self.plan["max_new_attempts"]:
                raise Incomplete("study_physical_attempt_cap")
            manifest = child_manifest(self.plan, alias, logical, ordinal)
            identity = manifest["attempts"][0]["id"]
            directory = Path(self.plan["study_root"]) / "attempts" / identity
            entry = {"id": identity, "safe": False}
            self.children.append(entry)
            journal, caught = accounting.Journal(directory, manifest), None
            try:
                with journal:
                    adapter = pinned.PinnedRouter(
                        journal, alias=alias, params=self.study.checked["parameters"][alias],
                        dispatch=True, credential_mode="shared_existing",
                        dispatch_admission=self.admission_hook, transport=transport, config=config)
                    adapter.bind(spec["contract"], spec["repetition"], case_id=spec["case_id"])
                    try:
                        await adapter.route_call("qualification-study", expected["messages"])
                    except BaseException as exc:
                        caught = exc
                # No replacement decision before the real writer has closed.
                state = journal.state
                self.states[identity] = state
                entry.update(journal_sha256=written_hash(journal),
                             manifest_hash=digest(manifest))
                frozen = self.child_stack.enter_context(lineage.Frozen(
                    directory / "answers.jsonl", entry["journal_sha256"]))
                self.child_inputs.append(frozen)
                self.study._claim((entry["journal_sha256"], identity), state.attempts[identity])
                if caught is None:
                    self.check()
                    answer = lineage.eligible_row(state, identity, self.study.manifests[alias])
                    if answer is None:
                        raise Incomplete("study_answer_unverified")
                    secret = pinned.credential("shared_existing")
                    if hashlib.sha256(secret.encode()).hexdigest() != self.plan["credential_fingerprint"]:
                        raise Incomplete("study_credential_changed")
                    entry["safe"] = True
                    self.study.rows[alias][logical] = answer
                    return answer["observations"][0]
                if not isinstance(caught, Exception):
                    raise caught
                failures = [e["error"] for e in journal.lines if e["kind"] == "failure"]
                if (ordinal != 0 or len(self.replaced) >= self.plan["max_total_replacements"]
                        or not lineage.rejected_row(state.attempts[identity], failures)):
                    raise caught
                entry["safe"] = True  # Safe known rejection; its expense remains reserved.
                self.replaced.add((alias, logical))
                await asyncio.sleep(self.plan["cooldown_seconds"])
                self.check(final=True)
                ordinal = 1
            except BaseException as exc:
                self.fail(exc, physical=entry if not entry["safe"] else None)
                raise

    def finish(self, *, admission_current):
        """Exclusive publication after writers close; a torn terminal never certifies."""
        try:
            return self._finish(admission_current=admission_current)
        finally:
            self.lock.close()
            self.child_stack.close()

    def _finish(self, *, admission_current):
        if not self.closed or self.root is None:
            raise Incomplete("study_writers_not_closed")
        if type(admission_current) is not bool:
            raise Incomplete("study_admission_flag_invalid")
        try:
            self._inputs_check(final=True)
        except Exception:
            admission_current = False
        complete = all(len(self.study.rows[a]) == len(m["attempts"])
                       for a, m in self.study.manifests.items())
        terminal = {
            "version": 1, "plan_hash": digest(self.plan),
            "root_journal_sha256": written_hash(self.root),
            "children": self.children, "writers_closed": True,
            "admission_current": admission_current,
            "status": "pass" if complete and admission_current and not self.stopped else "incomplete",
            "error": self.error,
        }
        if any(set(c) != {"id", "journal_sha256", "manifest_hash", "safe"} for c in self.children):
            raise Incomplete("study_child_closure_incomplete")
        with lineage.Frozen(self.root.directory / "answers.jsonl", terminal["root_journal_sha256"]):
            pass
        fd = private_open(self.root.directory / "terminal.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(terminal))
            stream.flush()
            os.fsync(stream.fileno())
        sync_directory(self.root.directory)
        return terminal


class LiveView(study_report.LogicalView):
    def __init__(self, study, alias, controller):
        super().__init__(study, alias)
        self.controller = controller

    def ready(self):
        self.controller.check()

    def fail(self, _identity, error):
        self.controller.fail(Incomplete(error))


class Router(pinned.PinnedRouter):
    def __init__(self, view, **kwargs):
        self.controller = view.controller
        super().__init__(view, dispatch=True, credential_mode="shared_existing",
                         dispatch_admission=self.controller.admission_hook, **kwargs)

    def stop_processing(self, case_id, error):
        try:
            super().stop_processing(case_id, error)
        finally:
            if not self.controller.stopped:
                self.controller.fail(error)

    async def _send(self, attempt, expected):
        answer = await self.controller.acquire(self.alias, attempt, expected,
                                               transport=self.transport, config=self.config)
        self.campaign.rows[attempt] = self.controller.study.rows[self.alias][attempt]
        return answer


async def execute(plan_path, consent_path, cases, temp_root, *, candidates, options,
                  config=None, transport=None, input_paths=(), input_descriptors=()):
    """Compute complete admission internally; saved preflight is never authority."""
    async def admission():
        return await preflight.check(
            cases, temp_root, reference_policy=options["reference_policy"],
            current_policy=options["current_reference_policy"],
            reference_approval=options["reference_approval"], source_files=options["source_files"],
            candidates=candidates, config=config)

    with ExitStack() as stack:
        inputs = []
        for path in (plan_path, consent_path):
            item = execution.Input(path)
            inputs.append(item)
            stack.callback(item.close)
        for path in input_paths:
            item = execution.Input(path, decode=bytes)
            inputs.append(item)
            stack.callback(item.close)
        checked = await admission()
        inputs.extend(input_descriptors)
        inputs.extend(source_inputs(checked, options))
        study = stack.enter_context(study_report.Study(inputs[0].value, checked, cases))
        controller = Controller(study, inputs[1].value, inputs=inputs)
        reports, records, states = {}, {}, {}
        current = False
        try:
            with controller:
                for alias, manifest in study.manifests.items():
                    view = LiveView(study, alias, controller)
                    router = Router(view, alias=alias, params=checked["parameters"][alias],
                                    transport=transport, config=config)
                    selected = sampling.campaign_cases(cases, checked, manifest, alias)
                    records[alias], clamped = await run.score(selected, router, temp_root, stop_on_error=True)
                    reports[alias] = run.summarize(alias, selected, records[alias], clamped, None)
                    if sampling.has_sample(manifest):
                        sampling.baseline_report(reports[alias], selected, records[alias], cases)
                    states[alias] = view.state
                current = digest(run.admission_identity(await admission())) == study.admission
        finally:
            if controller.closed:
                active = sys.exception()
                try:
                    controller.finish(admission_current=current)
                except BaseException as finalization:
                    if active is None or not isinstance(finalization, Exception):
                        raise
                    active.add_note("study finalization failed: " + type(finalization).__name__)
        return await study_report.report(study, cases, temp_root, config=config)
