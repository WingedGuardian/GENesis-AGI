"""Dollar reservations and incident recovery, derived only from campaign events.

This layer performs no network calls. Its caller must verify frozen maxima and
recovery evidence; transport and manifest preparation land in later PRs.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from decimal import MAX_EMAX, MIN_EMIN, Context, Decimal, localcontext

from .evidence import Campaign, Incomplete, canonical, load_json


def currency(value) -> Decimal:
    """Currency is a finite nonnegative fixed-point decimal string."""
    if not isinstance(value, str) or re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value) is None:
        raise Incomplete("currency must be a nonnegative decimal string")
    return Decimal(value)


def total(values) -> Decimal:
    """Sum without rounding to the process-global Decimal context."""
    values = list(values)
    if not values:
        return Decimal(0)
    with localcontext(Context(Emin=MIN_EMIN, Emax=MAX_EMAX)) as context:
        context.prec = max(
            28,
            max(v.adjusted() for v in values)
            - min(v.as_tuple().exponent for v in values)
            + len(str(len(values)))
            + 2,
        )
        return sum(values, Decimal(0))


def validate_manifest(manifest):
    if (
        not isinstance(manifest, dict)
        or type(manifest.get("version")) is not int
        or manifest["version"] != 1
        or not isinstance(manifest.get("binding"), dict)
        or not manifest["binding"]
        or not isinstance(manifest.get("attempts"), list)
        or not manifest["attempts"]
    ):
        raise Incomplete("invalid campaign manifest")
    currency(manifest.get("budget"))
    seen = set()
    for row in manifest["attempts"]:
        if not isinstance(row, dict) or any(
            not isinstance(row.get(k), str) or not row[k].strip()
            for k in ("id", "alias", "model", "upstream", "endpoint", "request_hash")
        ):
            raise Incomplete("invalid scheduled attempt")
        if row["id"] in seen or re.fullmatch(r"[a-f0-9]{64}", row["request_hash"]) is None:
            raise Incomplete("duplicate attempt or invalid request hash")
        seen.add(row["id"])
        currency(row.get("max_charge"))


@dataclass
class State:
    manifest: dict | None = None
    attempts: dict = field(default_factory=dict)
    blockers: set = field(default_factory=set)
    answers: dict = field(default_factory=dict)
    settled: Decimal = Decimal(0)
    reserved: Decimal = Decimal(0)


def _billing(row):
    """Missing cost can reconcile; contradictory or invalid evidence cannot settle."""
    charges, ids = [], set()
    if not row["observations"]:
        return None
    for answer in row["observations"]:
        if not isinstance(answer, dict):
            return None
        for key in ("model", "upstream"):
            if answer.get(key) is not None and answer[key] != row["spec"][key]:
                return None
        generation = answer.get("generation_id")
        if not isinstance(generation, str) or not generation.strip():
            return None
        ids.add(generation)
        usage = answer.get("usage")
        if usage is not None and not isinstance(usage, dict):
            return None
        if isinstance(usage, dict) and usage.get("cost") is not None:
            try:
                charges.append(currency(usage["cost"]))
            except Incomplete:
                return None
    receipts = row["receipts"]
    for receipt in receipts:
        if not isinstance(receipt, dict):
            return None
        if any(receipt.get(k) != row["spec"][k] for k in ("model", "upstream")):
            return None
        generation = receipt.get("generation_id")
        if not isinstance(generation, str) or not generation.strip():
            return None
        ids.add(generation)
        if receipt.get("charge") is None:
            continue
        try:
            charges.append(currency(receipt["charge"]))
        except Incomplete:
            return None
    if (
        not receipts
        or not charges
        or len(ids) != 1
        or not any(r.get("charge") is not None for r in receipts)
        or any(c != charges[0] for c in charges)
        or charges[0] > currency(row["spec"]["max_charge"])
    ):
        return None
    return charges[0]


def _apply(state, line, index):
    kind, attempt = line["kind"], line.get("attempt")
    if not isinstance(attempt, str) or attempt not in state.attempts:
        raise Incomplete("event names an unscheduled attempt")
    row = state.attempts[attempt]
    if kind == "reservation":
        if state.blockers:
            raise Incomplete("campaign stopped before reservation")
        if row.get("reserved_at") is not None or line.get("amount") != row["spec"]["max_charge"]:
            raise Incomplete("duplicate or contradictory reservation")
        if total([state.settled, state.reserved, currency(line["amount"])]) > currency(
            state.manifest["budget"]
        ):
            raise Incomplete("campaign budget exceeded")
        row["reserved_at"] = index
    elif row.get("reserved_at") is None:
        raise Incomplete("event precedes reservation")
    elif kind == "dispatch":
        if state.blockers - {f"reservation:{attempt}"}:
            raise Incomplete("campaign stopped before dispatch")
        if row["dispatched"] or line.get("request_hash") != row["spec"]["request_hash"]:
            raise Incomplete("duplicate dispatch or changed request")
        row["dispatched"] = True
    elif kind == "acknowledgement":
        incident = line.get("incident")
        if (
            incident
            not in {f"{prefix}:{attempt}" for prefix in ("reservation", "failure", "answer")}
            or incident not in state.blockers
            or not isinstance(line.get("resolution"), str)
            or not line["resolution"].strip()
        ):
            raise Incomplete("recovery must identify an incident and verified resolution")
        if incident == f"reservation:{attempt}":
            if row["dispatched"]:
                raise Incomplete("cannot acknowledge an unexplained reservation")
        elif _billing(row) is None:
            raise Incomplete("cannot acknowledge unresolved billing or identity")
        row["acknowledged"].add(incident)
    elif not row["dispatched"]:
        raise Incomplete("event precedes dispatch")
    elif kind == "observation":
        row["observations"].append(line.get("evidence"))
    elif kind == "billing":
        row["receipts"].append(line.get("receipt"))
    elif kind == "failure":
        if not isinstance(line.get("error"), str) or not line["error"].strip():
            raise Incomplete("failure must retain its error")
        row["failed"] = True
        row["acknowledged"].discard(f"failure:{attempt}")
    else:
        raise Incomplete("invalid journal event ordering")


def _summarize(state):
    generations = {}
    for attempt, row in state.attempts.items():
        for observation in row["observations"]:
            generation = observation.get("generation_id") if isinstance(observation, dict) else None
            if isinstance(generation, str) and generation.strip():
                generations.setdefault(generation, set()).add(attempt)
    collisions = {
        attempt for owners in generations.values() if len(owners) > 1 for attempt in owners
    }
    state.blockers, state.answers = {f"identity:{a}" for a in collisions}, {}
    settled, reserved = [], []
    for attempt, row in state.attempts.items():
        if row.get("reserved_at") is None:
            continue
        charge = None if attempt in collisions else _billing(row)
        if charge is None:
            reserved.append(currency(row["spec"]["max_charge"]))
            state.blockers.add(
                f"billing:{attempt}" if row["dispatched"] else f"reservation:{attempt}"
            )
        else:
            settled.append(charge)
            answers = row["observations"]
            if len(answers) == 1 and isinstance(answers[0].get("content"), str):
                state.answers[attempt] = copy.deepcopy(answers[0])
            else:
                state.blockers.add(f"answer:{attempt}")
        if row["failed"]:
            state.blockers.add(f"failure:{attempt}")
        state.blockers -= row["acknowledged"]
    state.settled, state.reserved = total(settled), total(reserved)


def reconstruct(lines) -> State:
    """Validate ordering and reconstruct liability; legacy evidence stays readable."""
    state = State()
    if not lines or lines[0]["kind"] != "manifest":
        state.blockers.add("legacy")
        return state
    state.manifest = copy.deepcopy(lines[0].get("manifest"))
    validate_manifest(state.manifest)
    state.attempts = {
        r["id"]: {
            "spec": r,
            "observations": [],
            "receipts": [],
            "dispatched": False,
            "failed": False,
            "acknowledged": set(),
        }
        for r in state.manifest["attempts"]
    }
    for index, line in enumerate(lines[1:], 1):
        _apply(state, line, index)
        _summarize(state)
    return state


class Journal(Campaign):
    """One writer for a frozen manifest; no completion can be dispatched twice."""

    def __init__(self, directory, manifest=None):
        super().__init__(directory)
        self.expected_manifest = copy.deepcopy(manifest)
        self.opened_at = 0
        self._derived = reconstruct([])

    def _load(self):
        super()._load()
        self._derived = reconstruct(self._lines)

    def __enter__(self):
        super().__enter__()
        try:
            if self.torn_tail:
                raise Incomplete("journal has an uncertain write")
            validate_manifest(self.expected_manifest)
            if not self._lines:
                super().append("manifest", manifest=self.expected_manifest)
                self._derived = reconstruct(self._lines)
            state = self.state
            if state.manifest is None:
                raise Incomplete("legacy journal cannot execute")
            if canonical(state.manifest) != canonical(self.expected_manifest):
                raise Incomplete("conflicting campaign manifest")
            self.opened_at = len(self._lines)
        except BaseException:
            self.__exit__()
            raise
        return self

    @property
    def state(self):
        with self._mutex:
            state = copy.deepcopy(self._derived)
            if self.torn_tail or self._poisoned:
                state.blockers.add("uncertain write")
            return state

    def append(self, kind, **data):
        with self._mutex:
            line = load_json(canonical({"kind": kind, **data}))
            if kind == "dispatch":
                attempt = line.get("attempt")
                row = self._derived.attempts.get(attempt) if isinstance(attempt, str) else None
                if row is None or row.get("reserved_at") is None:
                    raise Incomplete("dispatch requires reservation")
                ignore = f"reservation:{attempt}" if row["reserved_at"] >= self.opened_at else None
                self._ready(ignore)
            candidate = copy.deepcopy(self._derived)
            _apply(candidate, line, len(self._lines))
            _summarize(candidate)
            result = super().append(kind, **{k: v for k, v in line.items() if k != "kind"})
            self._derived = candidate  # Publish only after the event is durable.
            return result

    def _ready(self, ignore=None):
        if self._file is None or self.torn_tail or self._poisoned:
            raise Incomplete("journal is closed or has an uncertain write")
        blockers = self._derived.blockers - {ignore}
        if blockers:
            raise Incomplete("campaign stopped: " + ", ".join(sorted(blockers)))

    def reserve(self, attempt):
        self._ready()
        row = self._derived.attempts.get(attempt)
        if row is None:
            raise Incomplete("unscheduled attempt")
        return self.append("reservation", attempt=attempt, amount=row["spec"]["max_charge"])

    def dispatch(self, attempt):
        row = self._derived.attempts.get(attempt)
        if row is None or row.get("reserved_at") is None:
            raise Incomplete("dispatch requires reservation")
        return self.append("dispatch", attempt=attempt, request_hash=row["spec"]["request_hash"])

    def observe(self, attempt, evidence):
        return self.append("observation", attempt=attempt, evidence=evidence)

    def settle(self, attempt, receipt):
        return self.append("billing", attempt=attempt, receipt=receipt)

    def fail(self, attempt, error):
        return self.append("failure", attempt=attempt, error=error)

    def acknowledge(self, incident, resolution):
        # Incident IDs have a fixed prefix; attempt IDs themselves may contain colons.
        attempt = incident.partition(":")[2]
        return self.append(
            "acknowledgement", attempt=attempt, incident=incident, resolution=resolution
        )
