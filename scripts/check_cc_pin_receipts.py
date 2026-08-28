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
  * BLOCK when the pin cannot be READ at either side — unparseable, ambiguous,
    or not valid UTF-8. "I cannot tell what this file pins" is not a reason to
    wave a release through; it is the state a human must look at. (This is the
    inverse of the old behaviour, where an unreadable pin skipped.)
  * PASS when the pin is unchanged or moves backward.

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

#: Canonical semver only — no leading zeros. `int()` collapses "2.1.0246" and
#: "2.1.246" to the same tuple, so a pin written the first way reads as UNCHANGED
#: against the second and skips the gate entirely. npm cannot install a
#: leading-zero version either, so refusing to compare it is right twice.
_STRICT_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")

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
    #: "ok" | "receipts" | "unreadable". Lets a caller distinguish "the pin moved
    #: without receipts" from "I could not read the pin", which are different
    #: conversations with the operator even though both block.
    reason: str = "ok"


def _strip_formatting_chars(value: str) -> str:
    """Drop Unicode format characters (category Cf) — zero-width space, joiners,
    bidi controls. Python's ``\\S`` treats every one of them as non-whitespace, so
    a body of ``CC-Gate-Soak: \\u200b`` satisfied a "has a non-space value" test
    while rendering as an empty line."""
    return "".join(ch for ch in value if unicodedata.category(ch) != "Cf")


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
        stripped = line.strip()

        if in_comment:
            # Only the closer ends it; an opener inside a comment is inert.
            if _COMMENT_CLOSE in line:
                in_comment = False
            continue

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

        if _COMMENT_OPEN in line:
            before, _, rest = line.partition(_COMMENT_OPEN)
            if _COMMENT_CLOSE not in rest:
                in_comment = True
            # Text before the opener is still visible; the rest is not.
            if before.strip():
                visible.append(before)
            continue

        visible.append(line)

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


def _version_tuple(version: str) -> tuple[int, ...]:
    if not _STRICT_SEMVER.match(version):
        raise PinUnreadable(
            f"{version!r} is not canonical semver (leading zeros, or not X.Y.Z). "
            "Refusing to compare: 2.1.0246 and 2.1.246 compare EQUAL as integers, "
            "so a pin written that way would read as unchanged."
        )
    return tuple(int(p) for p in version.split("."))


def _pin_of(text: str, *, where: str) -> str:
    pin = parse_cc_version(text)
    if pin is None:
        raise PinUnreadable(
            f"could not determine CC_VERSION from {_PIN_PATH} at {where} — the "
            "pin is absent, or assigned more than once so the file's effective "
            "value is not statable. A pin nobody can read must not ship."
        )
    return pin


def evaluate(*, base_pin_text: str, head_pin_text: str, body: str) -> Verdict:
    """The whole decision, as a pure function.

    Takes the two file CONTENTS rather than reading them, so the CI adapter can
    supply them from git and the merge gate from the GitHub API, and so the
    behaviour is testable without a repository at all.

    NEVER raises for a policy outcome — every decision, pass or block, comes back
    as a Verdict. A caller that reads ``.blocked`` gets the whole truth, and one
    that forgets a ``try`` does not get a silent pass.
    """
    try:
        head_pin = _pin_of(head_pin_text, where="the proposed change")
        base_pin = _pin_of(base_pin_text, where="the base branch")
        return _compare(base_pin, head_pin, body)
    except PinUnreadable as exc:
        return Verdict(True, str(exc), reason="unreadable")


def _compare(base_pin: str, head_pin: str, body: str) -> Verdict:

    if head_pin == base_pin:
        return Verdict(False, f"CC pin unchanged ({head_pin}) — no receipts required.")

    if _version_tuple(head_pin) < _version_tuple(base_pin):
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

    lines = [
        f"CC pin moves FORWARD ({base_pin} → {head_pin}) but the PR body is missing "
        f"{len(absent)} required gate receipt(s):",
        "",
    ]
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
    return Verdict(True, "\n".join(lines), tuple(absent), reason="receipts")


# ── adapters ──────────────────────────────────────────────────────────────


def read_pin_at(ref: str, *, repo_root: Path) -> str:
    """The pin file's contents as of ``ref``. Raises PinUnreadable."""
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
        raise PinUnreadable(f"cannot run git: {exc}") from exc
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
        verdict = evaluate(
            base_pin_text=read_pin_at(args.base_sha, repo_root=args.repo_root),
            head_pin_text=read_pin_head(args.repo_root),
            body=body,
        )
    except PinUnreadable as exc:  # I/O from the adapters, not a policy outcome
        verdict = Verdict(True, str(exc), reason="unreadable")

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
