"""Fixed expectations for canonical proposal-operation probes, not policy code."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Step:
    action: str
    result: bool | int | None
    status: str
    revision: int = 1
    argument_revision: int = 1


@dataclass(frozen=True)
class Case:
    name: str
    steps: tuple[Step, ...]
    effects: int
    scope: str | None = "operate"


A = Step("approve", True, "approved")
C = Step("claim", True, "executed")
CASES = (
    Case("revocation-wins", (A, Step("revoke", True, "rejected"),
         Step("approve", False, "rejected"), Step("claim", False, "rejected")), 0),
    Case("dispatch-wins", (A, C, Step("revoke", False, "executed"),
         Step("approve", False, "executed"), Step("claim", False, "executed")), 1),
    Case("rejection-wins", (Step("reject", True, "rejected"),
         Step("approve", False, "rejected"), Step("claim", False, "rejected")), 0),
    Case("revision-wins", (Step("revise", 2, "pending", 2),
         Step("approve", False, "pending", 2), Step("claim", False, "pending", 2),
         Step("approve", True, "approved", 2, 2), Step("claim", True, "executed", 2)), 1),
    Case("approval-wins-revision", (A, Step("revise", None, "approved"), C), 1),
    Case("duplicate-approval", (A, Step("approve", False, "approved"), C,
         Step("claim", False, "executed")), 1),
    Case("develop-scope-blocked", (A, Step("claim", False, "approved")), 0, "develop"),
    Case("operate-scope-allowed", (A, C), 1),
    Case("legacy-scope-allowed", (A, C), 1, None),
)
