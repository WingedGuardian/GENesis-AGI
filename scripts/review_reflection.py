#!/usr/bin/env python3
"""The round reflection: what a session writes before a review round's first fix.

A review ROUND is a commit head that drew findings (``review_budget``). Before
fixing anything in an open round, the session reads the round as data and
commits a reflection that answers for every finding in it: how the findings
distribute, whether the change's premise holds, how the PR's scope fares, the
decision, and one disposition per finding (fix now, or file it). The
reflection is an EMPTY commit whose message is the reflection, so no reviewer
is ever triggered by it.

The format is a CLOSED HEADER BLOCK (owner ruling 2026-10-07): from the first
line to the first blank line, every line is one fixed ``Field: value``, each
value matched end to end, plain printable ASCII only. Any other line is
refused by line number. Prose may follow the blank line and is never parsed.
Reading free markdown with regexes made every markdown construct (fences,
list markers, comments, lookalike characters, headings git strips) a separate
hole; a closed grammar has none of those to handle::

    Round-reflection: keys=c101,c102 head=<40-hex sha>
    Class: unchecked subprocess result = 2
    Premise: LEAD
    Premise-why: the change routes around an existing helper
    Impossible: route every call through one checked helper
    Scope: covered: the key round-trips (tests/test_x.py)
    Decision: close-class
    Decision-why: the same mistake appears twice
    Disposition: c101 fix-now test=1
    Disposition: c102 file issue=#123
    Escalate: no

Rounds that owe more add ``Audit-evidence: <path> <label>``, or two
``Premise-check: P1 TRUE <text>`` lines plus ``Premise-evidence: <path>
SOUND|SOUND-BUT-INFERIOR|BROKEN`` (see ``obligations``). The audit's label is
free text and unchecked: an audit has no verdict vocabulary to bind it to. The
premise verdict is bound: it, the reflection's ``Premise:`` and every
``Premise-check`` claim must restate what the cited file concludes, so a file
concluding BROKEN forces an escalating reflection. Each cited file must be a regular file of at
most 1 MiB, written after the round started and before the reflection was
committed; a relative path is read from the repository the check runs in.
Cite a copy that nothing else rewrites (``<pr>-round<N>-<kind>.txt``), never
the review gate's per-worktree evidence file, which the next review replaces.
A premise-evidence file must carry the canonical ``Design-premise:`` block of
``.claude/docs/premise-check.md``: the verdict line at column 0 and at least
two ``P<n> <verdict>`` claim lines directly under it, verdict first. Each of
the PR's acceptance points must appear within one Scope line that starts
``covered:``; that is a substring floor for an honest session, not proof
that the line covers the point.

``covered_keys`` is THE coverage check: the commit gate (a later change) calls
it, and it applies every local rule, so a reflection committed by hand is held
to exactly the standard a tool-made one is.

CLI: ``status`` (what the open round owes, whether it has settled) and
``validate`` (a file, offline). Exit 1 means the reflection is invalid; exit
2 means the check could not be made.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

SETTLE = timedelta(minutes=30)
MIN_BLOCK_CHARS = 300
LOG_DEPTH = 1000
#: A template placeholder; it can never match a field, so it is refused anyway,
#: but naming it gives a clearer message.
FILL = "<FILL:"

# [0-9], never \d: \d matches every Unicode digit, and the header line is
# matched by this pattern alone.
_KEY = r"(?:c[0-9]{1,20}|r[0-9]{1,20}:[0-9]{1,9}|i[0-9]{1,20})"
HEADER_RE = re.compile(rf"^Round-reflection: keys=({_KEY}(?:,{_KEY})*) head=([0-9a-f]{{40}})$")
_TEXT = r"[!-~][ -~]{9,299}"  # 10-300 printable ASCII characters, not starting with a space
#: Every field the block may hold: name -> (pattern for the whole line, min, max).
FIELDS: dict[str, tuple[re.Pattern[str], int, int | None]] = {
    "Class": (
        re.compile(r"^Class: ([A-Za-z0-9][A-Za-z0-9 ,/()'.-]{1,79}) = ([1-9][0-9]{0,3})$"),
        1,
        None,
    ),
    "Premise": (re.compile(r"^Premise: (LEAD|SOUND|SOUND-BUT-INFERIOR|BROKEN|SUSPECT)$"), 1, 1),
    "Premise-why": (re.compile(rf"^Premise-why: ({_TEXT})$"), 1, 1),
    "Impossible": (re.compile(rf"^Impossible: ({_TEXT})$"), 1, 1),
    "Scope": (re.compile(rf"^Scope: ({_TEXT})$"), 1, None),
    "Decision": (re.compile(r"^Decision: (fix-instances|close-class|rework|send-back)$"), 1, 1),
    "Decision-why": (re.compile(rf"^Decision-why: ({_TEXT})$"), 1, 1),
    "Disposition": (
        re.compile(
            rf"^Disposition: ({_KEY}) (fix-now test=[1-4](?:,[1-4]){{0,3}}|file issue=#[1-9][0-9]{{0,6}})$"
        ),
        1,
        None,
    ),
    "Audit-evidence": (re.compile(r"^Audit-evidence: ([!-~]{1,300})(?: ([ -~]{1,300}))?$"), 0, 1),
    "Premise-evidence": (
        re.compile(r"^Premise-evidence: ([!-~]{1,300}) (SOUND-BUT-INFERIOR|SOUND|BROKEN)$"),
        0,
        1,
    ),
    "Premise-check": (
        re.compile(rf"^Premise-check: P([1-9][0-9]?) (TRUE|FALSE|UNPROVEN) ({_TEXT})$"),
        0,
        None,
    ),
    "Escalate": (re.compile(r"^Escalate: (yes|no)$"), 1, 1),
}
_FIELD_NAME_RE = re.compile(r"^([A-Za-z-]{1,20}): ")
#: The Premise and Escalate fields decide escalation. This search over the
#: whole message (prose included, NFKC-normalised) is a best-effort extra that
#: leans toward escalating; it does not catch every spelling or lookalike.
_ESCALATION_ANYWHERE_RE = re.compile(
    r"(?:escalate\s*:\s*yes\b|premise\s*:\s*\W*\s*(?:broken|suspect)\b)", re.IGNORECASE
)
_WS_RE = re.compile(r"\s+")


class Refused(Exception):
    """The check cannot be made; the message says why."""


# -- the reflection itself ---------------------------------------------------


@dataclass
class Reflection:
    head: str | None = None
    keys: list[str] = field(default_factory=list)
    classes: list[str] = field(default_factory=list)
    verdict: str | None = None
    decision: str | None = None
    escalate: bool = False
    audit_evidence: str | None = None
    premise_evidence: str | None = None
    premise_evidence_verdict: str | None = None
    scopes: list[str] = field(default_factory=list)
    premise_checks: int = 0
    #: ``Premise-check`` claims by number: what the reflection says the cited
    #: premise check concluded, bound to that file by ``evidence_problem``.
    premise_claims: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def obligations(round_number: int, gate_lane: bool) -> tuple[bool, bool]:
    """``(audit, premise)`` owed by the reflection of this round.

    The review ladder: the gate lane runs a class audit at round 1 and the
    premise check at round 2, its terminal round (owner rulings 2026-09-30).
    The ordinary lane checks the premise from round 2 and runs the class sweep
    at round 3, before its terminal round 4 (owner ruling 2026-10-08: rework
    costs least at round 2, so the premise question comes first there). The
    audit belongs to ITS round; the premise check stays owed after its round.
    """
    if gate_lane:
        return round_number == 1, round_number >= 2
    return round_number == 3, round_number >= 2


def _normalize(text: str) -> str:
    return _WS_RE.sub(" ", text).strip().lower()


def _escalates_anywhere(text: str) -> bool:
    folded = unicodedata.normalize("NFKC", text)
    folded = "".join(ch for ch in folded if unicodedata.category(ch) != "Cf")
    return bool(_ESCALATION_ANYWHERE_RE.search(folded))


def parse(
    text: str,
    *,
    round_number: int | None = None,
    gate_lane: bool = False,
    acceptance: Sequence[str] | None = None,
    previous_classes: Iterable[str] = (),
) -> Reflection:
    """Check a reflection against the closed grammar, offline. Never raises.

    Always checked: the header on the first line, every block line a known
    field matched end to end, field counts, one disposition per header key and
    nothing else, and the block's length. Given a round, also the round's
    obligations, the round-2 verdict and the recurring-class rule; given
    ``acceptance`` bullets, each must appear in a Scope line.
    """
    result = Reflection()
    result.escalate = _escalates_anywhere(text)
    lines = text.split("\n")
    end = next((i for i, line in enumerate(lines) if line == ""), len(lines))
    block = lines[:end]
    header = HEADER_RE.match(block[0]) if block else None
    if header is None:
        result.problems.append(
            "line 1 must be exactly 'Round-reflection: keys=<key>[,<key>...] head=<40-hex sha>'"
        )
    else:
        result.keys = header.group(1).split(",")
        result.head = header.group(2)
        if len(set(result.keys)) != len(result.keys):
            result.problems.append("the header names a key twice")
    if FILL in text:
        result.problems.append(f"unfilled template placeholder(s) remain ('{FILL} ...>')")
    if len("\n".join(block)) < MIN_BLOCK_CHARS:
        result.problems.append(f"the header block is at least {MIN_BLOCK_CHARS} characters")

    seen: dict[str, list[re.Match[str]]] = {name: [] for name in FIELDS}
    for number, line in enumerate(block[1:], start=2):
        if any(not (" " <= ch <= "~") for ch in line):
            result.problems.append(f"line {number}: only printable ASCII is allowed in the block")
            continue
        name = _FIELD_NAME_RE.match(line)
        spec = FIELDS.get(name.group(1)) if name else None
        match = spec[0].match(line) if spec else None
        if match is None:
            result.problems.append(
                f"line {number}: not a recognised field, or its value does not match "
                f"({line[:60]!r})"
            )
            continue
        seen[name.group(1)].append(match)
    for field_name, (_, low, high) in FIELDS.items():
        count = len(seen[field_name])
        if count < low:
            result.problems.append(f"missing '{field_name}:' line")
        if high is not None and count > high:
            result.problems.append(f"'{field_name}:' appears {count} times (at most {high})")

    result.classes = [_normalize(m.group(1)) for m in seen["Class"]]
    result.scopes = [m.group(1) for m in seen["Scope"]]
    # Distinct premise numbers: one check written twice is still one check.
    result.premise_checks = len({m.group(1) for m in seen["Premise-check"]})
    for m in seen["Premise-check"]:
        if result.premise_claims.setdefault(m.group(1), m.group(2)) != m.group(2):
            result.problems.append(f"'Premise-check: P{m.group(1)}' is given two verdicts")
    if seen["Premise"]:
        result.verdict = seen["Premise"][0].group(1)
    if seen["Decision"]:
        result.decision = seen["Decision"][0].group(1)
    if seen["Audit-evidence"]:
        result.audit_evidence = seen["Audit-evidence"][0].group(1)
    if seen["Premise-evidence"]:
        result.premise_evidence = seen["Premise-evidence"][0].group(1)
        result.premise_evidence_verdict = seen["Premise-evidence"][0].group(2)
    if any(m.group(1) == "yes" for m in seen["Escalate"]) or result.verdict in {
        "BROKEN",
        "SUSPECT",
    }:
        result.escalate = True

    disposed = [m.group(1) for m in seen["Disposition"]]
    if len(set(disposed)) != len(disposed):
        result.problems.append("a key has two dispositions")
    if result.keys:
        missing = [k for k in result.keys if k not in disposed]
        extra = sorted({k for k in disposed if k not in result.keys})
        if missing:
            result.problems.append("no disposition for: " + ", ".join(missing))
        if extra:
            result.problems.append(
                "dispositions for keys the header does not name: " + ", ".join(extra)
            )

    if round_number is None:
        return result

    if round_number >= 2 and result.verdict == "LEAD":
        result.problems.append(
            "from round 2 the premise verdict is SOUND, SOUND-BUT-INFERIOR or BROKEN"
        )
    covered_scopes = [
        line for line in map(_normalize, result.scopes) if line.startswith("covered:")
    ]
    for item in acceptance or ():
        wanted = _normalize(item)
        if wanted and not any(wanted in line for line in covered_scopes):
            result.problems.append(
                f"no 'Scope: covered:' line maps the acceptance point: {item[:80]}"
            )
    audit_owed, premise_owed = obligations(round_number, gate_lane)
    if audit_owed and not result.audit_evidence:
        result.problems.append(
            f"round {round_number} cites a fresh-context audit: 'Audit-evidence: <path> <verdict>'"
        )
    if premise_owed and result.premise_checks < 2:
        result.problems.append(
            f"round {round_number} carries an independent premise check: at least two "
            "'Premise-check: P<n> TRUE|FALSE|UNPROVEN <text>' lines"
        )
    if premise_owed and not result.premise_evidence:
        result.problems.append(
            f"round {round_number} cites the premise check's output: "
            "'Premise-evidence: <path> SOUND|SOUND-BUT-INFERIOR|BROKEN'"
        )
    recurring = sorted(set(result.classes) & {_normalize(c) for c in previous_classes})
    if recurring and result.decision == "fix-instances":
        result.problems.append(
            "a class recurs from an earlier round ("
            + ", ".join(recurring)
            + "): the decision is close-class, rework or send-back, never fix-instances"
        )
    return result


# -- git and the network -----------------------------------------------------

#: ``git replace`` could swap a fix commit for an empty one in every read.
_GIT_ENV = {**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"}


def _run(argv: Sequence[str], cwd: str | None = None) -> tuple[int, str, str]:
    """Run a command. Output is decoded with replacement, so a commit message
    in a legacy encoding is read (and then refused by the grammar), never a
    crash."""
    try:
        done = subprocess.run(
            list(argv),
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
            env=_GIT_ENV,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)
    return done.returncode, done.stdout, done.stderr


def _git(cwd: str, *args: str) -> str:
    code, out, err = _run(["git", "-C", cwd, *args])
    if code != 0:
        raise Refused(f"git {args[0]} failed: {err.strip()[:300]}")
    return out


def pr_identity(cwd: str) -> tuple[str, int]:
    """``(repo, number)`` of the current branch's open PR, through the commit
    gate's own reading of ``gh pr status`` (one parser, never a copy)."""
    import review_enforcement_commit as rec  # noqa: PLC0415 - heavy only on use

    code, out, err = _run(["gh", "pr", "status", "--json", "number,state,url"], cwd=cwd)
    if code != 0:
        raise Refused(f"gh pr status failed: {err.strip()[:300]}")
    identity = rec._current_branch_pr_identity(out)
    if identity is None:
        raise Refused("this branch has no open pull request; there is no round to reflect on")
    if isinstance(identity, dict):
        raise Refused(
            "cannot tell which pull request this branch belongs to: "
            + ", ".join(identity.get("errors", []))
        )
    return identity


def _pr_meta(repo: str, number: int) -> dict[str, Any]:
    code, out, err = _run(
        ["gh", "pr", "view", str(number), "--repo", repo, "--json", "body,baseRefName"]
    )
    if code != 0:
        raise Refused(f"cannot read the PR: {err.strip()[:300]}")
    try:
        meta = json.loads(out)
    except json.JSONDecodeError as exc:
        raise Refused(f"unreadable PR metadata from GitHub: {exc}") from exc
    if not isinstance(meta, dict):
        raise Refused("unreadable PR metadata from GitHub")
    return meta


#: What ``parse_acceptance`` says when the PR simply declares no acceptance
#: list. Any OTHER problem without bullets means the body could not be read
#: reliably, which must refuse, never silently disable the scope rule.
_ACCEPTANCE_ABSENT = ("empty PR body", "no ## Acceptance section")


def acceptance_points(body: str) -> list[str] | None:
    """The PR's acceptance bullets, None when it declares none. Raises
    ``Refused`` when a declaration exists but cannot be read."""
    import acceptance_declaration  # noqa: PLC0415

    parsed = acceptance_declaration.parse_acceptance(body)
    if parsed.get("present"):
        return list(parsed["bullets"])
    problems = list(parsed.get("problems", []))
    if not any(p in _ACCEPTANCE_ABSENT for p in problems):
        # Not "declares none": unreadable, too large, ambiguous, or a section
        # with no bullets. Refuse rather than silently drop the scope rule.
        raise Refused(
            "the PR's acceptance list cannot be read reliably ("
            + "; ".join(parsed.get("problems", []))
            + "): fix the PR body so the scope rule can be checked"
        )
    return None


# -- what is covered and what is owed ----------------------------------------


def _commit_objects(cwd: str, shas: Sequence[str]) -> dict[str, bytes]:
    """The raw commit objects, read by size through ``cat-file --batch``. No
    delimiter is involved, so no byte a message may hold can end a record
    early or forge the next one."""
    try:
        done = subprocess.run(
            ["git", "-C", cwd, "cat-file", "--batch"],
            input=("\n".join(shas) + "\n").encode(),
            capture_output=True,
            timeout=120,
            check=False,
            env=_GIT_ENV,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refused(f"git cat-file failed: {exc}") from exc
    if done.returncode != 0:
        raise Refused(f"git cat-file failed: {done.stderr.decode(errors='replace')[:300]}")
    out, at, objects = done.stdout, 0, {}
    for sha in shas:
        line_end = out.find(b"\n", at)
        header = out[at:line_end].decode(errors="replace").split()
        if line_end < 0 or len(header) != 3 or header[0] != sha or header[1] != "commit":
            raise Refused(f"git cat-file returned an unexpected record for {sha[:12]}")
        size = int(header[2])
        objects[sha] = out[line_end + 1 : line_end + 1 + size]
        at = line_end + 1 + size + 1
    return objects


def _header_time(line: bytes) -> datetime | None:
    """The epoch time on an ``author``/``committer`` header line, or None when
    it is missing or out of range (never a crash)."""
    parts = line.rsplit(b" ", 2)
    if len(parts) != 3 or not parts[1].isdigit():
        return None
    try:
        return datetime.fromtimestamp(int(parts[1]), UTC)
    except (ValueError, OverflowError, OSError):
        return None


def _log_reflections(cwd: str) -> list[tuple[str, str, str, datetime | None]]:
    """``(sha, "empty"|"content", body, committed_at)`` for each commit whose
    message starts with the header, newest first. Only commits carrying a
    header line are read, and a read that reaches ``LOG_DEPTH`` of them
    refuses rather than silently dropping the older ones. A message in a
    legacy encoding is decoded with replacement, so the grammar refuses it
    rather than crashing."""
    shas = _git(
        cwd,
        "log",
        f"-n{LOG_DEPTH}",
        "--basic-regexp",
        "--grep=^Round-reflection:",
        "--format=%H",
        "HEAD",
    ).split()
    if len(shas) >= LOG_DEPTH:
        raise Refused(f"{LOG_DEPTH} or more reflection commits; the history read would be cut off")
    found = []
    for sha, raw in _commit_objects(cwd, shas).items():
        headers, _, message = raw.partition(b"\n\n")
        body = message.decode("utf-8", errors="replace")
        if not body.startswith("Round-reflection:"):
            continue
        tree, parents, stamps = "", [], {}
        for line in headers.split(b"\n"):
            if line.startswith(b"tree "):
                tree = line[5:].decode()
            elif line.startswith(b"parent "):
                parents.append(line[7:].decode())
            elif line.startswith((b"author ", b"committer ")):
                stamps[line.split(b" ", 1)[0]] = _header_time(line)
                if line.startswith(b"committer "):
                    break  # tree, parents, author, committer come first, in that order
        # The earlier of the two: a rebase or amend rewrites the committer
        # time but keeps the author time, so replaying a reflection after a
        # late audit cannot move its bound forward. Either unreadable: None.
        times = [stamps.get(b"author"), stamps.get(b"committer")]
        committed_at = None if None in times else min(t for t in times if t is not None)
        empty = (
            len(parents) == 1 and tree == _git(cwd, "rev-parse", f"{parents[0]}^{{tree}}").strip()
        )
        found.append((sha, "empty" if empty else "content", body, committed_at))
    return found


def previous_class_labels(cwd: str, prior_heads: Sequence[str]) -> list[str]:
    """Class labels of every earlier round of this PR: the classes of every
    valid empty reflection naming one of ``prior_heads`` (the round heads
    ``review_budget`` reports before the current one). A reflection naming any
    other head is ignored, so a stray or mistyped one cannot stand in for the
    previous round. Drafts never count: an invalid or escalating reflection
    was never accepted as its round's answer."""
    wanted = set(prior_heads)
    classes: list[str] = []
    if not wanted:
        return classes
    for _, kind, body, _ in _log_reflections(cwd):
        parsed = parse(body)
        if kind == "empty" and parsed.ok and not parsed.escalate and parsed.head in wanted:
            classes.extend(c for c in parsed.classes if c not in classes)
    return classes


def _parse_when(raw: object) -> datetime | None:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else None
    if not isinstance(raw, str):
        return None
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else None


#: Commit times have one-second resolution; a file written in the same second
#: as the commit may carry a later fractional mtime.
_COMMIT_CLOCK_SLACK = timedelta(seconds=1)


#: The largest evidence file read. An audit or premise check runs to tens of
#: kilobytes; anything past this is not one, and an unbounded read of a cited
#: path is a hang (a device) or memory exhaustion on the hook path.
EVIDENCE_MAX_BYTES = 1024 * 1024

#: The premise-check block of .claude/docs/premise-check.md ("The output"):
#: a ``Design-premise:`` line at column 0, then optionally one ``Expected before
#: checking:`` line, then the claim lines ``P<n> TRUE|FALSE|UNPROVEN — <claim>``,
#: verdict FIRST (a short parenthesised qualifier may follow it). A claim's
#: verdict sits in a fixed slot, so no word in the claim's own text can be read
#: as it (#3120: the free-text reader misbound them). Each line is matched whole
#: after trailing whitespace is dropped, and a line that OPENS like one of the
#: block's lines but does not match it exactly is refused by its line number,
#: never read as prose (#3127 round 1: a malformed later verdict line and a
#: bodiless claim were silently skipped and accepted).
_PREMISE_VERDICT_LINE_RE = re.compile(
    r"Design-premise:[ \t]+(SOUND-BUT-INFERIOR|SOUND|BROKEN)"
    r"(?:[ \t]+[—–-][ \t]+\S.*)?"
)
#: Any column-0 line that opens like a verdict line, in any case, bold or
#: spacing: it must be a verdict line exactly. An indented or ``>``-quoted one
#: stays prose, which is how the doc says to quote an example.
_PREMISE_VERDICT_PREFIX_RE = re.compile(
    r"[*_]*design[-\u2010\u2011 ]premise[*_]*[ \t]*:", re.IGNORECASE
)
#: A heading or list item that gives a verdict (``## Design-premise: BROKEN``,
#: ``- Design-premise: SOUND (80%)``) is not a sanctioned quote, so it would
#: otherwise contradict the block unseen. A bare section heading such as
#: ``## Design-premise:`` carries no verdict word and stays prose.
_PREMISE_MARKED_VERDICT_RE = re.compile(
    r"(?:#{1,6}|[-*+]|[0-9]+[.)])[ \t]+[*_]*design[-\u2010\u2011 ]premise[*_]*[ \t]*:"
    r".*\b(?:sound|broken)\b",
    re.IGNORECASE,
)
_PREMISE_EXPECTED_LINE_RE = re.compile(r"Expected before checking:[ \t]+\S.*")
_PREMISE_CLAIM_LINE_RE = re.compile(
    r"[ \t]*P([1-9][0-9]?) (TRUE|FALSE|UNPROVEN)(?: \([^)\n]*\))?[ \t]+[\u2014\u2013-][ \t]+\S.*"
)
#: A line that starts like a claim. Within the paragraph that holds a block's
#: claims (up to the next blank line) every such line must be a claim line, or
#: the file is refused by that line: a near-miss or wrapped claim is never
#: silently dropped. Past the blank line it is prose, because a review's own
#: prose starts lines with "P2" too.
_PREMISE_CLAIM_LIKE_RE = re.compile(r"[ \t]*(?:[-*>|][ \t]*)?(?:\*\*)?P[0-9]")
_PREMISE_CLAIM_SHAPE = "'P<n> TRUE|FALSE|UNPROVEN — <claim>' (verdict first, plain text)"


def premise_block(text: str) -> tuple[str | None, dict[str, str], str | None]:
    """``(verdict, {claim number: verdict}, problem)`` from a premise-check
    output in the canonical block. Every column-0 line that opens like
    ``Design-premise:`` must be a verdict line, and they must all agree. The
    claims are the unbroken run of claim lines right under the first verdict
    line that has any (one non-empty ``Expected before checking:`` line may
    sit between); a later block may repeat any of those claims with the same
    verdict (claim text is not compared), never add one or change a verdict,
    and a claim line past the block's paragraph is prose. Any line in the
    paragraph after a verdict line that looks like a claim but is not one is
    refused by its line number. Lines are split on newlines only, as the
    reflection itself is. At least two distinct claims are required."""
    lines = [line.rstrip() for line in text.split("\n")]
    verdicts: set[str] = set()
    first: dict[str, str] | None = None
    for i, line in enumerate(lines):
        if _PREMISE_MARKED_VERDICT_RE.match(line):
            return None, {}, (
                f"line {i + 1} gives a 'Design-premise:' verdict in a heading or list item; "
                "write it as a plain column-0 line, or indent or >-quote it if it is not yours"
            )
        if not _PREMISE_VERDICT_PREFIX_RE.match(line):
            continue
        head = _PREMISE_VERDICT_LINE_RE.fullmatch(line)
        if head is None:
            return None, {}, (
                f"line {i + 1} starts like a 'Design-premise:' line but is not "
                "'Design-premise: SOUND|SOUND-BUT-INFERIOR|BROKEN[ — <qualifier>]' (plain text)"
            )
        verdicts.add(head.group(1))
        if len(verdicts) > 1:
            return None, {}, "it names more than one 'Design-premise:' verdict: " + ", ".join(
                sorted(verdicts)
            )
        run: dict[str, str] = {}
        j = i + 1
        if j < len(lines) and _PREMISE_EXPECTED_LINE_RE.fullmatch(lines[j]):
            j += 1
        while j < len(lines):
            claim = _PREMISE_CLAIM_LINE_RE.fullmatch(lines[j])
            if claim is None:
                break
            number, said = claim.group(1), claim.group(2)
            if run.setdefault(number, said) != said:
                return None, {}, f"line {j + 1}: claim P{number} carries two different verdicts"
            j += 1
        stop = j
        while (
            j < len(lines) and lines[j].strip() and not _PREMISE_VERDICT_PREFIX_RE.match(lines[j])
        ):
            if _PREMISE_CLAIM_LIKE_RE.match(lines[j]):
                if _PREMISE_CLAIM_LINE_RE.fullmatch(lines[j]):
                    return None, {}, (
                        f"line {stop + 1} breaks the claim lines under line {i + 1} (a "
                        "wrapped claim or Expected line, or an empty Expected line), so the "
                        f"claim at line {j + 1} is cut off"
                    )
                return None, {}, f"line {j + 1} looks like a claim but is not {_PREMISE_CLAIM_SHAPE}"
            j += 1
        if first is None:
            first = run or None
        elif any(first.get(number) != said for number, said in run.items()):
            return None, {}, (
                f"line {i + 1}: a repeated 'Design-premise:' block must restate the "
                "first block's claims, never add or change one"
            )
    claims = first or {}
    if not verdicts or len(claims) < 2:
        return (
            None,
            {},
            "it needs the canonical block of .claude/docs/premise-check.md: a "
            "'Design-premise: <verdict>' line at column 0 with at least two "
            "'P<n> TRUE|FALSE|UNPROVEN' claim lines directly under it",
        )
    return next(iter(verdicts)), claims, None


def _read_evidence(path: str, cwd: str) -> tuple[str | None, datetime | None, str | None]:
    """``(text, mtime, None)`` or ``(None, None, why)``. ``~`` expands; a
    relative path resolves against ``cwd``, the repository the check is about,
    never the process's own working directory. Only a regular file of at most
    ``EVIDENCE_MAX_BYTES`` is read, and the type is taken from the opened
    descriptor, so a path swapped for a pipe between a check and the open is
    still refused instead of blocking the read. The mtime is read before the
    content, so a rewrite in that window keeps the older time: a microsecond
    race that only the author of the reflection can run."""
    candidate = Path(os.path.expanduser(path))
    if not candidate.is_absolute():
        candidate = Path(cwd) / candidate
    try:
        fd = os.open(candidate, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        return None, None, f"is unreadable ({exc.strerror})"
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None, None, "is not a regular file"
        with os.fdopen(fd, "rb", closefd=True) as handle:
            fd = -1
            raw = handle.read(EVIDENCE_MAX_BYTES + 1)
    except OSError as exc:
        return None, None, f"is unreadable ({exc.strerror})"
    finally:
        if fd >= 0:
            os.close(fd)
    if len(raw) > EVIDENCE_MAX_BYTES:
        return None, None, f"is larger than {EVIDENCE_MAX_BYTES} bytes"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, None, "is not valid UTF-8"
    return text, datetime.fromtimestamp(info.st_mtime, UTC), None


def evidence_problem(
    path: str,
    *,
    kind: str,
    cwd: str,
    round_started: object,
    made_at: object,
    reflection: Reflection | None = None,
) -> str | None:
    """Why a cited evidence file does not count, or None. ``kind`` is
    ``"audit"`` (the review gate's own adversarial-evidence check) or
    ``"premise"`` (the premise-check block). Local only: the file must be a
    readable regular file, decode, pass its kind's content check, and have
    been last written inside the window from the round's start to the
    reflection's commit (``made_at``), so evidence written or changed after
    the reflection is refused. An unknown end of the window is a refusal,
    never a skipped check. File times can be set by hand, so this binds an
    honest session's evidence to its reflection; it is not proof against
    tampering.

    For premise evidence, a ``reflection`` is bound to the file: its
    ``Premise-evidence`` verdict and its ``Premise:`` must both be the file's
    verdict, and each of its ``Premise-check`` claims must match the file's
    claim of that number. The file decides; the reflection only restates it.
    Claims the reflection does not cite are not compared, but the overall
    verdict always is.
    A file concluding BROKEN therefore forces ``Premise: BROKEN``, which
    escalates."""
    label = {"audit": "Audit-evidence", "premise": "Premise-evidence"}[kind]
    text, mtime, why = _read_evidence(path, cwd)
    if why is not None or text is None or mtime is None:
        return f"{label} {why}"
    if kind == "audit":
        import review_state  # noqa: PLC0415

        ok, why = review_state._evidence_is_adversarial(text)
        if not ok:
            return f"{label} is not an adversarial audit: {why}"
    else:
        verdict, claims, why = premise_block(text)
        if why is not None:
            return f"{label} is not a premise check: {why}"
        if reflection is not None:
            if reflection.premise_evidence_verdict != verdict:
                return (
                    f"{label} concludes {verdict}, but the reflection cites it as "
                    f"{reflection.premise_evidence_verdict}"
                )
            if reflection.verdict != verdict:
                return (
                    f"{label} concludes {verdict}, but the reflection's Premise is "
                    f"{reflection.verdict}"
                )
            for number, said in sorted(reflection.premise_claims.items()):
                found = claims.get(number)
                if found != said:
                    return (
                        f"Premise-check P{number} says {said}, but {label} "
                        + (f"says {found}" if found else "has no claim P" + number)
                    )
    started = _parse_when(round_started)
    if started is None:
        return f"the round's start time is unknown, so the {kind}'s freshness cannot be checked"
    if mtime < started:
        return f"{label} predates this round's first review"
    made = _parse_when(made_at)
    if made is None:
        return f"the reflection's commit time is unknown, so the {kind} cannot be bound to it"
    if mtime > made + _COMMIT_CLOCK_SLACK:
        return f"{label} was written or changed after the reflection was committed"
    return None


def audit_problem(path: str, round_started: object, made_at: object, *, cwd: str) -> str | None:
    """``evidence_problem`` for an ``Audit-evidence`` file."""
    return evidence_problem(
        path, kind="audit", cwd=cwd, round_started=round_started, made_at=made_at
    )


def check(
    body: str,
    *,
    head: str,
    round_number: int,
    gate_lane: bool,
    previous_classes: Iterable[str],
    acceptance: Sequence[str] | None,
    round_started: object,
    made_at: object,
    cwd: str,
) -> Reflection:
    """Every local rule a reflection must meet for this round, in one place.
    ``cwd`` is the repository: a relative evidence path is read from there."""
    parsed = parse(
        body,
        round_number=round_number,
        gate_lane=gate_lane,
        acceptance=acceptance,
        previous_classes=previous_classes,
    )
    if parsed.head != head:
        parsed.problems.append(
            f"the reflection names head {str(parsed.head)[:12]}, not {head[:12]}"
        )
    if parsed.escalate:
        parsed.problems.append("the reflection escalates: it needs an architecture decision")
    for kind, path in (("audit", parsed.audit_evidence), ("premise", parsed.premise_evidence)):
        if path:
            trouble = evidence_problem(
                path,
                kind=kind,
                cwd=cwd,
                round_started=round_started,
                made_at=made_at,
                reflection=parsed,
            )
            if trouble:
                parsed.problems.append(trouble)
    return parsed


def before_any_fix(cwd: str, head: str, sha: str) -> bool:
    """Whether reflection ``sha`` was made before any fix to round ``head``.

    Every commit from the head to the reflection must have exactly one parent
    and that parent's tree: a fix that was later reverted, or a merge, between
    them disqualifies it. A reflection made after a fix is a rationalisation,
    not a reflection. A second reflection for the same round still qualifies.
    """
    code, _, _ = _run(["git", "-C", cwd, "merge-base", "--is-ancestor", head, sha])
    if code != 0:
        return False
    rows = _git(cwd, "log", "--format=%T %P", f"{head}..{sha}").split("\n")
    for row in (r for r in rows if r.strip()):
        tree, *parents = row.split()
        if len(parents) != 1:
            return False
        if tree != _git(cwd, "rev-parse", f"{parents[0]}^{{tree}}").strip():
            return False
    return True


def covered_keys(
    cwd: str,
    head: str,
    *,
    round_number: int,
    gate_lane: bool,
    prior_heads: Sequence[str],
    acceptance: Sequence[str] | None,
    round_started: object,
) -> set[str]:
    """Keys answered at ``head`` by committed reflections. THE coverage check.

    A reflection counts only when it is an EMPTY commit naming this exact head,
    made before any fix to it (``before_any_fix``), and ``check`` passes it:
    the grammar, the round's obligations, the recurring-class rule against
    every earlier round head (``prior_heads``), readable evidence files
    (an adversarial audit, a premise check) written between ``round_started``
    and the reflection's commit where cited or owed, no escalation, and the
    acceptance points (None only when the PR declares none). Every argument is
    required, so no caller gets a weaker check by leaving one out. A
    reflection committed by hand is held to exactly this; there is no weaker
    path. Raises ``Refused`` when git cannot be read: a caller must treat that
    as unknown, never as nothing owed.

    Some inputs are read as they are NOW, not as they were when the reflection
    was committed: the acceptance points (today's PR body), the lane (today's
    changed files) and the cited evidence files (read from disk at each check).
    Each change there can only refuse a reflection that passed, except a PR
    edit that removes an acceptance point, or one that moves the PR across the
    hook-surface boundary (the lanes owe different checks at a round); cite
    evidence from a place nothing prunes (``~/.genesis/review_evidence/``).
    """
    previous = previous_class_labels(cwd, prior_heads)
    covered: set[str] = set()
    for sha, kind, body, committed_at in _log_reflections(cwd):
        if kind != "empty" or not before_any_fix(cwd, head, sha):
            continue
        result = check(
            body,
            head=head,
            round_number=round_number,
            gate_lane=gate_lane,
            previous_classes=previous,
            acceptance=acceptance,
            round_started=round_started,
            made_at=committed_at,
            cwd=cwd,
        )
        if result.ok:
            covered.update(result.keys)
    return covered


def owed_state(
    budget: Mapping[str, Any], covered: Iterable[str], *, now: datetime
) -> dict[str, Any]:
    """What the open round owes, from a ``review_budget`` result. Pure."""
    state: dict[str, Any] = {
        "status": budget.get("status"),
        "round": budget.get("count"),
        "round_state": budget.get("round_state"),
        "head": budget.get("current_head"),
        "gate_lane": bool(budget.get("gate_surface")),
        "reflection_keys": budget.get("reflection_keys", "unknown"),
        "open_keys": list(budget.get("open_keys") or []),
        # None means UNKNOWN, never "nothing owed": an unreadable budget or a
        # round whose findings could not all be keyed owes something unknown.
        "owed": None,
        "round_head": None,
        "round_started": None,
        "settled": False,
        "settle_until": None,
    }
    rounds = budget.get("rounds") or []
    if budget.get("status") == "ok" and rounds:
        state["round_head"] = rounds[-1].get("head")
    if budget.get("status") != "ok":
        return state
    if budget.get("round_state") != "open":
        state["owed"] = []
        return state
    if state["reflection_keys"] == "ok":
        done = set(covered)
        state["owed"] = [k for k in state["open_keys"] if k not in done]
    times = [
        when
        for source in (rounds[-1].get("reviews") or [] if rounds else [])
        if (when := _parse_when(source.get("submitted_at"))) is not None
    ]
    if times:
        started = min(times)
        state["round_started"] = started.isoformat()
        state["settle_until"] = (started + SETTLE).isoformat()
        expected = set(budget.get("expected_reviewers") or [])
        reported = set(budget.get("reviewers_reported") or [])
        state["settled"] = now >= started + SETTLE or bool(expected and expected <= reported)
    return state


def status(cwd: str, *, now: datetime | None = None) -> dict[str, Any]:
    import review_budget  # noqa: PLC0415

    repo, number = pr_identity(cwd)
    budget = review_budget.evaluate_pr(repo, number)
    state = owed_state(budget, (), now=now or datetime.now(UTC))
    if state["status"] == "ok" and state["round_state"] == "open":
        if budget.get("body_changed"):
            raise Refused(
                "the PR body changed between the two reads, so its acceptance "
                "points are not known: run status again"
            )
        # The body comes with the budget read. Only a budget that carries no
        # body at all (an older review_budget) is answered by asking again; a
        # carried body of None (two reads that disagreed) never is.
        if "body" in budget:
            body = budget.get("body")
            if not isinstance(body, str):
                raise Refused("the PR body could not be read, so its acceptance is unknown")
        else:
            body = _pr_meta(repo, number).get("body") or ""
        covered = covered_keys(
            cwd,
            str(state["head"]),
            round_number=int(state["round"] or 0),
            gate_lane=state["gate_lane"],
            prior_heads=[str(r.get("head")) for r in (budget.get("rounds") or [])[:-1]],
            acceptance=acceptance_points(body),
            round_started=state["round_started"],
        )
        if state["reflection_keys"] == "ok":
            state["owed"] = [k for k in state["open_keys"] if k not in covered]
    state.update({"repo": repo, "pr": number, "errors": budget.get("errors", [])})
    return state


# -- CLI ---------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="what the open round owes")
    validate = sub.add_parser("validate", help="check a file against the grammar, offline")
    validate.add_argument("file", type=Path)
    validate.add_argument("--round", type=int, default=None)
    validate.add_argument("--gate", action="store_true", help="the gate lane's obligations")
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            print(json.dumps(status(os.getcwd()), indent=2))
            return 0
        text = args.file.read_bytes().decode("utf-8")
        parsed = parse(text, round_number=args.round, gate_lane=args.gate)
        for problem in parsed.problems:
            print(f"- {problem}")
        return 0 if parsed.ok else 1
    except Exception as exc:  # noqa: BLE001
        # Exit 1 means "the reflection is invalid"; anything that stopped the
        # check from being made is 2, never mistaken for a verdict.
        print(f"review_reflection: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
