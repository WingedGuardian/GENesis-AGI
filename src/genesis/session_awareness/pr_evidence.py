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

import re
from dataclasses import dataclass
from datetime import date
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
    "EVENT_GATED",
    "GAP_KINDS",
    "MAX_EVIDENCE_BYTES",
    "NOTE_PREFIXED_GAPS",
    "NOT_DEPLOYED",
    "NOT_YET_DONE",
    "NO_CONTROL_GAP",
    "OWNER_ONLY",
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

#: Per-CLAIM verdicts — whether that one claim held. Distinct from the SESSION
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

#: The four kinds a declared gap can be. The kind, not the prose, decides what the
#: gap does: ``not_yet_done`` keeps the row OPEN, and the other three close it with
#: the gap named. (``event_gated`` carries its trigger and re-measuring command so
#: the owed measurement is recorded in a shape a later recheck can act on.)
GAP_KINDS = ("method", "event_gated", "owner_only", "not_yet_done")

#: Rendered-gap prefixes for the kinds that are not plain method limits.
OWNER_ONLY = "OWNER ONLY"
EVENT_GATED = "EVENT-GATED"
NOT_YET_DONE = "NOT YET DONE"

#: Gaps that keep a row open AND must be visible on the backlog's one line, so the
#: CLI writes them ahead of the validator's note rather than only into the document.
#: NOT YET DONE is here so the commands that finish the row lead its note.
NOTE_PREFIXED_GAPS = (NOT_DEPLOYED, UNDEPLOYED_FAILURE, SUSPECTED_FAILURE, NOT_YET_DONE)

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


class MethodGap(BaseModel):
    """A limit of HOW this was verified, accepted as it stands."""

    model_config = _CONFIG

    kind: Literal["method"]
    detail: Text


class OwnerGap(BaseModel):
    """Only the owner can check it: their account, their device, their judgment."""

    model_config = _CONFIG

    kind: Literal["owner_only"]
    detail: Text


#: An event-gated trigger that is a DATE. Anything else is a named event.
TRIGGER_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: A string that LOOKS like a date. Matching it while failing to be a real
#: ``YYYY-MM-DD`` is refused: a mistyped date read as a named event would mean a
#: different thing than the writer wrote, and nothing downstream could tell.
_DATE_LIKE = re.compile(r"^\d{4}-?\d{1,2}-?\d{1,2}")


class EventGatedGap(BaseModel):
    """Measurable only after something happens: a date, or a named event."""

    model_config = _CONFIG

    kind: Literal["event_gated"]
    detail: Text
    #: ``YYYY-MM-DD``, or a named event (a job's next run, the next deploy).
    trigger: Text
    #: One read-only command or SQL that re-measures it once the trigger passes.
    recheck: Text

    @field_validator("trigger")
    @classmethod
    def _a_date_is_a_real_date(cls, value: str) -> str:
        if not _DATE_LIKE.match(value):
            return value
        try:
            real = bool(TRIGGER_DATE.match(value)) and bool(date.fromisoformat(value))
        except ValueError:
            real = False
        if not real:
            raise ValueError(
                f"{value!r} looks like a date but is not a real YYYY-MM-DD; write the "
                f"date exactly, or name the event in words"
            )
        return value


class NotYetDoneGap(BaseModel):
    """Reachable work the validator has not done. Keeps the row OPEN."""

    model_config = _CONFIG

    kind: Literal["not_yet_done"]
    detail: Text
    #: What to run to do it — it leads the row's note on the backlog.
    command: Text


Gap = Annotated[MethodGap | OwnerGap | EventGatedGap | NotYetDoneGap, Field(discriminator="kind")]


class EvidenceDocument(BaseModel):
    """The whole evidence document. ``extra="forbid"``: an unknown key is almost
    always a typo of a real one (``control`` for ``controls``), and dropping it
    silently would lose exactly the field the writer meant to fill. It is also what
    refuses a document still carrying ``scope_limits``, the untyped list ``gaps``
    replaced — see its hint."""

    model_config = _CONFIG

    repo: Repo
    # Upper bound = SQLite's INTEGER range: past it the driver raises OverflowError
    # at write time, a traceback where a named refusal belongs.
    pr: StrictInt = Field(gt=0, le=2**63 - 1)
    merge_commit: Text
    deploy: Deploy
    claims: list[Claim] = Field(min_length=1)
    controls: list[Text] = []
    gaps: list[Gap] = []
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
    "merge_commit": "the PR's OWN merge commit (gh pr view --json mergeCommit), 7+ hex "
    "chars — close checks it; name the deployed artifact in deploy.detail",
    "controls": "each entry names an arm that had to come out the other way, and did",
    "gaps": "each gap is an object whose 'kind' decides what it does: 'method' or "
    "'owner_only' (+detail), and 'event_gated' (+detail, +trigger, +recheck) close the "
    "row with the gap named; 'not_yet_done' (+detail, +command) keeps it OPEN until "
    "you run the command",
    "scope_limits": "replaced by 'gaps', a list of objects each with a 'kind' "
    "(method, event_gated, owner_only, not_yet_done). It is never mapped across: the "
    "kind decides the verdict, so each entry needs its kind chosen by you",
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
    #: Named gaps — the declared ``gaps``, rendered, plus the ones the document implies.
    gaps: tuple[str, ...]
    #: Claim texts that FAILED at an ESTABLISHING tier (non-empty exactly when
    #: ``verdict == "fail-intent"``). An INFERRED failure is a gap, not this.
    failed: tuple[str, ...]


def _render(gap: MethodGap | OwnerGap | EventGatedGap | NotYetDoneGap) -> str:
    if isinstance(gap, NotYetDoneGap):
        # Command first: the backlog clips a note at 120 characters, and the
        # command is the part the next validator needs.
        return f"{NOT_YET_DONE} — run: {gap.command} ({gap.detail})"
    if isinstance(gap, EventGatedGap):
        return f"{EVENT_GATED} ({gap.trigger}): {gap.detail} — recheck: {gap.recheck}"
    if isinstance(gap, OwnerGap):
        return f"{OWNER_ONLY}: {gap.detail}"
    return gap.detail


def derive(doc: EvidenceDocument) -> Derivation:
    """The verdict the document supports. Total: every valid document gets exactly one.

    ::

        deploy.established is false                   -> cannot-verify          (open)
        a claim failed at MEASURED or READ            -> fail-intent            (open)
        a not_yet_done gap                            -> not-yet-done           (open)
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

    ``not-yet-done`` sits after a failure and before every cannot-verify: work the
    validator could still do is not a reason the row cannot be verified, and an
    ESTABLISHED failure outranks it because that goes to the user whatever else is
    owed. Deployment still comes first — a not_yet_done gap on an undeployed tree is
    listed among its gaps, but there the deploy is the next step.

    ``gaps`` is DERIVED as well as declared: the declared ``gaps`` rendered with their
    kind, plus
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
        gaps += [_render(g) for g in doc.gaps]
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
    gaps: list[str] = suspected + [_render(g) for g in doc.gaps]
    if not doc.controls:
        gaps.append(NO_CONTROL_GAP)
    gaps += [
        f"NOT VERIFIABLE HERE: {c.claim}" for c in doc.claims if c.tier == "NOT_VERIFIABLE_HERE"
    ]
    gaps += [
        f"INFERRED: {c.claim}" for c in doc.claims if c.tier == "INFERRED" and c.verdict == "pass"
    ]

    if any(isinstance(g, NotYetDoneGap) for g in doc.gaps):
        return Derivation("not-yet-done", gaps=tuple(gaps), failed=())
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
      already parked, ``not-yet-done`` names work to do rather than a reason it
      cannot be done, and a FAILURE may never be parked: it goes to the owner as a
      conversation, never quietly into the backlog (standing ruling).
    * ``asserted`` is an optional expectation, checked for EQUALITY with the outcome.
      One comparison, so it cannot contradict anything. Kept because a validator who
      states what they expect catches a document that does not say what they think.
      Asserting anything but ``fail-intent`` on a document with an ESTABLISHED
      failing claim is exit 3 (policy); every other mismatch is exit 2, and the one
      a caller's judgment can resolve (``cannot-verify`` on a measured-gaps document)
      names ``--park``.
    * ``note`` is required when the outcome leaves the row open — it is the whole
      value of an open row — and refused on a closing outcome, where nothing would
      record it. ``not-yet-done`` is the exception: its NOT YET DONE gaps already
      say what is left and what to run, so they ARE the note, and a ``note`` given
      anyway is appended after them.
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
                "not-yet-done": "it names work still to do: do the commands, then re-run",
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
        elif d.verdict == "not-yet-done":
            # The tempting wrong fix is relabelling the gap's kind; name the right one.
            advice = (
                " A NOT YET DONE gap is work you can still do: run the listed commands, "
                "then re-measure. Never change a gap's kind to reach a verdict."
            )
        else:
            advice = (
                " The verdict follows from the claims, controls and gaps, so change the "
                "document if it is wrong, not the verdict."
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
    if not closing and not note_text and verdict != "not-yet-done":
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
                "and re-run, that is not cannot-verify — it is not-yet-done work: do it "
                "rather than record this." + _listing("gap", undeployed)
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
                "this is not cannot-verify — it is not-yet-done: do it, or name each "
                "step as a not_yet_done gap with the command that does it."
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
