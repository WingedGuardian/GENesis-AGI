"""Serial operating allocations with disclosed overrun risk, not invoice ceilings.

Consent is a trusted local operator assertion. This module authenticates neither
the human decision nor future provider charges. Journals retain all incidents.
"""

import fcntl
import hashlib
import os
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from pathlib import Path

from . import pinned
from .accounting import Journal, _billing, currency, total
from .evidence import Incomplete, canonical, check_directory, digest, load_json, private_open

ALIASES = ("openrouter-mimo", "openrouter-deepseek-flash")
PRO_COMPARISON = ("openrouter-deepseek-v4", "openrouter-deepseek-flash")
PROMISE = "operating_budget_with_overrun_risk"
STOP = "stop_both_on_unresolved_evidence_no_resend"
SINGLE_STOP = "stop_campaign_on_unresolved_evidence_no_resend"


def selected_aliases(models):
    """Select one explicit population, preserving its serial order."""
    if isinstance(models, dict):
        for aliases in ((ALIASES[1],), ALIASES, PRO_COMPARISON):
            if set(models) == set(aliases):
                return aliases
    raise Incomplete("operating_plan_invalid")


def identity(info):
    return tuple(getattr(info, "st_" + name) for name in (
        "dev", "ino", "size", "mtime_ns", "ctime_ns"
    ))


class Input:
    """A retained private descriptor; metadata per request, full hashes at boundaries."""

    def __init__(self, path):
        self.path = Path(path).absolute()
        self.fd = private_open(self.path, os.O_RDONLY)
        try:
            self.stamp = identity(os.fstat(self.fd))
            with os.fdopen(os.dup(self.fd), "rb") as stream:
                raw = stream.read()
            self.hash = hashlib.sha256(raw).hexdigest()
            self.value = load_json(raw)
            self.check()
        except BaseException:
            self.close()
            raise

    def check(self, *, final=False):
        if (identity(os.fstat(self.fd)) != self.stamp
                or identity(self.path.lstat()) != self.stamp):
            raise Incomplete("operating_input_changed")
        if final:
            os.lseek(self.fd, 0, os.SEEK_SET)
            with os.fdopen(os.dup(self.fd), "rb") as stream:
                raw = stream.read()
            if hashlib.sha256(raw).hexdigest() != self.hash:
                raise Incomplete("operating_input_changed")
            self.check()

    def close(self):
        os.close(self.fd)


def observed_charge(row):
    """Use the journal's identity/consistency rules without its allocation ceiling.

    Removing ONLY that ceiling permits an above-allocation receipt to remain an
    actual expense in the report. It never settles or releases journal liability.
    """
    receipts = row["receipts"]
    try:
        charges = [currency(r["charge"]) for r in receipts if r.get("charge") is not None]
    except (Incomplete, TypeError, KeyError, AttributeError):
        return None
    if not charges or any(r.get("error") for r in receipts):
        return None
    measured = _billing({**row, "spec": {**row["spec"], "max_charge": str(total(charges))}})
    if measured is None:
        return None
    generation = receipts[0].get("generation_id")
    # The gateway's GET namespace owns generation IDs, not the selected upstream
    # or its display name. Both campaigns use the same OpenRouter /generation.
    return (row["spec"]["generation_namespace"], generation), measured


def validate(plan, consent, checked, *, cases=None):
    from . import run

    shared = isinstance(plan, dict) and type(plan.get("version")) is int and plan["version"] == 2
    keys = {"version", "financial_promise", "stop_semantics", "combined_allowance",
            "credential_fingerprint", "price_snapshot", "models"}
    keys |= {"credential_mode"} if shared else {"expected_limit", "baseline_usage"}
    if (not isinstance(plan, dict) or set(plan) != keys
            or type(plan["version"]) is not int or plan["version"] != (2 if shared else 1)
            or (shared and plan["credential_mode"] != "shared_existing")
            or plan["financial_promise"] != PROMISE):
        raise Incomplete("operating_plan_invalid")
    aliases = selected_aliases(plan["models"])
    if (set(checked["models"]) != set(aliases)
            or plan["stop_semantics"] != (SINGLE_STOP if len(aliases) == 1 else STOP)):
        raise Incomplete("operating_plan_invalid")
    if (not isinstance(consent, dict)
            or set(consent) != {"version", "plan_hash", "scope", "author", "decision_evidence"}
            or type(consent["version"]) is not int or consent["version"] != 1
            or consent["plan_hash"] != digest(plan)
            or consent["author"] != "owner"
            or not isinstance(consent["decision_evidence"], str)
            or not consent["decision_evidence"].strip()):
        raise Incomplete("spending_approval_missing_or_changed")
    fingerprint = plan["credential_fingerprint"]
    if (not isinstance(fingerprint, str) or len(fingerprint) != 64
            or any(c not in "0123456789abcdef" for c in fingerprint)):
        raise Incomplete("operating_identity_invalid")
    combined = currency(plan["combined_allowance"])
    envelope = combined if shared else total([
        currency(plan["expected_limit"]), currency(plan["baseline_usage"]).copy_negate()
    ])
    snapshot = plan["price_snapshot"]
    if (not isinstance(snapshot, dict) or set(snapshot) != {"observed_at", "evidence"}
            or any(not isinstance(v, str) or not v.strip() for v in snapshot.values())):
        raise Incomplete("operating_price_evidence_missing")
    manifests, budgets, allocations = {}, [], []
    for alias in aliases:
        model = plan["models"][alias]
        if not isinstance(model, dict) or set(model) != {"manifest"}:
            raise Incomplete("operating_model_invalid")
        manifest = deepcopy(model["manifest"])
        sampled = "baseline_sample" in manifest["binding"]
        if sampled and aliases != PRO_COMPARISON:
            raise Incomplete("incumbent_sample_population_invalid")
        run.validate_campaign(manifest, checked, alias, cases=cases)
        transport = manifest["binding"]["transports"][alias]
        mode = "shared_existing" if shared else "dedicated"
        if ((not sampled and len(manifest["attempts"]) != 2250)
                or pinned.frozen_credential_mode(transport) != mode
                or transport["credential_fingerprint"] != fingerprint
                or any(currency(row["max_charge"]) <= 0 for row in manifest["attempts"])):
            raise Incomplete("operating_schedule_invalid")
        budgets.append(currency(manifest["budget"]))
        allocations.extend(currency(row["max_charge"]) for row in manifest["attempts"])
        manifest["binding"]["operating_plan_hash"] = digest(plan)
        manifest["binding"]["financial_promise"] = PROMISE
        manifests[alias] = manifest
    if consent["scope"] != f"{sum(len(m['attempts']) for m in manifests.values())}_requests":
        raise Incomplete("spending_approval_missing_or_changed")
    if combined <= 0 or total(budgets) > combined or not total(allocations) <= envelope <= combined:
        raise Incomplete("operating_allowance_invalid")
    return manifests


class Operating:
    """One campaign or pair, with boundary history reads and per-attempt updates."""

    def __init__(self, plan, consent, checked, *, cases=None):
        from .run import admission_identity

        self.inputs, self.plan = (plan, consent), plan.value
        self.manifests = validate(plan.value, consent.value, checked, cases=cases)
        self.aliases = tuple(self.manifests)
        self.admission = digest(admission_identity(checked))
        self.journals, self.costs, self.owners = {}, {}, {}
        self.observed = currency("0")
        self.pending, self.last, self.stopped = currency("0"), None, False

    def eligible(self, checked):
        from .run import admission_identity

        if digest(admission_identity(checked)) != self.admission:
            raise Incomplete("operating_admission_changed")
        for item in self.inputs:
            item.check()

    def attach(self, journals):
        self.journals = journals
        pending = []
        for alias, journal in journals.items():
            state = journal.state
            if canonical(state.manifest) != canonical(self.manifests[alias]):
                raise Incomplete("operating_history_changed")
            for attempt, row in state.attempts.items():
                if row["dispatched"]:
                    self.last = journal, attempt
                    self.update(alias, attempt, row)
                else:
                    pending.append(currency(row["spec"]["max_charge"]))
            journal.ready()
        self.pending = total(pending)

    def update(self, alias, attempt, row):
        measured = observed_charge(row)
        if measured is None:
            raise Incomplete("operating_cost_unresolved")
        generation, charge = measured
        owner = alias, attempt
        if generation in self.owners and self.owners[generation] != owner:
            raise Incomplete("operating_generation_collision")
        self.observed = total([self.observed, self.costs.get(owner, currency("0")).copy_negate(), charge])
        self.owners[generation], self.costs[owner] = owner, charge

    def stop(self, error, info=None):
        self.stopped = True
        if self.last is not None:
            journal, attempt = self.last
            # Fixed reason and validated numeric key observations, never credentials
            # or arbitrary transport exception text. Existing Journal is the ledger.
            try:
                journal.append("failure", attempt=attempt,
                               error="operating_comparison_stopped:" + type(error).__name__,
                               **({"key_status": info} if info is not None else {}))
            except BaseException as append_error:
                # Journal poison still refuses reads and new acquisition. Keep
                # the initiating error, including cancellation, authoritative.
                error.add_note("operating failure evidence append failed: " + type(append_error).__name__)

    def predicate(self, info):
        if self.plan.get("credential_mode") == "shared_existing":
            if (info != {"credential_mode": "shared_existing"}
                    or total([self.observed, self.pending]) > currency(self.plan["combined_allowance"])):
                raise Incomplete("operating_allowance_invalid")
            return
        if (info.get("limit_reset", "missing") is not None
                or currency(info["limit"]) != currency(self.plan["expected_limit"])
                or currency(info["usage"]) != total([
                    currency(self.plan["baseline_usage"]), self.observed
                ])
                or currency(info["limit_remaining"]) <= 0
                or currency(info["limit_remaining"]) < self.pending
                or currency(info.get("available")) < self.pending):
            raise Incomplete("operating_key_discrepancy")

    async def admission_hook(self, adapter, _attempt, info):
        if self.stopped:
            raise Incomplete("operating_comparison_stopped")
        try:
            for item in self.inputs:
                item.check()
            for journal in self.journals.values():
                journal.ready()
            if adapter.fingerprint != self.plan["credential_fingerprint"]:
                raise Incomplete("operating_credential_changed")
            self.predicate(info)
        except BaseException as exc:
            self.stop(exc, info)
            raise

    def router(self, journal, **kwargs):
        return OperatingRouter(journal, operating=self, **kwargs)

    def report(self):
        return {**cost_report({a: j.state for a, j in self.journals.items()}),
                "financial_promise": PROMISE,
                "combined_allowance": self.plan["combined_allowance"], "stopped": self.stopped}


def cost_report(states):
    """Census every generation claim before deciding which expenses are resolved."""
    known, owners, accounting, unresolved, over = {}, {}, {}, set(), 0
    for alias, state in states.items():
        accounting[alias] = {"journal_settled": str(state.settled),
                             "journal_reserved": str(state.reserved)}
        for attempt, row in state.attempts.items():
            if not row["dispatched"]:
                continue
            owner = alias, attempt
            # Invalid or missing costs still carry ownership claims. Deduplicate
            # repeated GET evidence within this attempt, not across owners.
            claims = {item[field] for item in row["observations"] + row["receipts"]
                      if isinstance(item, dict)
                      for field in ("generation_id", "body_generation_id", "header_generation_id")
                      if isinstance(item.get(field), str) and item[field].strip()}
            for generation in claims:
                key = row["spec"]["generation_namespace"], generation
                owners.setdefault(key, set()).add(owner)
            measured = observed_charge(row)
            if measured is None:
                unresolved.add(owner)
            else:
                known[owner] = measured[1]
                over += measured[1] > currency(row["spec"]["max_charge"])
    for claimed in owners.values():
        if len(claimed) > 1:
            unresolved.update(claimed)
    return {"observed_receipt_cost": str(total(charge for owner, charge in known.items()
                                                if owner not in unresolved)),
            "unresolved_attempts": len(unresolved), "over_allocation_attempts": over,
            "journal_accounting": accounting,
            "limitations": ["Late or undisclosed provider charges can exceed the allowance.",
                            "Observed receipts are not an authenticated final invoice."]}


class OperatingRouter(pinned.PinnedRouter):
    def __init__(self, campaign, *, operating, **kwargs):
        super().__init__(campaign, dispatch_admission=operating.admission_hook,
                         credential_mode=operating.plan.get("credential_mode", "dedicated"), **kwargs)
        self.operating = operating

    async def _send(self, attempt, expected):
        if self.operating.stopped:
            raise Incomplete("operating_comparison_stopped")
        try:
            answer = await super()._send(attempt, expected)
            row = self.campaign.attempt(attempt)
            self.operating.last = self.campaign, attempt
            self.operating.pending = total([
                self.operating.pending, currency(row["spec"]["max_charge"]).copy_negate()
            ])
            self.operating.update(self.alias, attempt, row)
            secret = pinned.credential(self.credential_mode)
            if hashlib.sha256(secret.encode()).hexdigest() != self.fingerprint:
                raise Incomplete("operating_credential_changed")
            info = (await pinned.key_status(secret, self.transport)
                    if self.credential_mode == "dedicated" else {"credential_mode": "shared_existing"})
            await self.operating.admission_hook(self, attempt, info)
            return answer
        except BaseException as exc:
            try:
                if self.campaign.attempt(attempt)["dispatched"]:
                    self.operating.last = self.campaign, attempt
                self.operating.stop(exc)
            except BaseException as append_error:
                exc.add_note("operating failure append failed: " + type(append_error).__name__)
            raise

    def stop_processing(self, case_id, error):
        self.operating.stop(error)
        super().stop_processing(case_id, error)


@contextmanager
def comparison_lock(fingerprint):
    # Stable across campaign order and caller-selected plan/campaign directories.
    root = Path.home() / ".genesis" / "qualification-locks"
    root.mkdir(mode=0o700, exist_ok=True)
    check_directory(root)
    fd = private_open(root / (fingerprint + ".lock"), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


async def execute(cases, checked, paths, temp_root, *, plan_path, approval_path, options,
                  config=None, transport=None):
    from . import run

    aliases = selected_aliases(paths)
    if len({Path(p).resolve() for p in paths.values()}) != len(aliases):
        raise Incomplete("paired_campaigns_invalid")
    with ExitStack() as stack:
        plan = Input(plan_path)
        stack.callback(plan.close)
        consent = Input(approval_path)
        stack.callback(consent.close)
        operating = Operating(plan, consent, checked, cases=cases)
        if aliases != operating.aliases:
            raise Incomplete("paired_campaigns_invalid")
        stack.enter_context(comparison_lock(operating.plan["credential_fingerprint"]))
        journals = {alias: stack.enter_context(Journal(paths[alias], operating.manifests[alias]))
                    for alias in aliases}
        try:
            operating.attach(journals)
        except (ValueError, OSError) as exc:
            operating.stop(exc)
        reports, records = {}, {}
        for alias in aliases:
            try:
                reports[alias], records[alias] = await run.qualify(
                    alias, cases, checked["parameters"][alias], journals[alias], temp_root,
                    dispatch=True, operating=operating, config=config, transport=transport, **options
                )
            except BaseException as exc:
                operating.stop(exc)
                raise
        try:
            for item in operating.inputs:
                item.check(final=True)
        except BaseException as exc:
            operating.stop(exc)
            raise
        financial = operating.report()
        if len(aliases) == 2:
            from .sampling import comparison

            result = comparison(cases, reports, records,
                                {a: j.state for a, j in journals.items()}, financial)
            if operating.stopped:
                result["status"] = "incomplete"
                if "comparison_completion" in result:
                    result["comparison_completion"] = "incomplete"
            return result
        return {"status": "incomplete" if operating.stopped else
                run.worst((r["status"] for r in reports.values()), run.OVERALL),
                "models": reports,
                "financial": financial}
