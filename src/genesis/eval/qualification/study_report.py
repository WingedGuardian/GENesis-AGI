"""Offline continuation admission and logical reports over immutable real journals.

Logical views are scoring inputs, never accounting journals. This module cannot
read a credential, submit a completion, reconcile a receipt or approve spending.
"""

import copy
import subprocess
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

from . import accounting, execution, lineage, pinned, preflight, run, sampling
from .evidence import Incomplete, canonical, digest

PROTOCOL = "bounded_continuation_v1"
KEYS = {"version", "protocol", "study_root", "source_commit", "runner_sources",
        "financial_promise", "credential_mode", "credential_fingerprint", "combined_allowance",
        "price_snapshot", "models", "histories", "expense_evidence", "reuse",
        "max_total_replacements", "max_new_attempts", "cooldown_seconds"}


def source_identity():
    """Freeze every qualification module, including additive controller modules."""
    package = Path(__file__).resolve().parent
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=package, check=True,
                            capture_output=True, text=True).stdout.strip()
    return {"source_commit": commit,
            "runner_sources": {p.name: preflight.file_hash(p) for p in sorted(package.glob("*.py"))}}


def coordinate(spec):
    return tuple(spec.get(k) for k in (
        "alias", "contract", "version", "case_id", "repetition", "prompt_hash"))


def same_request(prior, current):
    fields = ("alias", "model", "upstream", "endpoint", "generation_namespace", "request_hash",
              "credential_fingerprint", "receipt_provider", "provider_identity_evidence")
    return (canonical(coordinate(prior)) == canonical(coordinate(current))
            and all(prior.get(k) == current.get(k) for k in fields)
            and pinned.model_identities(prior) == pinned.model_identities(current)
            and pinned.frozen_credential_mode(prior) == pinned.frozen_credential_mode(current)
            and pinned.frozen_receipt_policy(prior) == pinned.frozen_receipt_policy(current))


class Study:
    """Validated frozen inputs; no constructor grants execution authority."""

    def __init__(self, plan, checked, cases):
        self._stack = ExitStack()
        self.plan, self.checked = copy.deepcopy(plan), copy.deepcopy(checked)
        self.histories, self.auxiliary, self.annotations, self.receipt_identities = {}, [], {}, {}
        try:
            self._validate(cases)
            self._read()
            self._select()
            self._fund()
            self.check(final=True)
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def close(self):
        self._stack.close()

    def _validate(self, cases):
        p = self.plan
        source = source_identity()
        if (not isinstance(p, dict) or set(p) != KEYS or type(p["version"]) is not int
                or p["version"] != 3 or p["protocol"] != PROTOCOL
                or p["financial_promise"] != execution.PROMISE
                or p["credential_mode"] != "shared_existing"
                or not lineage.is_hash(p["credential_fingerprint"])
                or not isinstance(p["study_root"], str) or not Path(p["study_root"]).is_absolute()
                or any(type(p[k]) is not int or p[k] < 0 for k in (
                    "max_total_replacements", "max_new_attempts", "cooldown_seconds"))
                or p["cooldown_seconds"] > 600
                or any(not isinstance(p[k], list) for k in ("histories", "expense_evidence"))
                or any(p[k] != v for k, v in source.items())):
            raise Incomplete("study_plan_invalid_or_stale")
        price = p["price_snapshot"]
        if (not isinstance(price, dict) or set(price) != {"observed_at", "evidence"}
                or any(not isinstance(v, str) or not v.strip() for v in price.values())):
            raise Incomplete("study_price_evidence_missing")
        self.aliases = execution.selected_aliases(p["models"])
        if set(self.checked["models"]) != set(self.aliases):
            raise Incomplete("study_candidate_population_changed")
        self.manifests = {}
        for alias in self.aliases:
            model = p["models"][alias]
            if not isinstance(model, dict) or set(model) != {"manifest"}:
                raise Incomplete("study_model_invalid")
            manifest = model["manifest"]
            run.validate_campaign(manifest, self.checked, alias, cases=cases)
            if sampling.has_sample(manifest) and self.aliases != execution.PRO_COMPARISON:
                raise Incomplete("incumbent_sample_population_invalid")
            transport = manifest["binding"]["transports"][alias]
            if (pinned.frozen_credential_mode(transport) != "shared_existing"
                    or transport["credential_fingerprint"] != p["credential_fingerprint"]
                    or any(accounting.currency(r["max_charge"]) <= 0 for r in manifest["attempts"])):
                raise Incomplete("study_transport_invalid")
            self.manifests[alias] = manifest
        self.admission = digest(run.admission_identity(self.checked))

    def _claim(self, owner, row):
        for item in row["observations"] + row["receipts"] + row["lookups"]:
            for field in ("generation_id", "body_generation_id", "header_generation_id", "response_generation_id"):
                generation = item.get(field)
                if generation is not None:
                    if not isinstance(generation, str) or not generation.strip():
                        raise Incomplete("study_generation_invalid")
                    key = row["spec"]["generation_namespace"], generation
                    if key in self.owners and self.owners[key] != owner:
                        raise Incomplete("study_generation_collision")
                    self.owners[key] = owner

    def _annotate(self, history, attempt, raw, *, model, provider):
        row = history._state.attempts[attempt]
        envelope = pinned.financial_json(raw, (("data", "total_cost"),))
        if "error" in envelope:
            raise Incomplete("study_expense_error_envelope")
        data = envelope["data"]
        generation = lineage._generation(row)
        if (not generation or data.get("id") != generation or data.get("model") != model
                or not isinstance(model, str) or not model.strip()
                or data.get("provider_name") != provider or provider != row["spec"]["receipt_provider"]):
            raise Incomplete("study_expense_association_changed")
        owner = history.hash, attempt
        charge = accounting.currency(data["total_cost"])
        identity = generation, model, provider
        if owner in self.annotations and (self.annotations[owner] != charge
                                         or self.receipt_identities[owner] != identity):
            raise Incomplete("study_expense_contradiction")
        settled = accounting._billing(row)
        usages = [item["usage"]["cost"] for item in row["observations"]
                  if isinstance(item.get("usage"), dict) and item["usage"].get("cost") is not None]
        if ((settled is not None and charge != settled)
                or any(accounting.currency(v) != charge for v in usages)):
            raise Incomplete("study_expense_contradiction")
        self.annotations[owner] = charge
        self.receipt_identities[owner] = identity

    def _read(self):
        self.owners, paths = {}, set()
        for ref in self.plan["histories"]:
            history = self._stack.enter_context(lineage.History(ref))
            if history.hash in self.histories or history.directory.resolve() in paths:
                raise Incomplete("study_duplicate_history")
            paths.add(history.directory.resolve())
            self.histories[history.hash] = history
            for attempt, row in history._state.attempts.items():
                if row["spec"].get("credential_fingerprint") != self.plan["credential_fingerprint"]:
                    raise Incomplete("study_historical_credential_changed")
                self._claim((history.hash, attempt), row)
                for receipt in row["receipts"]:
                    raw = receipt.get("raw_body")
                    # Invalid old receipts remain unresolved. A raw GET cost can
                    # still be disclosed separately without settling it.
                    if raw and receipt.get("response_model") and receipt.get("response_provider"):
                        self._annotate(history, attempt, raw.encode(), model=receipt["response_model"],
                                       provider=receipt["response_provider"])
        seen = set()
        for entry in self.plan["expense_evidence"]:
            required = {"path", "sha256", "journal_sha256", "attempt", "request_hash",
                        "generation_id", "model", "provider"}
            if not isinstance(entry, dict) or set(entry) != required or entry["sha256"] in seen:
                raise Incomplete("study_expense_evidence_invalid")
            history = self.histories[entry["journal_sha256"]]
            row = history._state.attempts[entry["attempt"]]
            if (entry["request_hash"] != row["spec"]["request_hash"]
                    or entry["generation_id"] != lineage._generation(row)
                    or entry["model"] != pinned.model_identities(row["spec"])[2]):
                raise Incomplete("study_expense_association_changed")
            frozen = self._stack.enter_context(lineage.Frozen(entry["path"], entry["sha256"]))
            self.auxiliary.append(frozen)
            seen.add(frozen.hash)
            self._annotate(history, entry["attempt"], frozen.raw,
                           model=entry["model"], provider=entry["provider"])

    def _select(self):
        self.rows, self.reuse, self.missing, self.replaced, self.execution_issues = {}, {}, {}, set(), []
        # Preserve acquisition order while comparing each large shared admission
        # once per history/alias, rather than once per observed answer.
        by_coordinate, admitted = {}, {}
        for history in self.histories.values():
            for alias, manifest in self.manifests.items():
                admitted[history.hash, alias] = lineage.compatible(history._state.manifest, manifest)
            for attempt, row in history._state.attempts.items():
                charges = [self.annotations.get((history.hash, attempt))]
                charges.extend(accounting.currency(o["usage"]["cost"]) for o in row["observations"]
                               if isinstance(o.get("usage"), dict) and o["usage"].get("cost") is not None)
                if any(c is not None and c > accounting.currency(row["spec"]["max_charge"])
                       for c in charges):
                    self.execution_issues.append("historical_charge_exceeds_allocation")
                by_coordinate.setdefault(coordinate(row["spec"]), []).append((history, attempt, row))
        for alias, manifest in self.manifests.items():
            self.rows[alias], self.reuse[alias], self.missing[alias] = {}, {}, []
            for spec in manifest["attempts"]:
                logical, rejected, unsafe = spec["id"], 0, False
                for history, attempt, row in by_coordinate.get(coordinate(spec), ()):
                    if not row["dispatched"]:
                        unsafe |= row.get("reserved_at") is not None
                        continue
                    matching = admitted[history.hash, alias] and same_request(row["spec"], spec)
                    answer = lineage.eligible(history, attempt) if matching else None
                    if answer is not None and logical not in self.rows[alias]:
                        self.rows[alias][logical] = answer
                        self.reuse[alias][logical] = {"journal_sha256": history.hash, "attempt": attempt}
                    elif matching and lineage.rejected(history, attempt):
                        rejected += 1
                    else:
                        unsafe = True
                if logical not in self.rows[alias]:
                    self.missing[alias].append(logical)
                    if unsafe or rejected > 1:
                        self.execution_issues.append("missing_coordinate_has_nonreplaceable_history")
                if rejected:
                    self.replaced.add((alias, logical))
        if canonical(self.plan["reuse"]) != canonical(self.reuse):
            raise Incomplete("study_reuse_map_changed")

    def _fund(self):
        missing = [r for a, m in self.manifests.items() for r in m["attempts"]
                   if r["id"] in self.missing[a]]
        remaining = self.plan["max_total_replacements"] - len(self.replaced)
        # Each still-unanswered coordinate can receive at most one replacement.
        extra = sorted((accounting.currency(r["max_charge"]) for r in missing
                        if (r["alias"], r["id"]) not in self.replaced), reverse=True)
        if remaining < 0 or remaining > len(extra) or self.plan["max_new_attempts"] != len(missing) + remaining:
            raise Incomplete("study_attempt_cap_invalid")
        self.pending = accounting.total([*(accounting.currency(r["max_charge"]) for r in missing),
                                         *extra[:remaining]])
        liability = accounting.total(accounting.total([h._state.settled, h._state.reserved])
                                     for h in self.histories.values())
        self.liability = liability
        if accounting.total([liability, self.pending]) > accounting.currency(self.plan["combined_allowance"]):
            raise Incomplete("study_allowance_insufficient")

    def check(self, *, final=False):
        for item in [*self.histories.values(), *self.auxiliary]:
            item.check(final=final)
        current = source_identity()
        if any(self.plan[k] != v for k, v in current.items()):
            raise Incomplete("study_source_changed")

    def financial(self):
        result = execution.cost_report({key: h._state for key, h in self.histories.items()})
        return {**result, "raw_reported_cost": str(accounting.total(self.annotations.values())),
                "raw_cost_scope": "Read-only receipt annotations; may overlap unresolved reservations.",
                "settled_plus_reserved": str(self.liability), "new_allocation": str(self.pending),
                "combined_allowance": self.plan["combined_allowance"]}

    def preview(self):
        self.check(final=True)
        existing = Path(self.plan["study_root"]).exists() or Path(self.plan["study_root"]).is_symlink()
        return {"status": "incomplete" if existing or self.execution_issues else "pass",
                "scope": "offline inherited-evidence admission only; no spending approval",
                "plan_hash": digest(self.plan), "reused": sum(map(len, self.reuse.values())),
                "missing": sum(map(len, self.missing.values())),
                "max_new_attempts": self.plan["max_new_attempts"],
                "execution_issues": sorted(set(self.execution_issues + (["study_root_exists"] if existing else []))),
                "financial": self.financial()}


class LogicalView:
    """Ephemeral scoring projection; physical rows and their specs stay intact."""

    def __init__(self, study, alias):
        self.study, self.alias, self.failed = study, alias, False
        self.manifest = study.manifests[alias]
        self.rows = {r["id"]: study.rows[alias].get(r["id"], {
            "spec": r, "dispatched": False, "observations": [], "totals": (0, 0),
        }) for r in self.manifest["attempts"]}

    @property
    def state(self):
        return SimpleNamespace(manifest=copy.deepcopy(self.manifest), attempts=copy.deepcopy(self.rows))

    def attempt(self, identity):
        return copy.deepcopy(self.rows[identity])

    def ready(self):
        self.study.check()
        if self.failed:
            raise Incomplete("study_processing_stopped")

    def fail(self, _identity, _error):
        self.failed = True


async def report(study, cases, temp_root, *, config=None):
    reports, records, states = {}, {}, {}
    for alias, manifest in study.manifests.items():
        view = LogicalView(study, alias)
        selected = sampling.campaign_cases(cases, study.checked, manifest, alias)
        router = pinned.PinnedRouter(view, alias=alias, params=study.checked["parameters"][alias],
                                     dispatch=False, config=config)
        records[alias], clamped = await run.score(selected, router, temp_root)
        reports[alias] = run.summarize(alias, selected, records[alias], clamped, None)
        reports[alias].pop("history")
        reports[alias].pop("financial")
        if sampling.has_sample(manifest):
            sampling.baseline_report(reports[alias], selected, records[alias], cases)
        reports[alias]["admission"] = study.admission
        states[alias] = view.state
    financial = study.financial()
    if len(reports) == 2:
        result = sampling.comparison(cases, reports, records, states, financial)
    else:
        result = {**sampling.aggregate_status(reports, financial), "models": reports, "financial": financial}
    result["lineage"] = study.preview()
    result["physical_history"] = {
        kind: sum(event["kind"] == kind for h in study.histories.values() for event in h._lines)
        for kind in ("dispatch", "observation", "billing", "failure", "receipt_lookup")}
    # This entry reports an inherited prefix. It never invents study closure.
    result.update(status="incomplete", terminal="unstarted_or_unverified")
    return result


async def inspect(plan_path, cases, temp_root, *, candidates, options, scoring=False, config=None):
    """Public read-only entry always computes full current admission internally."""
    with ExitStack() as stack:
        plan = execution.Input(plan_path)
        stack.callback(plan.close)

        async def admission():
            return await preflight.check(
                cases, temp_root, reference_policy=options["reference_policy"],
                current_policy=options["current_reference_policy"],
                reference_approval=options["reference_approval"], source_files=options["source_files"],
                candidates=candidates, config=config)

        checked = await admission()
        study = stack.enter_context(Study(plan.value, checked, cases))
        result = await report(study, cases, temp_root, config=config) if scoring else study.preview()
        if digest(run.admission_identity(await admission())) != study.admission:
            raise Incomplete("study_admission_changed")
        study.check(final=True)
        plan.check(final=True)
        return result
