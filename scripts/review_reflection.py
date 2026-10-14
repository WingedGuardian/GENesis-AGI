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
``.claude/docs/premise-check.md``: the verdict line at column 0, the
``Expected before checking:`` line directly under it, at least two distinct
``P<n> <verdict>`` claim lines in an unbroken run, verdict first, and then
only ``Effect:``, ``Better shape:``, ``PR-shape:``, ``Persisted-state:`` or
code-fence lines until a blank line or the next ``Design-premise:`` line (the
block's paragraph is closed). At least one block that has claims must carry
the Expected line. Each of
the PR's acceptance points must appear within one Scope line that starts
``covered:``; that is a substring floor for an honest session, not proof
that the line covers the point.

``covered_keys`` is THE coverage check: the commit gate (a later change) calls
it, and it applies every local rule, so a reflection committed by hand is held
to exactly the standard a tool-made one is. Late findings (reviews that opened
no round) are owed from ``REFLECTION_CUTOVER_ISO`` on and credited by
``covered_late_keys``, each under its own round's context; the cutover is unset
until the commit gate enforces reflections, so until then nothing more is owed.
A reflection that cites evidence answers one round: evidence must be written
inside its round's window, so answer each round in its own reflection.

CLI: ``status`` (what the open round owes, whether it has settled; ``--cutover``
reports what late findings would owe, enforcing nothing) and
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
    if not prior_heads:
        return []
    return _class_labels(_log_reflections(cwd), prior_heads)


def _class_labels(
    log: Sequence[tuple[str, str, str, datetime | None]], prior_heads: Sequence[str]
) -> list[str]:
    """``previous_class_labels`` over a log already read, so several rounds
    judged in one call read the history once."""
    wanted = set(prior_heads)
    classes: list[str] = []
    if not wanted:
        return classes
    for _, kind, body, _ in log:
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
#: a ``Design-premise:`` line at column 0, then the required ``Expected before
#: checking:`` line, then the claim lines ``P<n> TRUE|FALSE|UNPROVEN — <claim>``,
#: verdict FIRST (a short parenthesised qualifier may follow it). A claim's
#: verdict sits in a fixed slot, so no word in the claim's own text can be read
#: as it (#3120: the free-text reader misbound them). Each line is matched whole
#: after trailing whitespace is dropped. A line that opens like a verdict line
#: but does not match it is refused by its line number, and so is any line in
#: the block's paragraph that is not one of the block's lines (#3127, #3238).
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
#: A heading or list item labelled as the verdict (``## Design-premise: BROKEN``,
#: ``- Design-premise: SOUND (80%)``) is not a sanctioned quote, so it would
#: otherwise contradict the block unseen. It is refused when the text after
#: its colon carries an UPPERCASE verdict token, the grammar's own spelling; a
#: lowercase question or description ("what would make this sound?") and a bare
#: section heading stay prose. ``>`` is not a marker here: it is the doc's quote.
_PREMISE_MARKED_VERDICT_RE = re.compile(
    r"(?:#{1,6}|[-*+|]|[0-9]+[.)])[ \t]+[*_]*(?i:design[-\u2010\u2011 ]premise)[*_]*[ \t]*:(.*)"
)
_PREMISE_VERDICT_TOKEN_RE = re.compile(r"\b(?:SOUND|BROKEN)\b")
_PREMISE_EXPECTED_LINE_RE = re.compile(r"Expected before checking:[ \t]+\S.*")
_PREMISE_CLAIM_LINE_RE = re.compile(
    r"[ \t]*P([1-9][0-9]?) (TRUE|FALSE|UNPROVEN)(?: \([^)\n]*\))?[ \t]+[\u2014\u2013-][ \t]+\S.*"
)
#: After the claims, the block paragraph (up to the next blank line or verdict
#: line) may hold only the doc's trailer lines (and ``Persisted-state:``, which
#: the architect agent emits right after them) and a code fence. Every other
#: line in it is refused by its line number: the paragraph is closed, so no
#: near-miss spelling of a claim or Expected line can be dropped as prose (#3238).
_PREMISE_TRAILER_RE = re.compile(
    r"(?:Effect|Better shape|PR-shape|Persisted-state):[ \t]+\S.*|(?:```|~~~)\S*"
)


def premise_block(text: str) -> tuple[str | None, dict[str, str], str | None]:
    """``(verdict, {claim number: verdict}, problem)`` from a premise-check
    output in the canonical block. Every column-0 line that opens like
    ``Design-premise:`` must be a verdict line, and they must all agree; a
    heading or list item labelled as the verdict that carries an uppercase
    verdict token is refused. The block paragraph runs from a verdict line to
    the next blank line or verdict line and is CLOSED: it holds the verdict
    line, the ``Expected before checking:`` line directly under it,
    the unbroken run of claim lines, then only ``Effect:``, ``Better shape:``,
    ``PR-shape:``, ``Persisted-state:`` and code-fence lines. Any other line in it is refused by its
    line number. The claims are those of the first block that has any; a later
    block may repeat any of them with the same verdict (claim text is not
    compared), never add one or change a verdict. At least one block with
    claims must carry the Expected line (a restating summary need not), and at least two distinct claims are required. Lines are
    split on newlines only, as the reflection itself is. Outside a block
    paragraph, text is prose and is not checked."""
    lines = [line.rstrip() for line in text.split("\n")]
    verdicts: set[str] = set()
    first: dict[str, str] | None = None
    has_expected = False
    for i, line in enumerate(lines):
        marked = _PREMISE_MARKED_VERDICT_RE.fullmatch(line)
        if marked and _PREMISE_VERDICT_TOKEN_RE.search(marked.group(1)):
            return (
                None,
                {},
                (
                    f"line {i + 1} gives a 'Design-premise:' verdict in a heading or list item; "
                    "write it as a plain column-0 line, or indent or >-quote it if it is not yours"
                ),
            )
        if not _PREMISE_VERDICT_PREFIX_RE.match(line):
            continue
        head = _PREMISE_VERDICT_LINE_RE.fullmatch(line)
        if head is None:
            return (
                None,
                {},
                (
                    f"line {i + 1} starts like a 'Design-premise:' line but is not "
                    "'Design-premise: SOUND|SOUND-BUT-INFERIOR|BROKEN[ — <qualifier>]' (plain text)"
                ),
            )
        verdicts.add(head.group(1))
        if len(verdicts) > 1:
            return (
                None,
                {},
                "it names more than one 'Design-premise:' verdict: " + ", ".join(sorted(verdicts)),
            )
        run: dict[str, str] = {}
        j = i + 1
        expected_here = j < len(lines) and bool(_PREMISE_EXPECTED_LINE_RE.fullmatch(lines[j]))
        if expected_here:
            j += 1
        while j < len(lines):
            claim = _PREMISE_CLAIM_LINE_RE.fullmatch(lines[j])
            if claim is None:
                break
            number, said = claim.group(1), claim.group(2)
            if run.setdefault(number, said) != said:
                return None, {}, f"line {j + 1}: claim P{number} carries two different verdicts"
            j += 1
        while (
            j < len(lines) and lines[j].strip() and not _PREMISE_VERDICT_PREFIX_RE.match(lines[j])
        ):
            if not _PREMISE_TRAILER_RE.fullmatch(lines[j]):
                return (
                    None,
                    {},
                    (
                        f"line {j + 1} is inside the premise block that starts at line {i + 1} but "
                        "is not one of its lines: a claim "
                        "'P<n> TRUE|FALSE|UNPROVEN — <claim>' (one line, not wrapped), the "
                        "Expected line directly under the verdict, Effect:, Better shape:, "
                        "PR-shape:, Persisted-state: or a code fence. Leave a blank line before any other text"
                    ),
                )
            j += 1
        has_expected = has_expected or (expected_here and bool(run))
        if first is None:
            first = run or None
        elif any(first.get(number) != said for number, said in run.items()):
            return (
                None,
                {},
                (
                    f"line {i + 1}: a repeated 'Design-premise:' block must restate the "
                    "first block's claims, never add or change one"
                ),
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
    if not has_expected:
        return (
            None,
            {},
            (
                "the block needs its 'Expected before checking: <one line>' line directly "
                "under the 'Design-premise:' line (.claude/docs/premise-check.md)"
            ),
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
                    return f"Premise-check P{number} says {said}, but {label} " + (
                        f"says {found}" if found else "has no claim P" + number
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


# -- late findings -------------------------------------------------------------
#
# A late review (one that lands on a commit after one of its descendants was
# already reviewed) opens no round, so its keys are not in ``open_keys``;
# ``review_budget`` lists it in ``findings`` with the round it belongs to
# (``round_head``). Its keys are owed from the reflection cutover on (owner
# ruling a222b408), and each is credited only under ITS OWN round's context,
# so a reflection on a later head cannot clear an earlier round's keys
# without that round's evidence. The open round is never judged here:
# ``covered_keys`` keeps it exactly as before.

#: When late findings start being owed. None until the commit gate enforces
#: reflections (B1b-1b sets it at its own merge), so no debt appears before
#: anything enforces it (owner ruling 50cc9dec); with None, owed is exactly
#: the open round's.
REFLECTION_CUTOVER_ISO: str | None = None


@dataclass(frozen=True)
class RoundContext:
    """What a round owes, as of a time. ``round_number`` 0 is a late review
    with no earlier round: nothing more than the grammar is owed (owner,
    2026-10-12: a late-only answer owes no new audit or premise check)."""

    round_number: int
    round_started: str | None
    prior_heads: tuple[str, ...]


def _ancestry(budget: Mapping[str, Any]) -> tuple[set[str], Any]:
    """The PR's commits and ``ancestors(sha)``: the strict ancestors inside the
    PR, or None when they are unknown (a commit outside the graph, or one whose
    parents were not read). Unknown ancestry never credits a key."""
    parents: dict[str, list[str] | None] = {}
    for row in budget.get("commits") or []:
        if isinstance(row, Mapping) and isinstance(row.get("sha"), str):
            given = row.get("parents")
            parents[row["sha"]] = list(given) if isinstance(given, list) else None
    memo: dict[str, frozenset[str] | None] = {}

    def ancestors(sha: str) -> frozenset[str] | None:
        if sha in memo:
            return memo[sha]
        if sha not in parents:
            return None
        seen: set[str] = set()
        stack = [sha]
        while stack:
            given = parents[stack.pop()]
            if given is None:
                memo[sha] = None
                return None
            for parent in given:
                # A parent outside the PR is the base: the walk stops there.
                if parent in parents and parent not in seen:
                    seen.add(parent)
                    stack.append(parent)
        memo[sha] = frozenset(seen)
        return memo[sha]

    return set(parents), ancestors


def _descends(ancestors: Any, sha: str, base: str) -> bool:
    """Whether ``sha`` is ``base`` or a known descendant of it."""
    if sha == base:
        return True
    found = ancestors(sha)
    return found is not None and base in found


def _round_order(budget: Mapping[str, Any]) -> list[tuple[str, datetime]] | None:
    """Round heads in the order they OPENED, each with its opening time: the
    earliest review that opened it, or the earliest review of its head
    before ``review_budget.ROUND_RULE_CUTOVER_ISO`` (``marks``: that is what
    made it a legacy round). Never the ``rounds`` list order, which is by commit date
    (#3302), and never a guess: a round with no known time makes the whole
    order unknown (None). Ties break by head."""
    import review_budget  # noqa: PLC0415

    cutover = _parse_when(review_budget.ROUND_RULE_CUTOVER_ISO)
    legacy_marks: dict[str, list[datetime]] = {}
    for mark in budget.get("marks") or []:
        when = _parse_when(mark.get("at")) if isinstance(mark, Mapping) else None
        # A mark before the round-rule cutover is what made a head a legacy
        # round; its earliest one is when that round opened, even when the
        # head also drew a post-cutover review later.
        if when is not None and cutover is not None and when < cutover:
            legacy_marks.setdefault(str(mark.get("head")), []).append(when)
    order = []
    for entry in budget.get("rounds") or []:
        head = str(entry.get("head"))
        times = [
            when
            for review in entry.get("reviews") or []
            if (when := _parse_when(review.get("submitted_at"))) is not None
        ] + legacy_marks.get(head, [])
        if not times:
            return None
        order.append((head, min(times)))
    return sorted(order, key=lambda item: (item[1], item[0]))


def round_context(budget: Mapping[str, Any], round_head: str, at: datetime) -> RoundContext | None:
    """Round ``round_head``'s context as of ``at``: its number among the rounds
    opened by then, its start, and the heads of the rounds before it. None when
    it is not a round head, it had not opened by ``at``, or the rounds' order
    is unknown."""
    order = _round_order(budget)
    if order is None:
        return None
    heads = [head for head, when in order if when <= at]
    if round_head not in heads:
        return None
    index = heads.index(round_head)
    return RoundContext(index + 1, order[index][1].isoformat(), tuple(heads[:index]))


def late_findings(
    budget: Mapping[str, Any], cutover: str | None, fix_time: Any = None
) -> list[dict[str, Any]] | None:
    """The finding keys owed besides the open round, one entry per key, from
    reviews submitted at or after ``cutover``. ``[]`` when ``cutover`` is None.

    A review that opened its round was the open round's to answer while it was
    open, and the open-round check enforced it then; it is owed here only when
    it arrived AFTER a fix to its head existed (owner ruling 2026-10-13: the
    after-fix review). ``fix_time(head)`` gives that fix's author time, or None
    when nothing has been fixed since the head. A late review (one that opened
    no round) was never the open round's, so it is always owed. A review of a
    head outside the commit graph that opened its round cannot be placed
    against a fix and is not owed (stated residual).

    None (unknown) when such a review has an unreadable time or a key that
    could not be derived, or a fix time is needed and ``fix_time`` is None. A
    review before the cutover never makes it unknown."""
    if cutover is None:
        return []
    start = _parse_when(cutover)
    if start is None:
        return None
    own = set(budget.get("open_keys") or []) if budget.get("round_state") == "open" else set()
    opening = {
        key
        for entry in budget.get("rounds") or []
        for review in entry.get("reviews") or []
        for key in review.get("finding_keys") or []
    }
    in_graph = {row.get("sha") for row in budget.get("commits") or [] if isinstance(row, Mapping)}
    found: dict[str, dict[str, Any]] = {}
    for item in budget.get("findings") or []:
        when = _parse_when(item.get("submitted_at"))
        if when is None:
            return None
        if when < start:
            continue
        keys = item.get("keys") or []
        if any(not isinstance(key, str) for key in keys):
            return None
        head = str(item.get("head"))
        if head not in in_graph:
            # A force-push dropped it (owner ruling 2026-10-14).
            continue
        if keys and set(keys) <= opening:
            if fix_time is None:
                return None
            fixed = fix_time(head)
            if fixed is None or when <= fixed:
                continue
        for key in keys:
            if key not in own and key not in found:
                found[key] = {**item, "key": key, "at": when}
    # Every owed key must have a round to be answered under. One that cannot
    # be placed (unknown round order, unknown ancestry) makes owed unknown,
    # never a key no reflection could ever clear.
    _, ancestors = _ancestry(budget)
    latest = datetime.max.replace(tzinfo=UTC)
    if any(_contexts(budget, item, latest, ancestors) is None for item in found.values()):
        return None
    return list(found.values())


def _contexts(
    budget: Mapping[str, Any], item: Mapping[str, Any], at: datetime, ancestors: Any
) -> list[RoundContext] | None:
    """Every context a late key must be answered under. Its own round when it
    has one. With none (``round_head`` null): round 0 when no earlier round is
    its ancestor, else every candidate round (the two sides of a merge), all of
    which must pass. None when its round cannot be placed."""
    if item.get("round_head"):
        context = round_context(budget, str(item["round_head"]), at)
        return [context] if context else None
    found = ancestors(str(item.get("head")))
    if found is None:
        return None
    order = _round_order(budget)
    if order is None:
        return None
    older = {head for head, _ in order if head in found}
    if not older:
        return [RoundContext(0, item["at"].isoformat(), ())]
    # The newest by ancestry: no other candidate descends from it. Two
    # incomparable ones (the two sides of a merge) must both pass; a candidate
    # with unknown ancestry places nothing.
    if any(ancestors(head) is None for head in older):
        return None
    newest = [
        head for head in older if not any(head in ancestors(other) for other in older - {head})
    ]
    contexts = [round_context(budget, head, at) for head in sorted(newest)]
    return None if any(c is None for c in contexts) else contexts  # type: ignore[return-value]


def _fix_times(cwd: str, base: str, head: str) -> list[datetime]:
    """The author time of every fix on the LINE from ``base`` to ``head``
    (``--ancestry-path``: descendants of ``base`` that ``head`` reaches, so a
    side branch's own commits drop out and the merge that brought it in
    stays): each commit that is a merge or changes the tree (an empty
    reflection commit is no fix). The owe rule (earliest fix to the current
    head) and the credit rule (latest fix to the reflection's head) read the
    same line, so a key owed is answerable until the next fix lands. A fix
    committed while a key is owed leaves it owed for good: the commit gate
    that sets the cutover must stop that commit (stated for that PR). The
    AUTHOR time, the earlier of the two: amending a fix rewrites only its
    committer time (owner ruling 2026-10-13). Raises ``Refused`` when
    git cannot say."""
    code, out, _ = _run(
        ["git", "-C", cwd, "log", "--ancestry-path", "--format=%H %T %P|%at", f"{base}..{head}"]
    )
    if code != 0:
        raise Refused(f"git log {base[:12]}..{head[:12]} failed")
    times = []
    for row in out.split("\n"):
        if not row.strip():
            continue
        names, _, stamp = row.partition("|")
        _, tree, *parents = names.split()
        if len(parents) == 1 and tree == _git(cwd, "rev-parse", f"{parents[0]}^{{tree}}").strip():
            continue
        if not stamp.strip().isdigit():
            raise Refused(f"a fix after {base[:12]} has no readable author time")
        times.append(datetime.fromtimestamp(int(stamp), UTC))
    return times


def fix_time_reader(cwd: str, current: str) -> Any:
    """``fix_time`` for ``late_findings``: the earliest fix after a head on the
    way to ``current``, read from local git once per head."""
    memo: dict[str, datetime | None] = {}

    def fix_time(head: str) -> datetime | None:
        if head not in memo:
            times = _fix_times(cwd, head, current)
            memo[head] = min(times) if times else None
        return memo[head]

    return fix_time


def covered_late_keys(
    cwd: str,
    budget: Mapping[str, Any],
    late: Sequence[Mapping[str, Any]],
    *,
    gate_lane: bool,
    acceptance: Sequence[str] | None,
    deadline: float | None = None,
) -> set[str]:
    """Late keys answered by committed reflections. A reflection R naming head
    N, committed at T, credits late key k (reviewed on head H at time S) only
    when ALL hold:

    - R is an empty commit made before any fix to N (``before_any_fix``);
    - N is the current head or in its history, and N is H or descends from it
      (from the budget's commit graph; unknown ancestry never credits). A head
      that left the graph (a force-push of the PR branch) is never owed here
      (owner ruling 2026-10-14: a force-push drops late debt; the default
      branch itself refuses force-pushes by ruleset);
    - S is no later than T: an answer cannot predate its finding;
    - when N is not H, S is after the LATEST fix on the way from H to N (author
      time). A review that arrived before a fix was owed before it, and a
      reflection made after that fix is a rationalisation, not a reflection;
    - ``check`` passes R under k's own round's context (every candidate round
      when k's round is ambiguous): that round's number, its earlier rounds'
      classes, the PR's acceptance points, and evidence written from that
      round's start to T. Round 0 (a late review with no earlier round) owes
      the grammar only: no acceptance points, no evidence window.

    Returns only keys from ``late``; the open round's coverage is never mixed
    in (``owed_state`` keeps the two apart).

    ``deadline`` is a ``time.monotonic()`` bound; past it the check refuses.
    Raises ``Refused`` when git cannot be read: unknown, never nothing owed."""
    import time  # noqa: PLC0415

    current = budget.get("current_head")
    in_graph, ancestors = _ancestry(budget)
    log = _log_reflections(cwd)
    labels: dict[tuple[tuple[str, ...], datetime], list[str]] = {}
    checked: dict[tuple[str, RoundContext], bool] = {}
    fix_times: dict[tuple[str, str], datetime | None] = {}
    covered: set[str] = set()
    for sha, kind, body, made_at in log:
        if deadline is not None and time.monotonic() > deadline:
            raise Refused("the late-findings check ran out of time")
        parsed = parse(body)
        head = parsed.head
        if kind != "empty" or made_at is None or not head:
            continue
        if not (head == current or _descends(ancestors, str(current), head)):
            continue
        if not before_any_fix(cwd, head, sha):
            continue
        for item in late:
            key = item["key"]
            if key in covered or key not in parsed.keys:
                continue
            reviewed = str(item.get("head"))
            if reviewed not in in_graph or not _descends(ancestors, head, reviewed):
                continue
            if item["at"] > made_at + _COMMIT_CLOCK_SLACK:
                continue
            if head != reviewed:
                if (reviewed, head) not in fix_times:
                    found = _fix_times(cwd, reviewed, head)
                    fix_times[(reviewed, head)] = max(found) if found else None
                fixed = fix_times[(reviewed, head)]
                if fixed is not None and item["at"] <= fixed:
                    continue
            contexts = _contexts(budget, item, made_at, ancestors)
            if not contexts:
                continue
            passed = True
            for context in contexts:
                if (sha, context) not in checked and context.round_number == 0:
                    checked[(sha, context)] = parsed.ok and not parsed.escalate
                if (sha, context) not in checked:
                    if (context.prior_heads, made_at) not in labels:
                        earlier = [row for row in log if row[3] is not None and row[3] <= made_at]
                        labels[(context.prior_heads, made_at)] = _class_labels(
                            earlier, context.prior_heads
                        )
                    checked[(sha, context)] = check(
                        body,
                        head=head,
                        round_number=context.round_number,
                        gate_lane=gate_lane,
                        previous_classes=labels[(context.prior_heads, made_at)],
                        acceptance=acceptance,
                        round_started=context.round_started,
                        made_at=made_at,
                        cwd=cwd,
                    ).ok
                passed = passed and checked[(sha, context)]
            if passed:
                covered.add(key)
    return covered


def owed_state(
    budget: Mapping[str, Any],
    covered: Iterable[str],
    *,
    now: datetime,
    cutover: str | None = None,
    fix_time: Any = None,
    late_covered: Iterable[str] = (),
) -> dict[str, Any]:
    """What is owed, from a ``review_budget`` result: the open round's keys
    less ``covered`` (``covered_keys``), plus the late keys from ``cutover`` on
    (``late_findings``) less ``late_covered`` (``covered_late_keys``). The two
    are subtracted apart, so a reflection judged under the open round never
    clears a late key, which only its own round's context can. With
    ``cutover`` None that is exactly the open round, and pure; a ``fix_time``
    reader (``fix_time_reader``) reads local git and may raise ``Refused``."""
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
        "cutover": cutover,
        "late_keys": [],
    }
    rounds = budget.get("rounds") or []
    if budget.get("status") == "ok" and rounds:
        state["round_head"] = rounds[-1].get("head")
    if budget.get("status") != "ok":
        return state
    done = set(covered)
    late_done = set(late_covered)
    late = late_findings(budget, cutover, fix_time)
    state["late_keys"] = None if late is None else [item["key"] for item in late]
    late_owed = None if late is None else [k for k in state["late_keys"] if k not in late_done]
    if budget.get("round_state") != "open":
        state["owed"] = late_owed
        # A finished round's reviews have all landed: once late findings are
        # read (a cutover is set) and known, its evidence is final, owed or
        # not. With no cutover this is main's answer, False.
        state["settled"] = cutover is not None and late_owed is not None
        return state
    if state["reflection_keys"] == "ok" and late_owed is not None:
        state["owed"] = [k for k in state["open_keys"] if k not in done] + late_owed
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


def _acceptance(budget: Mapping[str, Any], repo: str, number: int) -> list[str] | None:
    """The PR's acceptance points, from the body the budget read carried."""
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
    return acceptance_points(body)


_MODULE_CUTOVER = object()


def status(
    cwd: str, *, now: datetime | None = None, cutover: Any = _MODULE_CUTOVER
) -> dict[str, Any]:
    """What is owed now. ``cutover`` defaults to ``REFLECTION_CUTOVER_ISO`` as
    it stands at the call; any other value is a report only: it shows what
    late findings would owe, and enforces nothing."""
    import review_budget  # noqa: PLC0415

    if cutover is _MODULE_CUTOVER:
        cutover = REFLECTION_CUTOVER_ISO
    repo, number = pr_identity(cwd)
    budget = review_budget.evaluate_pr(repo, number)
    fix_time = None
    if cutover is not None and budget.get("status") == "ok":
        fix_time = fix_time_reader(cwd, str(budget.get("current_head")))
    state = owed_state(budget, (), now=now or datetime.now(UTC), cutover=cutover, fix_time=fix_time)
    is_open = state["status"] == "ok" and state["round_state"] == "open"
    late = late_findings(budget, cutover, fix_time) if state["status"] == "ok" else []
    if is_open or late:
        acceptance = _acceptance(budget, repo, number)
        covered: set[str] = set()
        late_covered: set[str] = set()
        if is_open:
            covered = covered_keys(
                cwd,
                str(state["head"]),
                round_number=int(state["round"] or 0),
                gate_lane=state["gate_lane"],
                prior_heads=[str(r.get("head")) for r in (budget.get("rounds") or [])[:-1]],
                acceptance=acceptance,
                round_started=state["round_started"],
            )
        if late:
            late_covered = covered_late_keys(
                cwd, budget, late, gate_lane=state["gate_lane"], acceptance=acceptance
            )
        state = owed_state(
            budget,
            covered,
            now=now or datetime.now(UTC),
            cutover=cutover,
            fix_time=fix_time,
            late_covered=late_covered,
        )
    state.update({"repo": repo, "pr": number, "errors": budget.get("errors", [])})
    return state


# -- CLI ---------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    status_cmd = sub.add_parser("status", help="what the open round owes")
    status_cmd.add_argument(
        "--cutover",
        default=None,
        help="report what late findings from this ISO time on would owe (enforces nothing)",
    )
    validate = sub.add_parser("validate", help="check a file against the grammar, offline")
    validate.add_argument("file", type=Path)
    validate.add_argument("--round", type=int, default=None)
    validate.add_argument("--gate", action="store_true", help="the gate lane's obligations")
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            report = (
                status(os.getcwd(), cutover=args.cutover) if args.cutover else status(os.getcwd())
            )
            print(json.dumps(report, indent=2, default=str))
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
