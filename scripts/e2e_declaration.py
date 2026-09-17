"""The `E2E:` PR-body declaration — one parser, two readers (issue #1699 sibling).

WHY THIS EXISTS. Running the end-to-end verification a merged change needs is an
obligation that lives AFTER the merge, and it was carried by memory alone. Measured
2026-09-04: of two owner-directed merges, one had its E2E forgotten until the owner
asked — 50% on n=2, and BOTH E2Es, once run, found something real. Two failure modes
sit behind that: (A) nobody decided whether the PR needed an E2E at all, and (B) the
decision was made and then evaporated at merge time. This module is the shared
reading of the declaration that closes (A); the repo-pulse worker turns a declaration
into a durable row to close (B).

THE CONVENTION, in the PR body:

    E2E: <one-line plan for the post-merge verification>
    E2E: none — <reason there is no runtime surface to verify>

The point is NOT to force an E2E onto a docs PR. It is to make the DECISION explicit
— the obligation-side twin of "a check that could not run must say so" (#1683).

`none` SATISFIES THIS READER BUT DOES NOT RELEASE THE VALIDATOR (spec §8.13). A validation
session assumes every merged PR has an end-to-end test and hunts for it; this line is
its first LEAD, never a boundary. So `none` is a declaration, not an authority — a
builder who forgets, or who declares `none` wrongly, is still caught from the merge
side.

WHY THE PARSING IS THIS PARANOID. Every hardening below was bought by a measured
defect in `check_cc_pin_receipts.py`, the sibling body-reader, and is inherited
deliberately rather than re-learned:

  * HTML comments and fenced blocks are stripped BEFORE any marker is sought. This
    repo's own PULL_REQUEST_TEMPLATE.md is built from `<!-- -->` blocks, so a
    declaration that is invisible in the rendered PR is the likely accident, not an
    exotic one — and a fence is how the format gets DOCUMENTED, so text inside one
    describes a declaration rather than making one.
  * The stripping is a LINE SCANNER, never a regex pair. A non-greedy `<!--.*?-->`
    costs ~14s of CPU on a 65_536-byte body (GitHub's cap) — tolerable in an advisory
    CI job, fatal on the merge gate, which runs under a wall clock and skips its
    remaining checks when killed. And both regexes need a CLOSER, so an unterminated
    opener inverts the rule.
  * Horizontal whitespace only (`[^\\S\\n]`) around the colon. Plain `\\s*` crosses
    newlines, which let an EMPTY marker borrow the NEXT line's text as its value.
  * Markdown wrappers are tolerated (`-`, `*`, `>`, `- [x]`, `**E2E**`, `` `E2E` ``).
    Refusing to read a compliant PR's declaration is how an operator learns to
    ignore the advisory. (Written when this reader gated the merge; it is
    advisory since 2026-09-06, and the tolerance is worth keeping either way —
    an advisory nobody's declaration matches is an advisory nobody reads.)
  * EVERY occurrence is considered, not the first: a leftover template line above a
    filled one must not veto the filled one.
  * Placeholders are derived from the shipped examples, never hand-listed.

THE SHARPEST POINT, stated so the next reader does not "simplify" it away: the
`none — <reason>` form is a TRAILING QUALIFICATION, the exact shape
`repo_pulse.FOLLOWUP_MARKER_RE` is deliberately anchored at BOTH ends to reject.
That rejection is right for a 32-hex id, where any trailing text means the line was
prose about an id rather than a claim on it. Here the trailing text IS the payload,
so both-end anchoring cannot be copied. What replaces it is line-START anchoring plus
a substance check on the reason: prose like "the E2E: I ran it by hand" does not
begin a line at the marker, and `E2E: none` with no reason is INVALID (reported
undeclared) rather than a silent pass.

Pure functions, stdlib only, no I/O — so the merge gate and the repo-pulse worker can
both load it without dragging in the other's dependencies.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

#: PRs created BEFORE this instant are exempt. The convention cannot bind a body
#: written before it existed, and retro-blocking the open queue would make an
#: unrelated backlog the price of landing this. Compared against the PR's
#: ``createdAt`` (GitHub ISO-8601, UTC). Set to the day after this lands so nothing
#: in flight is caught mid-review.
E2E_CUTOFF_ISO = "2026-09-08T00:00:00Z"

#: The marker, and the two shipped example values. The placeholder set is DERIVED
#: from these, so a pasted-but-unfilled template can never read as a declaration.
MARKER = "E2E"
_EXAMPLES = (
    "<one-line plan for the post-merge verification>",
    "none — <reason there is no runtime surface to verify>",
)
_PLACEHOLDERS = frozenset(tok.lower() for ex in _EXAMPLES for tok in re.findall(r"<[^<>]+>", ex))

#: GitHub's PR-body cap. Bounds the scan regardless of its O(n).
_MAX_BODY = 65_536
_COMMENT_OPEN, _COMMENT_CLOSE = "<!--", "-->"
_FENCE_MARKS = ("```", "~~~")

#: Words that state an omission rather than a decision. `none` is deliberately NOT
#: here — it is a VALID declaration when it carries a reason, and is classified
#: before this set is consulted.
_REFUSAL_WORDS = frozenset({"todo", "tbd", "pending", "n/a", "na", "yes", "no", "?"})

#: A reason/plan must carry real content — but the floor is deliberately LOW, and
#: the placeholder/refusal checks above it do the real work. MEASURED (architect,
#: 2026-09-06): at 12 this rejected `none — docs only` (8 alnum) — the exact string
#: this module's own GUIDANCE prints as the remedy. When this reader still GATED
#: the merge, with no override sigil, that was a closed loop: the author was
#: blocked, typed the suggested line verbatim, and was refused identically, with
#: nothing in the message naming an undocumented length rule. A false refusal here
#: is worse than a false pass — it was worse as a block, and it is still worse as
#: an advisory, because a reader who is wrongly corrected stops reading. And
#: `test_every_example_in_the_guidance_passes_the_parser` now makes the class
#: unrepeatable: any example this module tells an author to write must survive it.
#: 7 admits "docs only" and "prose-only"; "x", "ok" and "n/a" still fail.
_MIN_SUBSTANCE = 7

#: Markdown-tolerant, line-START anchored. See the module docstring for why every
#: piece is here. `re.IGNORECASE` so `e2e:` works; the marker is short, and the
#: line-start anchor is what keeps it from matching prose.
#: The marker's PREFIX — everything up to and including the colon. Shared by the
#: value-bearing pattern and the presence-only probe so the two can never drift
#: about what counts as a marker (one definition, two questions).
_MARKER_PREFIX = (
    rf"^[^\S\n]*(?:[-*+>][^\S\n]*)*(?:\[[ xX]\][^\S\n]*)?"
    rf"[*_`]{{0,2}}{re.escape(MARKER)}[*_`]{{0,2}}"
    rf"[^\S\n]*:"
)
_MARKER_RE = re.compile(
    _MARKER_PREFIX + r"[^\S\n]*(\S[^\n]*)$",
    re.MULTILINE | re.IGNORECASE,
)
#: Presence only — matches a marker whether or not it carries a value, so an EMPTY
#: declaration can be reported as empty rather than as absent.
_MARKER_PRESENT_RE = re.compile(_MARKER_PREFIX, re.MULTILINE | re.IGNORECASE)

#: A `none` declaration: the word, then ANY separator an author might type, then
#: the reason. The shipped example uses an em dash, but requiring one misclassified
#: every other natural phrasing as a PLAN — MEASURED before this shape existed:
#: `none because docs only`, `none, docs only` and `none: docs only` all read as
#: plans. At the gate that is invisible (both kinds pass), which is exactly why it
#: had to be found by mutation rather than by use: the damage lands downstream,
#: where the repo-pulse worker turns a PLAN into a hot follow-up row and a docs PR
#: acquires a junk obligation reading "Run the declared E2E: none, docs only".
#: The reason itself is still REQUIRED — a bare `E2E: none` is the decision left
#: unmade wearing the grammar of a decision — but that is enforced by the substance
#: check on the remainder, not by the punctuation.
#: A separator (or an empty remainder, or an explicit "needed/required") is
#: REQUIRED. MEASURED (architect, 2026-09-06) with the separator optional: `none of
#: the existing tests cover the new path; run it by hand after merge` classified as
#: `none` — a real PLAN swallowed. That is failure mode (B) from the spec arriving
#: through the parser: at the gate both kinds pass, so nothing shows, and in PR-2b
#: a `none` creates NO follow-up row — so a PR that declared a real plan silently
#: acquires no obligation, which is the exact loss this feature exists to prevent.
_NONE_RE = re.compile(
    r"^none(?:[^\S\n]+(?:needed|required|necessary|applicable))?"
    r"[^\S\n]*(?:(?:[—–\-:,;.]|\bbecause\b)[^\S\n]*(.*))?$",
    re.IGNORECASE | re.DOTALL,
)

_FORMATTING = "*_`~ \t"


def _strip_formatting(value: str) -> str:
    return value.strip(_FORMATTING)


def _load_sibling_readable_body():
    """``readable_body`` from ``check_cc_pin_receipts.py`` — the SAME scanner the pin
    gate uses, so the two body-readers can never disagree about what is visible.

    Falls back to the local scanner below if that module cannot be loaded (it carries
    imports this one deliberately does not need). Degrading the STRIPPING is safe in
    the direction that matters: the local copy implements the same rules."""
    import sys

    name = "_cc_pin_receipts_for_e2e"
    try:
        path = Path(__file__).resolve().parent / "check_cc_pin_receipts.py"
        spec = importlib.util.spec_from_file_location(name, path)
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            # REGISTERED BEFORE exec: that module's dataclasses resolve their own
            # module out of sys.modules, so exec-ing an unregistered module raises
            # `AttributeError: 'NoneType' object has no attribute '__dict__'`.
            # Without this line the load ALWAYS failed and the local fallback
            # silently ran instead — so the docstring's "the SAME scanner the pin
            # gate uses, so the two can never disagree" was false in every run
            # (Kimi P2, 2026-09-06). The guard's own _load_pin_receipt_checker
            # carries this line and says why; I read that comment and did not copy
            # it. On failure the half-initialised entry is removed so a later
            # import of the real module is not poisoned.
            sys.modules[name] = mod
            try:
                spec.loader.exec_module(mod)
            except Exception:
                sys.modules.pop(name, None)
                raise
            fn = getattr(mod, "readable_body", None)
            if callable(fn):
                return fn
    except Exception:
        pass
    return None


def _local_readable_body(body: str) -> str:
    """Line scanner: drop fenced blocks and HTML comments. Mirrors the sibling's
    rules (a fenced line is opaque and never interpreted; an unterminated opener
    hides to the end, as CommonMark does)."""
    visible: list[str] = []
    in_comment = False
    fence: str | None = None
    for line in body[:_MAX_BODY].splitlines():
        if fence is not None:
            if line.strip().startswith(fence):
                fence = None
            continue
        out: list[str] = []
        rest = line
        while rest:
            if in_comment:
                close = rest.find(_COMMENT_CLOSE)
                if close == -1:
                    rest = ""
                    break
                in_comment = False
                rest = rest[close + len(_COMMENT_CLOSE) :]
                continue
            open_at = rest.find(_COMMENT_OPEN)
            if open_at == -1:
                out.append(rest)
                break
            out.append(rest[:open_at])
            in_comment = True
            rest = rest[open_at + len(_COMMENT_OPEN) :]
        kept = "".join(out)
        stripped = kept.strip()
        for mark in _FENCE_MARKS:
            if stripped.startswith(mark):
                fence = mark
                break
        else:
            visible.append(kept)
            continue
    return "\n".join(visible)


def readable_body(body: str) -> str:
    """The part of a PR body a human actually reads."""
    fn = _load_sibling_readable_body()
    return fn(body[:_MAX_BODY]) if fn else _local_readable_body(body)


def _reason_is_real(value: str) -> bool:
    cleaned = _strip_formatting(value)
    normalised = re.sub(r"<\s*([^<>]+?)\s*>", r"<\1>", cleaned.lower())
    if any(p in normalised for p in _PLACEHOLDERS):
        return False
    # A value that is ENTIRELY one angle-bracketed span is a template placeholder
    # whatever words it contains — an author who reworded or reflowed the shipped
    # example (`< one-line plan >`) still has not made a decision. Derived
    # placeholders alone caught only the verbatim string; before the substance
    # floor was lowered, the reworded form was rejected by LENGTH, which is an
    # accident rather than a rule. Safe by shape: a real plan or reason is never
    # wholly wrapped in angle brackets.
    if re.fullmatch(r"<[^<>]+>", normalised.strip()):
        return False
    if normalised.strip(" .-—–") in _REFUSAL_WORDS:
        return False
    return sum(ch.isalnum() for ch in cleaned) >= _MIN_SUBSTANCE


def parse_e2e(body: str | None) -> dict:
    """Classify a PR body's E2E declaration.

    Returns ``{"kind", "text", "detail"}`` where kind is:
      * ``plan``    — a real post-merge verification plan (the text).
      * ``none``    — an explicit, reasoned "no runtime surface" (the reason).
      * ``absent``  — no ``E2E:`` line at all.
      * ``invalid`` — a line exists but says nothing (empty, placeholder, a bare
        ``none`` with no reason, a refusal word). Classified as undeclared, the
        same as absent, but reported differently so the author is told WHICH
        mistake they made.

    CRLF is normalised first: GitHub's textarea stores bodies with `\\r\\n`, and a
    line-anchored `$` would otherwise never match a real line (the defect
    `repo_pulse._pr_text` was written to fix).

    LAST valid occurrence wins for content, and ANY valid occurrence satisfies
    presence — so a filled line below a leftover template line is what counts.
    """
    if not body:
        return {"kind": "absent", "text": "", "detail": "empty PR body"}

    text = body[:_MAX_BODY].replace("\r\n", "\n").replace("\r", "\n")
    visible = readable_body(text)

    values = _MARKER_RE.findall(visible)
    if not values:
        # A VALUELESS marker (`E2E:` with nothing after it) does not match the
        # value-bearing pattern at all, so without this probe the single most
        # common mistake — the template pasted and the line left unfilled, which
        # is the state EVERY template-created PR starts in — reported "no E2E:
        # line in the PR body" to an author looking at a body that visibly
        # contains one (architect SHOULD-FIX, 2026-09-06). The `invalid` bucket
        # exists to name WHICH mistake was made; this is the one it most owes.
        if _MARKER_PRESENT_RE.search(visible):
            return {
                "kind": "invalid",
                "text": "",
                "detail": "the E2E: line is present but EMPTY — fill it in",
            }
        return {"kind": "absent", "text": "", "detail": "no E2E: line in the PR body"}

    best: dict | None = None
    saw_invalid_detail = ""
    for raw in values:
        value = _strip_formatting(raw)
        none_match = _NONE_RE.match(value)
        if none_match:
            # group(1) is None for a bare `none` (the whole separator+reason group
            # is optional), which is the "decision left unmade" case — coalesce to
            # "" so the substance check below is the single place that decides.
            reason = (none_match.group(1) or "").strip()
            if _reason_is_real(reason):
                best = {"kind": "none", "text": reason, "detail": ""}
            else:
                saw_invalid_detail = (
                    "`E2E: none` needs a REASON — what makes this PR have no "
                    "runtime surface to verify (e.g. `none — docs only`)"
                )
            continue
        # No separate bare-`none` branch: _NONE_RE matches `none` with an empty
        # remainder, so the substance check above is the single place that decides
        # whether a `none` carries its reason. One decision point, not two that
        # must agree.
        if _reason_is_real(value):
            best = {"kind": "plan", "text": value, "detail": ""}
        elif not saw_invalid_detail:
            # Name the ACTUAL rule. A terse-but-real plan ("manual", "smoke",
            # "run CI") fails only the length floor, and telling that author the
            # line "has no real content" is a false diagnosis that sends them
            # guessing (Kimi P3, 2026-09-06). It mattered more when this reader
            # gated the merge with no override; it still matters, because an
            # advisory that misdiagnoses is one nobody reads twice. Say which
            # rule bit, and quote what they wrote back to them.
            shown = _strip_formatting(value)[:60]
            if sum(ch.isalnum() for ch in _strip_formatting(value)) < _MIN_SUBSTANCE:
                saw_invalid_detail = (
                    f"the E2E: line is too short to be a plan ({shown!r}) — give at "
                    f"least a few words saying what will be run, or use "
                    f"`E2E: none — <reason>`"
                )
            else:
                saw_invalid_detail = (
                    "the E2E: line has no real content (a placeholder, or a "
                    "template value left unfilled)"
                )

    if best is not None:
        return best
    return {"kind": "invalid", "text": "", "detail": saw_invalid_detail}


def is_pre_cutoff(created_at: str | None) -> bool:
    """True when a PR predates the convention and is therefore exempt.

    Fails CLOSED (not exempt) on an unreadable/absent timestamp: the pre-cutoff
    population is finite and shrinking, so treating a parse failure as "old" would
    build a permanent hole out of a temporary transition.
    """
    if not created_at:
        return False
    try:
        stamp = created_at.strip().replace("Z", "+00:00")
        cutoff = E2E_CUTOFF_ISO.replace("Z", "+00:00")
        from datetime import datetime

        return datetime.fromisoformat(stamp) < datetime.fromisoformat(cutoff)
    except (ValueError, TypeError):
        return False


#: What the advisory prints when a body has no usable declaration. Names BOTH
#: valid forms and the §8.13 seam, so an author reading the NOTE knows that `none`
#: is a real option AND that it does not end the obligation. Nothing here is
#: enforced — which raises the bar on this text rather than lowering it: an
#: advisory earns its reading or gets skipped.
GUIDANCE = (
    "Add an E2E: line to the PR body — one of:\n"
    f"  {MARKER}: <one-line plan for the post-merge verification>\n"
    f"  {MARKER}: none — <reason there is no runtime surface to verify>\n"
    # A CONCRETE example, not only the bracketed template: an author reading this
    # needs a line they can copy, and `test_every_example_in_the_guidance_passes_
    # the_parser` runs every concrete example here through the parser so this text
    # can never prescribe a remedy the parser would refuse. That test predates the
    # advisory downgrade and outlives it: the examples were WORSE than useless when
    # the reader rejected its own printed remedy, and are still the whole value now
    # that nothing forces anyone to read them.
    # BOTH forms get a concrete, copyable example. Offering one only for `none`
    # meant the single copyable line was the one that creates NO obligation — in
    # text whose purpose is prompting them (Kimi P3, 2026-09-06).
    f"For example:  {MARKER}: restart genesis-server and confirm /api/genesis/health answers 200\n"
    f"         or:  {MARKER}: none — docs only, no runtime surface\n"
    "`none` is a legitimate answer for a docs/prose PR. Leaving the line out does "
    "NOT block the merge — but nothing else records the decision yet either "
    "(the per-merge obligation row is unbuilt, issue #1718), so right now this "
    "line is the only record. Note `none` does NOT release the validator, which "
    "assumes every merged PR has an E2E and hunts for one anyway (spec §8.13) — "
    "the line is its first lead, not a boundary."
)
