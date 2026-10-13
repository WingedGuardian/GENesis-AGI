"""The evidence document a validator records against a ``pr_verifications`` row,
and the ONE function that turns it into a verdict.

WHY THIS MODULE EXISTS, AND WHY IT IS IN ``src/``. The validator CLI
(``scripts/pr_verification.py``) used to hold this policy itself, as a hand-written
shape validator plus eight refusal rules policing a verdict the caller asserted. Two
review rounds and an adversarial pass produced twenty findings against it, and the
premise check that followed MEASURED why: across 176 enumerated documents, 88.6% had
exactly one legal verdict (so asking the caller for it added only a chance to be
refused), and 2.8% had NO legal verdict at all — the rules contradicted each other.
Every answer grew the rule list, and every larger list produced a new contradiction.

So the shape changed. The document is validated by a strict pydantic model (the
house pattern in ``genesis.decisions.types``, whose own docstring names this exact
failure: hand-written per-field checks are a denylist, and ``str()`` quietly
coerces what it should refuse). The verdict is then DERIVED from the document by
:func:`derive`, a decision tree that is total by construction — every valid
document yields exactly one verdict, so no pair of rules can disagree. The caller
keeps one judgment bit (``park``) for the region that genuinely is a judgment.

It lives in ``src/`` so the policy is importable by any writer, not only the CLI:
``src/`` cannot import ``scripts/``, and a second caller (an MCP tool is the
anticipated one) would otherwise bypass every rule here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from genesis.db.crud.pr_verifications import MAX_EVIDENCE_BYTES, PASS_VERDICTS, VERDICTS

__all__ = [
    "CLAIM_VERDICTS",
    "DEPLOY_METHODS",
    "ESTABLISHING_TIERS",
    "MAX_EVIDENCE_BYTES",
    "NOTE_PREFIXED_GAPS",
    "NOT_DEPLOYED",
    "NO_CONTROL_GAP",
    "SUSPECTED_FAILURE",
    "UNDEPLOYED_FAILURE",
    "TIERS",
    "Decision",
    "Derivation",
    "EvidenceDocument",
    "EvidenceError",
    "Refusal",
    "decide",
    "derive",
    "parse_evidence",
    "reason_for",
]

#: Evidence tiers, in the vocabulary CLAUDE.md's evidence rule already uses, so a
#: validator is not asked to learn a second one.
TIERS = ("MEASURED", "INFERRED", "READ", "NOT_VERIFIABLE_HERE")

#: The tiers that ESTABLISH a claim. INFERRED is deliberately absent: an inferred
#: claim never enters permanent record in the grammar of a fact, and a discharged
#: obligation is permanent record. READ is present on purpose: a change whose
#: deployment was established by content — the diff is present and correct in the
#: deployed artifact — is verified by reading it.
ESTABLISHING_TIERS = ("MEASURED", "READ")

#: How deployment was established. ``ancestry`` is the weakest: a fast positive path,
#: never a negative verdict (a stacked PR lands inside its parent's squash).
DEPLOY_METHODS = ("behaviour", "content", "ancestry")

#: Per-CLAIM verdicts — whether that one claim held. Distinct from the four SESSION
#: verdicts, which :func:`derive` computes across claims. ``unverified`` exists so a
#: claim nobody could check is not forced into ``pass`` (a falsification) or ``fail``
#: (an accusation that escalates to the user); it pairs with ``NOT_VERIFIABLE_HERE``
#: exactly — the model refuses either one without the other.
CLAIM_VERDICTS = ("pass", "fail", "unverified")

#: The gap named automatically when a document carries no negative control. A
#: missing control IS a measured gap: without one a verification cannot separate
#: "the change did this" from "this was already true".
NO_CONTROL_GAP = "no negative control"

#: The gap prefix for a claim marked ``fail`` at ``INFERRED``. A failure nobody
#: established is not a finding to take to the user as one, and it is not a pass
#: either: it keeps the row OPEN, named, until someone measures it.
SUSPECTED_FAILURE = "SUSPECTED FAILURE (INFERRED)"

#: The gap named when ``deploy.established`` is false: nothing measured on a tree
#: that was not shown to carry the merge says anything about the merge, so the row
#: stays OPEN whatever the claims say.
NOT_DEPLOYED = "DEPLOYMENT NOT ESTABLISHED"

#: The gap prefix for a claim that FAILED on a tree not shown to carry the merge.
#: Not fail-intent — the failure may be the stale tree's — but never dropped either.
UNDEPLOYED_FAILURE = "FAILED WHERE DEPLOYMENT WAS NOT ESTABLISHED"

#: Gaps that keep a row open AND must be visible on the backlog's one line, so the
#: CLI writes them ahead of the validator's note rather than only into the document.
NOTE_PREFIXED_GAPS = (NOT_DEPLOYED, UNDEPLOYED_FAILURE, SUSPECTED_FAILURE)

#: Prose: any non-blank string, whitespace-trimmed. Never a coerced number, bool,
#: null or object — the exact values ``str(x).strip()`` used to wave through.
Text = Annotated[StrictStr, StringConstraints(strip_whitespace=True, min_length=1)]

#: ``owner/name``. Not stripped: it is half of the ledger row's key and is compared
#: exactly, so a padded slug is refused BY NAME (see the validator) rather than
#: silently trimmed into a different spelling of the key.
Repo = Annotated[StrictStr, StringConstraints(pattern=r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")]

_CONFIG = ConfigDict(extra="forbid")


class Deploy(BaseModel):
    """HOW deployment was checked, and WHETHER it was established.

    ``established`` is required and structured because :func:`derive` gates on it:
    a claim measured on a checkout that does not carry the merge proves nothing
    about the merge, and when the outcome lived only in ``detail`` prose the
    verdict could not see it, so a stale-tree measurement could close a row
    permanently (Codex, round 3)."""

    model_config = _CONFIG

    method: Literal["behaviour", "content", "ancestry"]
    established: StrictBool
    detail: Text


class Claim(BaseModel):
    model_config = _CONFIG

    claim: Text
    verdict: Literal["pass", "fail", "unverified"]
    tier: Literal["MEASURED", "INFERRED", "READ", "NOT_VERIFIABLE_HERE"]
    measurement: Text

    @model_validator(mode="after")
    def _unverified_iff_not_verifiable_here(self) -> Claim:
        # Either half alone is incoherent: "pass" on a claim nobody could check is a
        # falsification that closes rows, and "unverified" at MEASURED says a
        # measurement was taken and then not believed.
        if (self.tier == "NOT_VERIFIABLE_HERE") != (self.verdict == "unverified"):
            raise ValueError(
                f"verdict {self.verdict!r} with tier {self.tier!r} is incoherent: a "
                f"claim nobody could check is verdict 'unverified' with tier "
                f"'NOT_VERIFIABLE_HERE', and only that pair uses either value"
            )
        return self


class Finding(BaseModel):
    model_config = _CONFIG

    summary: Text
    disposition: Text


class EvidenceDocument(BaseModel):
    """The whole evidence document. ``extra="forbid"``: an unknown key is almost
    always a typo of a real one (``scope_limit`` for ``scope_limits``), and dropping
    it silently would lose exactly the field the writer meant to fill."""

    model_config = _CONFIG

    repo: Repo
    # Upper bound = SQLite's INTEGER range: past it the driver raises OverflowError
    # at write time, a traceback where a named refusal belongs.
    pr: StrictInt = Field(gt=0, le=2**63 - 1)
    merge_commit: Text
    deploy: Deploy
    claims: list[Claim] = Field(min_length=1)
    controls: list[Text] = []
    scope_limits: list[Text] = []
    findings: list[Finding] = []

    @field_validator("repo", mode="before")
    @classmethod
    def _repo_padding_is_named(cls, value: Any) -> Any:
        # pydantic's own refusal for a padded slug is a raw regex dump, which reads
        # as "malformed" when the value LOOKS identical to the right one on a
        # terminal. Name the actual problem.
        if isinstance(value, str) and value.strip() and value != value.strip():
            raise ValueError(
                f"has leading or trailing whitespace ({value!r}); it is half of the "
                f"ledger row's key and is compared exactly, so it is refused rather "
                f"than trimmed"
            )
        return value


class EvidenceError(ValueError):
    """The document does not satisfy the model. The message names each field path."""


#: Why the key fields exist, appended to their refusal so the writer learns the
#: reason, not just the rule.
_HINTS = {
    "repo": "repo + pr ARE the ledger row's key; a closed row cannot be amended, so a "
    "document naming the wrong row would be permanent",
    "pr": "repo + pr ARE the ledger row's key; a closed row cannot be amended, so a "
    "document naming the wrong row would be permanent",
    "merge_commit": "it names WHICH artifact was checked — deploy.method alone cannot",
    "controls": "each entry names an arm that had to come out the other way, and did",
    "scope_limits": "each entry names a gap this install could not reach",
}


def _path(loc: tuple[Any, ...]) -> str:
    out = "evidence"
    for part in loc:
        out += f"[{part}]" if isinstance(part, int) else f".{part}"
    return out


def parse_evidence(obj: object) -> EvidenceDocument:
    """Validate *obj* strictly, or raise :class:`EvidenceError` naming every bad field."""
    try:
        return EvidenceDocument.model_validate(obj)
    except ValidationError as exc:
        lines = []
        for err in exc.errors():
            loc = tuple(err.get("loc", ()))
            msg = str(err.get("msg", "invalid")).removeprefix("Value error, ")
            hint = _HINTS.get(str(loc[0])) if loc else None
            lines.append(f"{_path(loc)}: {msg}" + (f" — {hint}" if hint else ""))
        raise EvidenceError("\n".join(lines)) from None


@dataclass(frozen=True)
class Derivation:
    """What the document itself establishes, before any caller judgment."""

    verdict: str
    #: Named gaps — the declared ``scope_limits`` plus the ones the document implies.
    gaps: tuple[str, ...]
    #: Claim texts that FAILED at an ESTABLISHING tier (non-empty exactly when
    #: ``verdict == "fail-intent"``). An INFERRED failure is a gap, not this.
    failed: tuple[str, ...]


def derive(doc: EvidenceDocument) -> Derivation:
    """The verdict the document supports. Total: every valid document gets exactly one.

    ::

        deploy.established is false                   -> cannot-verify          (open)
        a claim failed at MEASURED or READ            -> fail-intent            (open)
        a claim failed at INFERRED                    -> cannot-verify          (open)
        no claim passing at MEASURED or READ          -> cannot-verify          (open)
        no gaps                                       -> pass-mechanical        (closes)
        otherwise                                     -> pass-with-measured-gaps (closes)

    Deployment is checked FIRST: nothing measured on a tree not shown to carry the
    merge can discharge it or accuse it, so an unestablished deployment derives
    ``cannot-verify`` with :data:`NOT_DEPLOYED` named, and each failed claim is kept
    as an :data:`UNDEPLOYED_FAILURE` gap rather than dropped.

    A failure is gated by tier exactly as a pass is. ``fail-intent`` goes to the user
    as a conversation, so it needs an ESTABLISHED failure; one that was only inferred
    becomes a named :data:`SUSPECTED_FAILURE` gap and keeps the row open — never a
    closure, and never reported as measured.

    ``gaps`` is DERIVED as well as declared: the declared ``scope_limits``, plus
    :data:`NO_CONTROL_GAP` when there are no controls, plus every claim at
    ``NOT_VERIFIABLE_HERE`` or ``INFERRED`` (an inferred FAILURE as its
    :data:`SUSPECTED_FAILURE` gap, listed once). Deriving them is what makes the tree
    total — a document that established something but named no gap and ran no
    control is not "incomplete with nothing to say", it is a pass with one measured
    gap, and now it says so. ``pass-mechanical`` therefore means established,
    controlled, and nothing left out, by construction.
    """
    if not doc.deploy.established:
        undeployed = [
            f"{UNDEPLOYED_FAILURE} ({c.tier}): {c.claim}" for c in doc.claims if c.verdict == "fail"
        ]
        gaps = [f"{NOT_DEPLOYED} ({doc.deploy.method}): {doc.deploy.detail}", *undeployed]
        gaps += list(doc.scope_limits)
        return Derivation("cannot-verify", gaps=tuple(gaps), failed=())

    failed = tuple(
        c.claim for c in doc.claims if c.verdict == "fail" and c.tier in ESTABLISHING_TIERS
    )
    if failed:
        return Derivation("fail-intent", gaps=(), failed=failed)

    suspected = [
        f"{SUSPECTED_FAILURE}: {c.claim}"
        for c in doc.claims
        if c.verdict == "fail" and c.tier == "INFERRED"
    ]
    gaps: list[str] = suspected + list(doc.scope_limits)
    if not doc.controls:
        gaps.append(NO_CONTROL_GAP)
    gaps += [
        f"NOT VERIFIABLE HERE: {c.claim}" for c in doc.claims if c.tier == "NOT_VERIFIABLE_HERE"
    ]
    gaps += [
        f"INFERRED: {c.claim}" for c in doc.claims if c.tier == "INFERRED" and c.verdict == "pass"
    ]

    established = any(c.verdict == "pass" and c.tier in ESTABLISHING_TIERS for c in doc.claims)
    if suspected or not established:
        return Derivation("cannot-verify", gaps=tuple(gaps), failed=())
    if not gaps:
        return Derivation("pass-mechanical", gaps=(), failed=())
    return Derivation("pass-with-measured-gaps", gaps=tuple(gaps), failed=())


@dataclass(frozen=True)
class Decision:
    """What gets written: the outcome verdict, and the derivation behind it."""

    verdict: str
    derivation: Derivation
    parked: bool

    @property
    def closing(self) -> bool:
        return self.verdict in PASS_VERDICTS


@dataclass(frozen=True)
class Refusal:
    """Why nothing gets written. ``code`` is the CLI's exit code (2 malformed, 3 policy)."""

    code: int
    message: str


def _listing(label: str, items: tuple[str, ...]) -> str:
    return "".join(f"\n  {label}: {item}" for item in items)


def decide(
    doc: EvidenceDocument,
    *,
    park: bool = False,
    asserted: str | None = None,
    note: str | None = None,
) -> Decision | Refusal:
    """Combine the derivation with the caller's inputs. Returns a Decision or a Refusal.

    The caller supplies judgment in exactly one place — ``park`` — and everything
    else is either derived or a consistency check:

    * ``park`` turns a derived ``pass-with-measured-gaps`` into ``cannot-verify``:
      something was established, but nothing MATERIAL was. Refused on any other
      derivation — ``pass-mechanical`` has no gaps to park on, ``cannot-verify`` is
      already parked, and a FAILURE may never be parked: it goes to the owner as a
      conversation, never quietly into the backlog (standing ruling).
    * ``asserted`` is an optional expectation, checked for EQUALITY with the outcome.
      One comparison, so it cannot contradict anything. Kept because a validator who
      states what they expect catches a document that does not say what they think.
      Asserting anything but ``fail-intent`` on a document with an ESTABLISHED
      failing claim is exit 3 (policy); every other mismatch is exit 2, and the one
      a caller's judgment can resolve (``cannot-verify`` on a measured-gaps document)
      names ``--park``.
    * ``note`` is required exactly when the outcome leaves the row open — it is the
      whole value of an open row — and refused on a closing outcome, where nothing
      would record it.
    """
    if asserted is not None and asserted not in VERDICTS:
        return Refusal(2, f"--verdict must be one of {list(VERDICTS)}, got {asserted!r}")

    d = derive(doc)
    verdict = d.verdict

    if park:
        if d.verdict == "fail-intent":
            return Refusal(
                3,
                "--park refused: a claim FAILED, and a failure is never parked — the "
                "verdict is fail-intent, which goes to the user as a conversation "
                "(never an automatic rollback) and gets the defect filed."
                + _listing("FAILED", d.failed),
            )
        if d.verdict != "pass-with-measured-gaps":
            why = {
                "pass-mechanical": "it has no gaps to park on — it is a clean pass",
                "cannot-verify": "it already derives cannot-verify; --park adds nothing",
            }[d.verdict]
            return Refusal(2, f"--park refused: this document derives {d.verdict}, and {why}.")
        verdict = "cannot-verify"

    if asserted is not None and asserted != verdict:
        # A FAILED claim under any other verdict is a POLICY refusal (3), not a
        # malformed request (2): the failure is real and the verdict is trying to
        # route around it — into a closure, or into a quiet parked row.
        code = 3 if d.verdict == "fail-intent" else 2
        detail = _listing("FAILED", d.failed) or _listing("gap", d.gaps)
        parked = " (after --park)" if park else ""
        if asserted == "cannot-verify" and verdict == "pass-with-measured-gaps":
            # The one mismatch the caller's own judgment can legitimately resolve.
            advice = (
                " If nothing MATERIAL was established, that is what --park records: "
                "re-run with --park and a --note saying why."
            )
        elif any(g.startswith(NOT_DEPLOYED) for g in d.gaps):
            # The tempting wrong fix is flipping `established`; name the right one.
            advice = (
                " Deployment was not established, so no claim here can discharge or "
                "accuse the merge: deploy it and re-measure. Never set established to "
                "reach a verdict."
            )
        elif asserted == "fail-intent" and any(g.startswith(SUSPECTED_FAILURE) for g in d.gaps):
            # The tempting wrong fix is relabelling the tier; name the right one.
            advice = (
                " An INFERRED failure is a suspicion, not fail-intent: MEASURE it, and "
                "record MEASURED only if the measurement shows it. Never relabel a tier "
                "to reach a verdict."
            )
        else:
            advice = (
                " The verdict follows from the claims, controls and scope limits, so "
                "change the document if it is wrong, not the verdict."
            )
        return Refusal(
            code,
            f"--verdict {asserted} does not match: this document derives "
            f"{verdict}{parked}.{advice}" + detail,
        )

    closing = verdict in PASS_VERDICTS
    note_text = (note or "").strip()
    outcome = (
        "the outcome is cannot-verify (after --park)"
        if park
        else f"this document derives {verdict}"
    )
    suspected = tuple(g for g in d.gaps if g.startswith(SUSPECTED_FAILURE))
    undeployed = tuple(g for g in d.gaps if g.startswith((NOT_DEPLOYED, UNDEPLOYED_FAILURE)))
    if not closing and not note_text:
        if verdict == "fail-intent":
            why = (
                "record what failed and what the user needs to know — this row is the "
                "record the conversation with them starts from."
            )
        elif undeployed and doc.deploy.method == "ancestry":
            why = (
                "ancestry is never a negative verdict — a stacked PR lands inside its "
                "parent's squash, unreachable by ancestry while fully deployed. Check "
                "by content or behaviour first; record this only if those fail too."
                + _listing("gap", undeployed)
            )
        elif undeployed:
            why = (
                "say what stands between this tree and the merge. If you can deploy it "
                "and re-run, that is not cannot-verify — it is not-yet-done: leave the "
                "row alone." + _listing("gap", undeployed)
            )
        elif suspected:
            why = (
                "say what would settle the suspicion — how to MEASURE it. The row "
                "stays open with the suspicion named ahead of your note."
                + _listing("gap", suspected)
            )
        else:
            why = (
                "it is what stops the next validator re-deriving why this could not be "
                "finished.\n"
                "  And if you COULD have finished it with the access and time you had, "
                "this is not cannot-verify — it is not-yet-done. Leave the row alone."
            )
        return Refusal(2, f"{outcome}, which leaves the row OPEN, so --note is required: {why}")
    if closing and note_text:
        return Refusal(
            2,
            f"--note is only recorded when the row stays open, and {outcome}, which "
            f"closes it — the note would be silently discarded. Drop --note; if it says "
            f"something the record should keep, add it to the document's findings, "
            f"which does not change the verdict.",
        )
    return Decision(verdict=verdict, derivation=d, parked=park)


def reason_for(doc: EvidenceDocument, decision: Decision) -> str:
    """The generated ``closed_reason``: verdict, tier census, and the gaps or failures.

    Generated, never typed, so the record cannot drift into however each validator
    happens to phrase it — and so a row that did not close mechanical says WHY. Only
    a CLOSING decision gets one: an open row keeps the validator's own ``--note``,
    and its verdict and document are stored beside it.
    """
    tiers = [c.tier for c in doc.claims]
    census = ", ".join(f"{tiers.count(t)} {t}" for t in TIERS if t in tiers)
    out = f"{decision.verdict.upper()} — {len(tiers)} claim(s): {census}"
    d = decision.derivation
    if d.failed:
        out += "; failed: " + "; ".join(d.failed)
    elif d.gaps:
        out += "; gaps: " + "; ".join(d.gaps)
    return out
