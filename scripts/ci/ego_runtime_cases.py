"""Frozen offline mechanics population; metadata never authorizes an effect."""

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Case:
    name: str
    kind: str
    wakes: int = 1
    reactive: bool = False
    fault: str | None = None
    blocked: bool = False


CASES = (
    Case("effect", "approved"),
    Case("noop", "echo"),
    Case("refusal", "pending"),
    Case("provider_error", "provider_error"),
    Case("handler_error", "handler_error"),
    Case("large_context", "approved"),
    Case("reactive_noop", "echo", reactive=True),
    Case("restart_duplicate", "approved", wakes=2),
    Case("reference_continuity", "approved", wakes=2),
    Case("observation_failure", "approved", fault="observation"),
    Case("final_failure", "approved", fault="final"),
    Case("reference_conflict", "approved", blocked=True),
    Case("reactive_reference_conflict", "approved", reactive=True, blocked=True),
)
SCENARIOS = tuple(case.name for case in CASES if not case.fault and not case.blocked)
FAULTS = tuple(case.name for case in CASES if case.fault)
CONFLICTS = tuple(case.name for case in CASES if case.blocked)
TWO_WAKES = tuple(case.name for case in CASES if case.wakes == 2)


def case_for(name):
    for case in CASES:
        if case.name == name:
            return case
    raise ValueError("unknown mechanics case")


def kind_for(name, second=False):
    # Preserve the existing B controller's mapping, including its second-wake
    # override. case_for is the strict selector for new consumers.
    return ("echo" if name in {"noop", "reactive_noop"} else "pending" if name == "refusal" or second else
            name if name in {"provider_error", "handler_error"} else "approved")


def manifest():
    return {"schema": "ego-runtime-mechanics/v1", "cases": [asdict(case) for case in CASES],
            "scope": "Synthetic invocation/continuity mechanics; no full policy, model quality or Codex parity."}


def outer_source(config):
    source = config.get("outer_source", "monolith")
    if type(source) is not str or source not in {"monolith", "ego-owned-loop"}:
        raise ValueError("invalid fixture outer source")
    return source
