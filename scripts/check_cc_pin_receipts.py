#!/usr/bin/env python3
"""CC pin-receipt check — a change that moves the Claude Code pin FORWARD must
carry the two gate receipts where the person merging it can read them.

``origin`` is the public repo, so merging the pin *is* the release. The update
procedure (``docs/reference/cc-compatibility.md`` §Updating) makes two gates
mandatory before that happens: the full changelog read over ``(pinned, target]``,
and the local-first soak of the candidate. Before this check, both were prose. A
pin change could satisfy every reviewable receipt while never having run either.

WHERE THIS RUNS, AND WHY THAT IS THE WHOLE DESIGN
-------------------------------------------------
The authority is the **merge gate** (``scripts/hooks/git_push_guard.py
--check-pr``), not a CI status. Three consequences, each of which removes a
failure mode rather than mitigating one:

  * **The body is read at the moment of merge.** A PR body is mutable after any
    CI run finishes, so a status describing it is a claim about the past. Read it
    when the merge is authorised and there is no window to edit afterwards.
  * **The checker is main's copy.** ``.claude/settings.json`` runs the gate from
    ``$CLAUDE_PROJECT_DIR``, so a PR cannot edit the code that gates it — which
    a workflow running from the PR's own checkout cannot say.
  * **The base is simply ``origin/main``.** At merge time that IS the thing the
    pin is about to land on, so there is no merge-ref to anchor against and no
    ``HEAD^1``/merge-base question to get wrong.

The CI job is ADVISORY and always exits 0 (``--advisory``). It exists to tell an
author early, not to gate. That is deliberate and mirrors ``review-depth-check``,
advisory for the same reason: a blocking status over a MUTABLE input cannot be
cleared by fixing the input, because a completed check run is immutable and the
merge gate treats a stale FAILURE as red on purpose (see PR #1424 — the gate
forces ``--admin``, so letting a later SUCCESS clear an earlier FAILURE would
make re-running a job until green sufficient to merge).

WHAT THIS IS, PRECISELY
-----------------------
It stops **omission, not forgery**. Anyone can type a receipt line that is not
true; nothing here can tell. That limit is deliberate and already settled in this
repo: ``review-depth-check`` is advisory *by design* because "a committed audit
artifact is forgeable and the local hook is editable by the same author, so the
enforcing teeth are the independent reviewer + a required human approval". What
this does do is convert *forgetting* into *consciously writing a false
statement*, which is a different act.

DOWNGRADES ARE EXEMPT
---------------------
A pin that moves BACKWARD needs no soak receipt by construction: it returns to a
version that already ran here. The downgrade path is also the project's
incident-recovery route — it is *why* a managed-settings ``requiredMinimumVersion``
floor was evaluated and rejected, after a real 2.1.90 → 2.1.87 rollback. Putting
a gate between an operator and that rollback would be a regression dressed as
rigor. The exemption is automatic precisely so nobody has to recall a syntax
under incident pressure.

ERROR POLICY — WHAT BLOCKS AND WHAT DOES NOT
--------------------------------------------
  * BLOCK when the pin moved forward and a required receipt is absent.
  * BLOCK when the HEAD pin cannot be READ — unparseable, ambiguous, not valid
    UTF-8, or not ``X.Y.Z``. "I cannot tell what this PR pins" is not a reason to
    wave a release through; it is the state a human must look at.
  * BLOCK when the head pin is not CANONICAL semver (leading zeros) **and this PR
    wrote it** — i.e. it differs from the base's. ``npm install
    @anthropic-ai/claude-code@2.1.0218`` does not resolve, so an authored pin in
    that form ships a version nothing can install, in any direction: forward,
    backward, unknown, or a respelling of the same number. NOT applied when the
    head pin is identical to the base's. An INHERITED malformed pin is not this
    PR's to fix, and refusing it would make that pin block every open PR including
    its own repair — the wedge above, arriving by a different door.
  * **REQUIRE RECEIPTS — do not block outright — when the BASE pin cannot be read.**
    Direction is then unknowable, and the two available answers are both wrong:
    blocking wedges the repository (a base-side fault is inherited by every open PR
    and repairable by none of them through a gate with no override sigil), while
    passing lets a PR that repairs the base and bundles a forward release ship
    unreceipted. CI does not cover that second case — the merge tree carries the
    REPAIRED file, so lockstep passes and the check is green. So the gate asks for
    the attestation in place of the comparison, and marks the verdict
    ``direction_verified=False`` so a caller cannot mistake it for a real PASS.
    Receipts are a line in the PR body, so this refuses a merge, never the
    repository's ability to repair itself.
  * **That base-side rule splits in two, and the split decides the verdict.**
    CONTENT — git ran and what came back is not a readable pin (absent, empty,
    assigned twice, not valid UTF-8) — is a fact about the TREE, and takes the
    require-receipts path above. PLUMBING — the read ITSELF failed, git would not
    run, the contents API timed out — is this gate failing, not either branch, and
    passes NON-BLOCKING (``base_unreadable``); blocking there would wall off every
    merge in the repository on one transient hiccup, through a gate with no
    override sigil. Collapsing the two is a live fail-open: a non-UTF-8 base read
    as plumbing would hand a free pass to a pin nobody can decode.
  * PASS when the pin is unchanged, respelled to the same version, or moves
    backward.

SCOPE OF THAT POLICY — it governs TEXT THIS MODULE RECEIVES, not whether it
receives any. ``evaluate()`` is handed the head's content and either the base's
content or ``None``; everything above is about what those say. Whether the file
changed AT ALL is decided upstream by the merge gate, from the blob SHA — content
cannot distinguish "identical" from "both unavailable", and asking it to do so is
what let two different oversized blobs read as an untouched pin.

Note it compares the PARSED PIN VALUE, not whether the file changed:
``scripts/lib/cc_version.sh`` is edited for many reasons that leave the pin alone
(suppression helpers, probe dirs, shadow scan), and those changes must not be
asked for soak receipts.

Usage:
    python scripts/check_cc_pin_receipts.py --base-sha X --body-file B
    python scripts/check_cc_pin_receipts.py --advisory ...   # never exits non-zero
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "ci"))

from cc_pin_parse import parse_cc_version  # noqa: E402  (path seam above)

_PIN_PATH = "scripts/lib/cc_version.sh"

#: The receipt trailers, and the canonical example rendered for each. The example
#: is the SINGLE source for both the help text and the placeholder set below, so
#: the two cannot drift — a placeholder that stops appearing in the example stops
#: being rejected, automatically and in the same edit.
_RECEIPTS = {
    "CC-Gate-Changelog": (
        "the full changelog read over (pinned, target], per §Updating",
        "read (<from>, <to>] in full from <source>, <date>",
    ),
    "CC-Gate-Soak": (
        "the local-first soak: candidate, interval, running-binary sweep, sign-off",
        "<candidate> on <where> <start>..<end>, running-binary sweep <result>, signed off <who>",
    ),
}

#: Derived, never hand-listed: exactly the tokens the shipped example contains.
#: A SHAPE heuristic (any ``<...>``) was rejected — it rejects ``<b>bold</b>``,
#: and markdown in a PR body is routine, while still admitting ``<2026-08-25>``
#: because that one does not start with a letter. Matching the real template
#: tokens has neither failure.
_PLACEHOLDERS = frozenset(
    tok.lower() for _, example in _RECEIPTS.values() for tok in re.findall(r"<[^<>]+>", example)
)

#: Rendered out of the PR body before any marker is looked for. A receipt inside
#: an HTML comment satisfies a line-anchored regex while being INVISIBLE in the
#: rendered PR — and this repo's own PULL_REQUEST_TEMPLATE.md is built entirely
#: from ``<!-- -->`` blocks, so that is the likely accident, not an exotic one.
#: A fenced block is the other case: it is how the format gets DOCUMENTED, so
#: text inside one is a description of a receipt, never a receipt.
#: A line scanner, not a pair of regexes. Two reasons, both measured:
#:
#:  * A non-greedy ``<!--.*?-->`` rescans the remainder for every opener that
#:    never closes. At GitHub's 65_536-byte body cap that is ~14s of CPU for a
#:    body any contributor can write — fine to ignore in an advisory CI job,
#:    fatal on the merge gate, which runs under a wall-clock and whose remaining
#:    checks are skipped when it is killed.
#:  * Both regexes require a CLOSER, so an UNTERMINATED opener was left intact —
#:    exactly inverting the rule. A body ending ``<!-- template start`` with the
#:    receipts below it renders them invisible while the check counted them.
#:    Deleting a trailing ``-->`` is how that happens by accident.
#:
#: Walking lines is O(n), and "still open at end of body" is naturally hidden
#: to the end — which is what CommonMark does with an unclosed block too.
_COMMENT_OPEN, _COMMENT_CLOSE = "<!--", "-->"
_FENCE_MARKS = ("```", "~~~")

#: GitHub's PR-body limit. Bound the work regardless of the scanner's O(n).
_MAX_BODY = 65_536

#: Canonical semver only — no leading zeros. Applied to the HEAD pin, and ONLY on
#: the paths where this PR is publishing it: npm cannot install a leading-zero
#: version, so a forward move to one ships a pin that does not resolve.
#:
#: NOT applied when the pin is unchanged or moves backward. Refusing there makes a
#: malformed pin already on the base branch block every PR — including the one that
#: would repair it — which is the wedge this module exists to avoid, arriving by a
#: different door. Whether the pin is well-formed is only this PR's business when
#: this PR is the one shipping it.
_STRICT_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")

#: Any ``X.Y.Z`` of digits, leading zeros included — the pin's numeric VALUE rather
#: than its spelling. This is what DIRECTION is computed from, so that a base pin
#: nobody can respell (``2.1.0218``) still yields the comparison "2.1.218 is the
#: same version", instead of an unanswerable question that blocks the repair.
_NUMERIC_SEMVER = re.compile(r"^([0-9]+)\.([0-9]+)\.([0-9]+)$")

#: Below this many alphanumerics a value is not a receipt, it is a keystroke.
#: Kills ".", "-", "n/a" and a lone zero-width space. NOT a truthfulness check —
#: that is explicitly out of scope; this only rejects the degenerate.
_MIN_SUBSTANCE = 3


class PinUnreadable(Exception):
    """The pin could not be read at one side — block, do not guess."""


@dataclass(frozen=True)
class Verdict:
    """Outcome of a pin-receipt evaluation. ``blocked`` is the only gate input.

    ONE channel, deliberately. An earlier shape returned this dataclass for the
    passing cases and RAISED for the blocking one — so ``blocked`` was
    structurally incapable of being ``True``, while its own docstring invited a
    caller to write ``if evaluate(...).blocked:``. That call site would have
    been a permanently open gate whose tests all pass. If a field is the gate
    input, every outcome has to travel through it.
    """

    blocked: bool
    message: str
    #: Which markers were absent — empty for every non-receipt outcome.
    missing: tuple[str, ...] = ()
    #: "ok" | "receipts" | "unreadable-head". Lets a caller distinguish "the pin
    #: moved without receipts" from "I could not read the pin at all", which are
    #: different conversations with the operator.
    #:
    #: The base branch has no reason of its own. It is not a party to the decision
    #: — it supplies a reference point, and when it cannot, that shows up as
    #: ``direction_verified=False`` rather than as an outcome. Reasons naming the
    #: base (``unreadable-base``, ``incomparable``, ``unchanged-unreadable``) existed
    #: until 2026-08-29 and were removed with the branching that produced them.
    reason: str = "ok"
    #: Whether the base branch actually yielded a version to compare against. False
    #: means the direction of this change is UNKNOWN — receipts were required in
    #: place of a comparison, so a non-blocking verdict here has verified the
    #: attestation and nothing else. Callers surface it; they must not treat a pass
    #: on this path as "the pin did not move forward".
    direction_verified: bool = True


def _strip_formatting_chars(value: str) -> str:
    """Drop Unicode format characters (category Cf) — zero-width space, joiners,
    bidi controls. Python's ``\\S`` treats every one of them as non-whitespace, so
    a body of ``CC-Gate-Soak: \\u200b`` satisfied a "has a non-space value" test
    while rendering as an empty line."""
    return "".join(ch for ch in value if unicodedata.category(ch) != "Cf")


def _outside_comments(line: str, in_comment: bool) -> tuple[str, bool]:
    """``(text outside HTML comments, still-open flag)`` for ONE line.

    Scans the whole line rather than partitioning once, because a line may carry
    several comments and may have real text after the last of them. Handling only
    the first opener — or discarding a line the moment a closer appeared — is what
    made a receipt written beside a template comment invisible.

    An UNTERMINATED opener hides the rest of the line and stays open into the next,
    which is what CommonMark does with an unclosed block and what the line scanner
    was introduced for: a body ending ``<!-- template start`` must not have the
    receipts below it counted.
    """
    out: list[str] = []
    i, n = 0, len(line)
    while i < n:
        if in_comment:
            close = line.find(_COMMENT_CLOSE, i)
            if close == -1:
                return "".join(out), True
            i = close + len(_COMMENT_CLOSE)
            in_comment = False
            continue
        opener = line.find(_COMMENT_OPEN, i)
        if opener == -1:
            out.append(line[i:])
            return "".join(out), False
        out.append(line[i:opener])
        i = opener + len(_COMMENT_OPEN)
        in_comment = True
    return "".join(out), in_comment


def readable_body(body: str) -> str:
    """The part of a PR body a human actually reads.

    Removes HTML comments and fenced code blocks. Both hide text from the
    rendered view (a comment) or mark it as documentation rather than assertion
    (a fence), and a receipt the reviewer cannot see defeats the only enforcement
    this check has — a human reading a claim someone chose to make.
    """
    visible: list[str] = []
    in_comment = False
    fence: str | None = None

    for line in body[:_MAX_BODY].splitlines():
        # Strip comments FIRST, and keep whatever the line has outside them. The
        # previous version dropped the entire remainder of a line once it saw a
        # comment — both when the comment opened on that line and when it closed
        # there — so `<!-- what did you run? --> CC-Gate-Soak: …` was invisible to
        # the check while rendering perfectly on GitHub. This repo's own
        # PULL_REQUEST_TEMPLATE.md is built from `<!-- -->` blocks, so that is the
        # shape an author naturally produces, and this gate has no override: it
        # refused a compliant PR and told the author the receipts were missing
        # while they were plainly there. MEASURED before the fix — both receipts
        # reported absent, with the identical text on its own lines accepted.
        rendered, in_comment = _outside_comments(line, in_comment)
        stripped = rendered.strip()

        if fence is not None:
            # A fence is closed only by its OWN marker: ``~~~`` does not end a
            # ``` block, which is how a receipt below a mismatched closer stayed
            # rendered-as-code while counting as visible.
            if stripped.startswith(fence):
                fence = None
            continue

        if stripped.startswith(_FENCE_MARKS):
            fence = stripped[:3]
            continue

        if stripped:
            visible.append(rendered)

    return "\n".join(visible)


#: Words that state an omission rather than a receipt. These read as compliance
#: to a presence check while saying the gate did NOT run — which is the exact
#: thing this exists to convert into a deliberate false statement.
_REFUSAL_WORDS = frozenset({"todo", "tbd", "pending", "none", "no", "yes", "na"})


def _value_is_real(value: str) -> bool:
    cleaned = _strip_formatting_chars(value).strip()

    # Normalise `< from >` to `<from>` before the membership test: whitespace
    # inside the brackets otherwise walks a pasted template straight past it,
    # and innocent reformatting produces the same shape.
    normalised = re.sub(r"<\s*([^<>]+?)\s*>", r"<\1>", cleaned.lower())
    if any(p in normalised for p in _PLACEHOLDERS):
        return False

    if normalised.strip(" .-") in _REFUSAL_WORDS:
        return False

    return sum(ch.isalnum() for ch in cleaned) >= _MIN_SUBSTANCE


def missing_receipts(body: str) -> list[str]:
    """Receipt markers that are absent, valueless, degenerate, or still template.

    Every occurrence of a marker is considered, not the first: a leftover
    template line ABOVE a filled one must not veto the filled one.
    """
    visible = readable_body(body)
    missing = []
    for marker in _RECEIPTS:
        # `[^\S\n]` is horizontal whitespace ONLY. Plain `\s*` matches newlines,
        # so "CC-Gate-Changelog:\nCC-Gate-Soak: 2.1.246 ..." let the EMPTY
        # changelog trailer borrow the soak line as its value, and both markers
        # came back satisfied by one real receipt.
        # Tolerate the markdown a PR body is ACTUALLY written in. The strict
        # line-start form rejected every one of these fully-filled receipts:
        #   - CC-Gate-Soak: …      * CC-Gate-Soak: …      > CC-Gate-Soak: …
        #   - [x] CC-Gate-Soak: …  **CC-Gate-Soak**: …    `CC-Gate-Soak`: …
        # The repo's own PULL_REQUEST_TEMPLATE.md ends in a `- [ ]` checklist
        # under "## Testing", which is precisely where an author will put a soak
        # receipt. Blocking a compliant PR at merge time, on a public release, is
        # how an operator learns to route around the gate.
        pattern = re.compile(
            rf"^[^\S\n]*(?:[-*+>][^\S\n]*)*(?:\[[ xX]\][^\S\n]*)?"
            rf"[*_`]{{0,2}}{re.escape(marker)}[*_`]{{0,2}}"
            rf"[^\S\n]*:[^\S\n]*(\S[^\n]*)$",
            re.MULTILINE | re.IGNORECASE,
        )
        if not any(_value_is_real(m) for m in pattern.findall(visible)):
            missing.append(marker)
    return missing


def _numeric_value(version: str) -> tuple[int, ...] | None:
    """The pin's numeric value, or ``None`` if it is not ``X.Y.Z`` at all.

    Deliberately tolerant of leading zeros. DIRECTION is a question about versions,
    not about spelling, and ``2.1.0218`` names the same release as ``2.1.218``. An
    earlier revision refused to compare either side unless both were canonical,
    which made a single malformed pin on the base branch unmergeable-by-anyone —
    the canonical repair included, since it is itself a pin-moving change. The
    spelling is still enforced, by ``_refuse_unpublishable`` below, at the one
    point where it can do harm: a pin this PR moves FORWARD.
    """
    match = _NUMERIC_SEMVER.match(version)
    return tuple(int(part) for part in match.groups()) if match else None


def _refuse_unpublishable(head_pin: str) -> Verdict | None:
    """A Verdict when the head pin must not be PUBLISHED, else ``None``.

    Called only from the paths that ship the head pin — a forward move, and a move
    whose direction could not be established. ``npm install @2.1.0218`` does not
    resolve, so merging that pin breaks every install path that reads it, and this
    gate is the last thing standing between such a pin and the public repo.
    """
    if _STRICT_SEMVER.match(head_pin):
        return None
    return Verdict(
        True,
        f"CC pin {head_pin!r} is not canonical semver (leading zeros, or not X.Y.Z). "
        "This PR publishes that pin, and npm cannot install it — `npm install "
        "@anthropic-ai/claude-code@2.1.0218` does not resolve. Write it as X.Y.Z "
        "with no leading zeros.",
        reason="unreadable-head",
    )


def _pin_of(text: str, *, where: str) -> str:
    pin = parse_cc_version(text)
    if pin is None:
        raise PinUnreadable(
            f"could not determine CC_VERSION from {_PIN_PATH} at {where} — the "
            "pin is absent, or assigned more than once so the file's effective "
            "value is not statable. A pin nobody can read must not ship."
        )
    return pin


def evaluate(
    *,
    base_pin_text: str | None,
    head_pin_text: str,
    body: str,
    base_unreadable: bool = False,
) -> Verdict:
    """The whole decision, as a pure function.

    Takes the two file CONTENTS rather than reading them, so the CI adapter can
    supply them from git and the merge gate from the GitHub API, and so the
    behaviour is testable without a repository at all. ``base_pin_text=None`` means
    the base pin could not be read AT ALL — see the third question below.

    NEVER raises for a policy outcome — every decision, pass or block, comes back
    as a Verdict. A caller that reads ``.blocked`` gets the whole truth, and one
    that forgets a ``try`` does not get a silent pass.

    TWO ORDERED QUESTIONS, and the order is the design
    --------------------------------------------------
    This replaced (2026-08-29) a cascade of early returns over the product of
    (head state × base state × parse outcome). Four separate cells of that product
    were found to return the wrong answer, in four review rounds, by four different
    arguments — which is the signature of a shape that generates bugs rather than
    of four bugs. Two of them let an unreceipted forward bump merge; one blocked
    every possible repair of a malformed base. The cure is that the base branch no
    longer has any early return of its own:

      1. **Is the head pin readable?** Answered with no reference to the base
         whatsoever. Unreadable ⇒ BLOCK. Previously a base-side branch could return
         BEFORE this question was asked, so a PR that introduced an empty pin file
         over an absent base merged with no usable pin at the head.

      2. **Which direction does it move?** The base supplies a reference point, and
         when it cannot supply one, that is not an answer — it is a missing input.
         Receipts are then required IN PLACE of the comparison, and the verdict is
         marked ``direction_verified=False``. Previously an unreadable base returned
         a non-blocking verdict directly, so a PR that repaired the base and bundled
         a forward release in the same change skipped the receipts entirely. CI does
         not cover that case: the merge tree carries the REPAIRED file, so lockstep
         passes and the check is green.

    WHAT IS NOT ASKED HERE. "Did this PR touch the pin file?" — the caller answers
    it, from the blob SHA, before calling. This module sees CONTENT, and content
    cannot distinguish "identical" from "both unavailable".
    """
    # ── 1. IS THE HEAD PIN READABLE? ──
    try:
        head_pin = _pin_of(head_pin_text, where="the proposed change")
    except PinUnreadable as exc:
        return Verdict(True, str(exc), reason="unreadable-head")

    head_value = _numeric_value(head_pin)
    if head_value is None:
        return Verdict(
            True,
            f"CC pin {head_pin!r} at the head is not a version (expected X.Y.Z), so "
            "whether this PR moves the pin forward cannot be established.",
            reason="unreadable-head",
        )

    # A base we could not READ AT ALL is this gate's own plumbing failing, not a fact
    # about any branch — a contents-API timeout, a non-JSON body, an unresolvable ref.
    # It must stay non-blocking, or one transient hiccup walls off every merge in the
    # repository through a gate with no override sigil. Distinct from a base whose
    # content is FAULTY (absent, empty, unassigned, doubly-assigned, undecodable),
    # which is a real statement about the base branch and does require the receipts.
    #
    # Collapsing those two into "base_pin_text is None" is what this parameter exists
    # to prevent: the CONTENT-vs-PLUMBING split is the gate's other axis, and the
    # revision before this one flattened it on the base side while preserving it on
    # the head side.
    #
    # Placed AFTER the head check, never before it. A base-side branch that returns
    # ahead of head validation is the exact defect this module was restructured to
    # remove, and re-adding one above would reintroduce it for this cell.
    # `and base_pin_text is None` so the two parameters cannot contradict each other.
    # If a caller ever supplies BOTH a flag saying "unreadable" and readable content,
    # the CONTENT wins — it is the stronger evidence, and honouring the flag over it
    # would discard a usable base and hand out a free pass on the say-so of a boolean.
    if base_unreadable and base_pin_text is None:
        # One head-side CONTENT fact still applies with no base at all: a pin that
        # cannot be INSTALLED. `npm install @…@2.1.0218` does not resolve, so
        # merging that spelling publishes a version nothing can fetch — true
        # whether this PR wrote it or inherited it, which is the one part of the
        # spelling rule that does not need to know the author.
        #
        # Everywhere else the rule IS authorship-scoped, because refusing an
        # inherited malformed pin would wedge every open PR. That scoping does not
        # rescue this path: a transport failure is transient, so the cost of
        # refusing here is one retry, while the cost of allowing is a published pin
        # that cannot be installed. An earlier revision let it through and recorded
        # that as an accepted consequence; on review the trade was the wrong way
        # round.
        if unpublishable := _refuse_unpublishable(head_pin):
            return unpublishable
        return Verdict(
            False,
            f"The pin file could not be READ on the base branch — a transport failure "
            f"in this check, not a fault in either branch. This PR pins {head_pin}, but "
            f"the direction of the change could not be established.",
            direction_verified=False,
        )

    # ── 2. WHICH DIRECTION? ──
    base_pin = None
    if base_pin_text is not None:
        try:
            base_pin = _pin_of(base_pin_text, where="the base branch")
        except PinUnreadable:
            base_pin = None  # a missing input, NOT a verdict — see below

    # THE PIN DID NOT MOVE. Asked before anything judges the base's spelling, and
    # answered on the pin STRING, so a file edited for one of its many other reasons
    # (the aligner, the probe dirs, the shadow scan) is never asked for release
    # receipts — which is this module's stated contract. It also keeps an INHERITED
    # non-canonical pin from becoming every PR's problem via the rule below.
    if base_pin is not None and head_pin == base_pin:
        return Verdict(False, f"CC pin unchanged ({head_pin}) — no receipts required.")

    # THE BASE MUST BE INSTALLABLE TO SERVE AS A REFERENCE POINT.
    # `_numeric_value` is deliberately lenient, which is what lets `2.1.0246` and
    # `2.1.246` be recognised as one version — but leniency about SPELLING became
    # leniency about TRUST: a non-canonical base was accepted as "the version that
    # already ran here", and both the unchanged and the BACKWARD exemptions rest on
    # exactly that claim. MEASURED: `2.1.0250` → `2.1.246` passed with an empty body
    # as a rollback, though `npm install @…@2.1.0250` does not resolve, so that
    # version never ran anywhere and there is nothing to roll back TO.
    #
    # So a non-canonical base yields NO reference value and takes the unknown-
    # direction path below: receipts required, in place of a comparison that cannot
    # be trusted. The canonical repair stays mergeable — it just has to be attested,
    # which is the same bar every other unverifiable direction meets. Blocking it
    # outright is the wedge this module exists to remove; exempting it silently is
    # the hole this fixes.
    base_value = _numeric_value(base_pin) if base_pin and _STRICT_SEMVER.match(base_pin) else None

    # The head pin's SPELLING is this PR's responsibility exactly when this PR WROTE
    # it — that is, whenever it differs from the base's. An inherited non-canonical
    # pin is not this PR's to fix, and refusing it would wedge every open PR; an
    # authored one is, and `npm install @…@2.1.0218` does not resolve.
    #
    # Placed here, ahead of the direction branches, because it applies to ALL of them.
    # Checking it only on the forward path let a PR REPLACE a good `2.1.246` with
    # `2.1.0246` and pass as "the same version, respelled" — the F4 repair rule run
    # backwards, shipping the exact pin npm cannot install.
    if base_pin != head_pin and (unpublishable := _refuse_unpublishable(head_pin)):
        return unpublishable

    if base_value is None:
        # Direction is unknowable, so the gate falls back to the attestation. It does
        # NOT fall back to permission: an unreadable base is exactly the state a PR
        # that rewrites the pin file produces, and waving those through is how a
        # release ships unreceipted. Receipts are in-band — a line in the PR body —
        # so this refuses a merge, never the repository's ability to repair itself.
        absent = missing_receipts(body)
        if absent:
            return _receipts_verdict(
                absent,
                f"The base branch's pin could not be read, so whether this PR moves the "
                f"CC pin forward cannot be established. Receipts are required in place "
                f"of that comparison. This PR pins {head_pin}, and the body is missing "
                f"{len(absent)} required gate receipt(s):",
                direction_verified=False,
            )
        return Verdict(
            False,
            f"The base branch's pin could not be read, so the direction of this change "
            f"was NOT established — but both gate receipts are present for {head_pin}, "
            f"which is what the comparison would have required. The base-side fault is "
            f"inherited by every open PR and repairable by none of them through this "
            f"gate; CI covers it (the pin file is required to parse).",
            direction_verified=False,
        )

    # Both sides are canonical by the time we get here (the base by the gate above,
    # the head by `_refuse_unpublishable`), and two canonical spellings of one version
    # are the same string — so equality here means the identical pin, which the
    # unchanged check has already returned on. Kept as a guard rather than dropped:
    # it costs nothing and it states the invariant for anyone who loosens either rule.
    if head_value == base_value:
        return Verdict(False, f"CC pin unchanged ({head_pin}) — no receipts required.")

    if head_value < base_value:
        return Verdict(
            False,
            f"CC pin moves BACKWARD ({base_pin} → {head_pin}) — exempt. A rollback "
            "returns to a version that already ran here, and the downgrade path is "
            "the project's incident-recovery route.",
        )

    # An empty body is NOT indeterminate. The absence of both receipts is fully
    # determined by it, so there is nothing to be graceful about.
    absent = missing_receipts(body)
    if not absent:
        return Verdict(
            False,
            f"CC pin moves forward ({base_pin} → {head_pin}) and both gate receipts are present.",
        )
    return _receipts_verdict(
        absent,
        f"CC pin moves FORWARD ({base_pin} → {head_pin}) but the PR body is missing "
        f"{len(absent)} required gate receipt(s):",
    )


def _receipts_verdict(
    absent: list[str], headline: str, *, direction_verified: bool = True
) -> Verdict:
    """The blocking receipts message, rendered once for both paths that require them.

    Shared so the forward-move case and the unknown-direction case cannot drift in
    what they ask the author to DO — the remediation is identical, and only the
    reason for asking differs.
    """
    lines = [headline, ""]
    for marker in absent:
        why, example = _RECEIPTS[marker]
        lines.append(f"  {marker}: — {why}")
        lines.append(f"      e.g.  {marker}: {example}")
    lines += [
        "",
        "Merging this pin publishes it: `origin` is the public repo, and the host",
        "follows via `update-cc`. Both gates are mandatory before that",
        "(docs/reference/cc-compatibility.md §Updating).",
        "",
        "Put the receipts in the PR body itself — not inside an HTML comment or a",
        "code fence, which hide them from the person merging.",
        "",
        "This checks the receipts are PRESENT; it cannot check they are true. If a",
        "gate genuinely was not run, run it — do not write the line.",
    ]
    return Verdict(
        True,
        "\n".join(lines),
        tuple(absent),
        reason="receipts",
        direction_verified=direction_verified,
    )


# ── adapters ──────────────────────────────────────────────────────────────


class PinTransportError(PinUnreadable):
    """The read ITSELF failed — git would not run at all.

    Distinct from ``PinUnreadable``, which means we read something and could not
    make a pin out of it. The two take opposite fail directions, and a caller that
    cannot tell them apart will classify the same repository state differently from
    the merge gate — which is exactly what happened: ``main()`` collapsed both into
    "no base text", so a transport failure required receipts through the CLI while
    the gate waved it through.
    """


def read_pin_at(ref: str, *, repo_root: Path) -> str:
    """The pin file's contents as of ``ref``.

    Raises ``PinTransportError`` when git could not be run at all, and plain
    ``PinUnreadable`` when git ran and refused — a bad ref, or no such path at that
    ref. The second is a fact about the tree; only the first is our plumbing.
    """
    try:
        out = subprocess.run(
            ["git", "show", f"{ref}:{_PIN_PATH}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PinTransportError(f"cannot run git: {exc}") from exc
    except UnicodeDecodeError as exc:
        # `text=True` decodes INSIDE subprocess.run, and UnicodeDecodeError is a
        # ValueError — neither of the types above. Left uncaught it escaped the
        # base-read handler in main() and crashed --advisory with a traceback.
        #
        # CONTENT, not transport: git ran and handed back bytes, and those bytes
        # are not a readable pin — a fact about the TREE. PinTransportError would
        # set base_unreadable=True, which evaluate() reads as this gate's own
        # plumbing failing and passes NON-BLOCKING; a pin nobody can decode would
        # then merge with no receipts. Same call read_pin_head already makes for
        # the identical condition on the head side.
        raise PinUnreadable(f"{_PIN_PATH} at {ref} is not valid UTF-8: {exc}") from exc
    if out.returncode != 0:
        raise PinUnreadable(f"cannot read {_PIN_PATH} at {ref}: {out.stderr.strip()[:200]}")
    return out.stdout


def read_pin_head(repo_root: Path) -> str:
    try:
        return (repo_root / _PIN_PATH).read_text(encoding="utf-8")
    except OSError as exc:
        raise PinUnreadable(f"cannot read {_PIN_PATH}: {exc}") from exc
    except UnicodeDecodeError as exc:
        # NOT an OSError. Left uncaught this reached the top-level handler and
        # became a pass — a forward bump with zero receipts, from one stray byte.
        raise PinUnreadable(f"{_PIN_PATH} is not valid UTF-8: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CC pin-receipt check.")
    parser.add_argument("--base-sha", default=os.environ.get("PR_BASE_SHA", ""))
    parser.add_argument("--body-file", type=Path, default=None)
    parser.add_argument("--repo-root", type=Path, default=_REPO_ROOT)
    parser.add_argument(
        "--advisory",
        action="store_true",
        help="Report, never fail. The CI mode: the merge gate is the authority.",
    )
    args = parser.parse_args(argv)

    if args.body_file is not None:
        try:
            body = args.body_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"cc-pin-receipts: cannot read {args.body_file}: {exc}", file=sys.stderr)
            return 0 if args.advisory else 1
    else:
        body = os.environ.get("PR_BODY", "")

    if not args.base_sha:
        # Advisory: nothing to compare, say so and pass. Enforcing: a missing base
        # is not "nothing to check", it is "I could not check" — on the one event
        # this exists for, that must not read as approval.
        print("cc-pin-receipts: no base SHA to compare against.", file=sys.stderr)
        return 0 if args.advisory else 1

    try:
        head_pin_text = read_pin_head(args.repo_root)
    except PinUnreadable as exc:
        # The head is this change's own file. Unreadable here is a fact about what
        # is being proposed, and it blocks — the same direction `evaluate` takes.
        verdict = Verdict(True, str(exc), reason="unreadable-head")
    else:
        # The SAME split the merge gate makes, made here too. Both are None-valued,
        # and collapsing them meant this adapter and the gate returned opposite
        # verdicts for one repository state: a transport failure required receipts
        # through the CLI while the gate treated it as plumbing and passed.
        base_pin_text, base_unreadable = None, False
        try:
            base_pin_text = read_pin_at(args.base_sha, repo_root=args.repo_root)
        except PinTransportError:
            base_unreadable = True  # git would not run — our plumbing, not the tree
        except PinUnreadable:
            # git ran and refused: a bad ref, or no pin at that ref. That is a fact
            # about the tree, so it takes the content path — receipts in place of the
            # comparison, never a block, which would make an unreadable base
            # unmergeable-by-anyone with the repair PR included.
            base_pin_text = None
        verdict = evaluate(
            base_pin_text=base_pin_text,
            head_pin_text=head_pin_text,
            body=body,
            base_unreadable=base_unreadable,
        )

    if not verdict.blocked:
        print(f"cc-pin-receipts: {verdict.message}")
        return 0

    if args.advisory:
        # A stderr line inside a green check is not a signal. An ::warning::
        # annotation is what actually surfaces on the PR — the same mechanism
        # check_review_depth.py uses to be seen while staying advisory.
        print(f"::warning title=CC pin receipts::{verdict.message.splitlines()[0]}")
    print(f"cc-pin-receipts: {verdict.message}", file=sys.stderr)
    return 0 if args.advisory else 1


if __name__ == "__main__":
    sys.exit(main())
