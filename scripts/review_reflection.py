#!/usr/bin/env python3
"""The round reflection: what a session writes before a review round's first fix.

A review ROUND is a commit head that drew findings (``review_budget``). Before
fixing anything in an open round, the session reads the round as data and
commits a reflection that answers for every finding in it: how the findings
distribute, whether the change's premise holds, how the PR's scope fares, the
decision, and one disposition per finding (fix now, or file it). The
reflection is an EMPTY commit whose message is the reflection, so no reviewer
is ever triggered by it.

This module is the format and the coverage check. ``covered_keys`` is THE
check: the commit gate (a later change) calls it, and it applies every local
rule, so a reflection committed by hand is held to exactly the standard a
tool-made one is. Writing helpers (template, commit, mirror) are a separate
change.

CLI:

``status``    what the open round on the current branch's PR owes, and
              whether the round has settled (30 minutes since its first
              review, or every expected reviewer has reported).
``validate``  checks a reflection file's structure, offline.

Keys come from ``review_budget``: ``c<id>`` an inline finding, ``r<id>:<n>`` a
review's ``n`` body findings answered as one, ``i<id>`` a Codex findings
comment.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

SETTLE = timedelta(minutes=30)
MIN_CHARS = 400
LOG_DEPTH = 300
IMPOSSIBLE_PROMPT = "What change would make this whole class impossible?"
#: Every template placeholder starts with this, and none may survive into a
#: committed reflection. Distinct from ordinary angle-bracket prose ("r<id>").
FILL = "<FILL:"

_KEY = r"(?:c\d{1,20}|r\d{1,20}:\d{1,9}|i\d{1,20})"
HEADER_RE = re.compile(rf"^Round-reflection: keys=({_KEY}(?:,{_KEY})*) head=([0-9a-f]{{40}})$")
_SECTION_RE = re.compile(r"^##[ \t]+(\S.*?)[ \t]*$")
_FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})")
_CLASS_ROW_RE = re.compile(r"^[ \t]*[-*][ \t]+([^:\n]{2,80}?)[ \t]*:[ \t]*(\d{1,4})\b")
_VERDICT_RE = re.compile(
    r"^Design-premise:[ \t]*(LEAD|SOUND-BUT-INFERIOR|SOUND|BROKEN|SUSPECT)\b[ \t]*(\S.*)?$"
)
_DECISION_RE = re.compile(
    r"^Decision:[ \t]*(fix-instances|close-class|rework|send-back)\b[ \t]*(\S.*)?$"
)
_LABELLED_RE = re.compile(r"^(Design-premise|Decision):")
_DISPOSITION_RE = re.compile(
    rf"^[ \t]*[-*][ \t]+({_KEY})[ \t]*:[ \t]*"
    r"(fix-now[ \t]*\(test[ \t]*[1-4](?:[ \t]*,[ \t]*[1-4])*\)"
    r"|file[ \t]*\(fails all four;[ \t]*#\d+\))"
)
_ANY_DISPOSITION_RE = re.compile(rf"^[ \t]*[-*][ \t]+({_KEY})[ \t]*:")
_AUDIT_RE = re.compile(r"^Audit-evidence:[ \t]*(\S+)")
_PREMISE_LINE_RE = re.compile(r"^[ \t]*[-*]?[ \t]*P\d+\b.*\b(TRUE|FALSE|UNPROVEN)\b")
#: Escalation is read from the RAW text, any line, any indent, fenced or not:
#: a flag is honoured wherever it was put, never lost to placement.
_ESCALATION_RE = re.compile(
    r"^[ \t>*-]*(?:Design-premise:[ \t]*(?:BROKEN|SUSPECT)\b|Escalate:[ \t]*yes\b)",
    re.IGNORECASE | re.MULTILINE,
)
_WS_RE = re.compile(r"\s+")
REQUIRED_SECTIONS = ("Distribution", "Premise", "Scope", "Decision", "Dispositions")


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
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def obligations(round_number: int, gate_lane: bool) -> tuple[bool, bool]:
    """``(audit, premise)`` owed by the reflection of this round.

    The review ladder (owner rulings 2026-09-30): the gate lane runs a class
    audit at round 1 and the premise check at round 2, its terminal round; the
    ordinary lane audits at round 2 and checks the premise from round 3. The
    audit belongs to ITS round; the premise check stays owed after its round.
    """
    if gate_lane:
        return round_number == 1, round_number >= 2
    return round_number == 2, round_number >= 3


def _sections(lines: Sequence[str]) -> dict[str, list[str]]:
    """Lines under each ``## heading``. A fenced block is quotation, not
    structure: its lines belong to no section, so neither a heading nor a
    disposition inside one counts."""
    out: dict[str, list[str]] = {}
    current: str | None = None
    fence: str | None = None
    for line in lines:
        opener = _FENCE_RE.match(line)
        if opener:
            mark = opener.group(1)[0]
            fence = None if fence == mark else (fence or mark)
            continue
        if fence:
            continue
        match = _SECTION_RE.match(line)
        if match:
            current = match.group(1)
            out.setdefault(current, [])
        elif current is not None:
            out[current].append(line)
    return out


def _normalize(text: str) -> str:
    return _WS_RE.sub(" ", text).strip().lower()


def _one(lines: Sequence[str], pattern: re.Pattern[str], label: str, problems: list[str]):
    """The single line ``pattern`` matches in ``lines``; more than one is refused,
    so an early line can never hide a later one."""
    hits = [m for line in lines if (m := pattern.match(line))]
    if len(hits) > 1:
        problems.append(f"'{label}' appears more than once")
    return hits[0] if len(hits) == 1 else None


def parse(
    text: str,
    *,
    round_number: int | None = None,
    gate_lane: bool = False,
    acceptance: Sequence[str] | None = None,
    previous_classes: Iterable[str] = (),
) -> Reflection:
    """Structure-check a reflection, offline. Never raises.

    ``round_number`` None checks only what any reader can: the header on the
    FIRST line, one disposition per header key and nothing else, the length,
    and no unfilled placeholder. Given a round, it also checks every required
    part, the round's obligations and the recurring-class rule; given
    ``acceptance`` bullets, each must appear in the Scope section.
    """
    result = Reflection()
    lines = text.splitlines()
    header = HEADER_RE.match(lines[0]) if lines else None
    if header is None:
        result.problems.append(
            "the FIRST line must be exactly "
            "'Round-reflection: keys=<key>[,<key>...] head=<40-hex sha>'"
        )
    else:
        result.keys = header.group(1).split(",")
        result.head = header.group(2)
        if len(set(result.keys)) != len(result.keys):
            result.problems.append("the header names a key twice")
    if sum(1 for line in lines if line.startswith("Round-reflection:")) > 1:
        result.problems.append("a reflection has exactly one header")
    if len(text.strip()) < MIN_CHARS:
        result.problems.append(f"a reflection is at least {MIN_CHARS} characters")
    if FILL in text:
        result.problems.append(f"unfilled template placeholder(s) remain ('{FILL} ...>')")
    result.escalate = bool(_ESCALATION_RE.search(text))

    sections = _sections(lines)
    disposed: dict[str, str] = {}
    for line in sections.get("Dispositions", []):
        loose = _ANY_DISPOSITION_RE.match(line)
        if loose is None:
            continue
        key = loose.group(1)
        if key in disposed:
            result.problems.append(f"{key} has two dispositions")
            continue
        strict = _DISPOSITION_RE.match(line)
        if strict is None:
            result.problems.append(
                f"{key}: a disposition is 'fix-now (test N)' (N in 1-4) "
                "or 'file (fails all four; #N)'"
            )
            disposed[key] = ""
            continue
        disposed[key] = strict.group(2)
    if result.keys:
        missing = [k for k in result.keys if k not in disposed]
        extra = [k for k in disposed if k not in result.keys]
        if missing:
            result.problems.append("no disposition for: " + ", ".join(missing))
        if extra:
            result.problems.append(
                "dispositions for keys the header does not name: " + ", ".join(extra)
            )

    # Labelled lines are read from THEIR section only, and exactly once, so a
    # decoy line elsewhere neither satisfies nor hides the real one.
    premise_lines = sections.get("Premise", [])
    decision_lines = sections.get("Decision", [])
    verdict = _one(premise_lines, _VERDICT_RE, "Design-premise", result.problems)
    decision = _one(decision_lines, _DECISION_RE, "Decision", result.problems)
    for name, body in sections.items():
        for line in body:
            label = _LABELLED_RE.match(line)
            if label and not (
                (label.group(1) == "Design-premise" and name == "Premise")
                or (label.group(1) == "Decision" and name == "Decision")
            ):
                result.problems.append(f"'{label.group(1)}:' belongs only in its own section")
    result.verdict = verdict.group(1) if verdict else None
    result.decision = decision.group(1) if decision else None
    for line in sections.get("Distribution", []):
        row = _CLASS_ROW_RE.match(line)
        if row:
            result.classes.append(_normalize(row.group(1)))
    all_lines = [line for body in sections.values() for line in body]
    audit = next((m for line in all_lines if (m := _AUDIT_RE.match(line))), None)
    result.audit_evidence = audit.group(1) if audit else None

    if round_number is None:
        return result

    for name in REQUIRED_SECTIONS:
        if name not in sections:
            result.problems.append(f"missing section '## {name}'")
    if not result.classes:
        result.problems.append("Distribution needs at least one '- <class>: <count>' row")
    if verdict is None or not (verdict.group(2) or "").strip():
        result.problems.append(
            "Premise needs 'Design-premise: LEAD|SOUND|SOUND-BUT-INFERIOR|BROKEN|SUSPECT <why>'"
        )
    elif round_number >= 2 and result.verdict == "LEAD":
        result.problems.append(
            "from round 2 the premise verdict is SOUND, SOUND-BUT-INFERIOR or BROKEN"
        )
    if not _answer_to(premise_lines, IMPOSSIBLE_PROMPT):
        result.problems.append(
            f"answer '{IMPOSSIBLE_PROMPT}' in Premise (on that line or the next)"
        )
    if decision is None or not (decision.group(2) or "").strip():
        result.problems.append(
            "Decision needs 'Decision: fix-instances|close-class|rework|send-back <why>'"
        )
    scope_lines = sections.get("Scope", [])
    if not [line for line in scope_lines if re.match(r"^[ \t]*[-*][ \t]+\S", line)]:
        result.problems.append("Scope needs at least one '- ' entry")
    scope_text = _normalize("\n".join(scope_lines))
    for item in acceptance or ():
        wanted = _normalize(item)
        if wanted and wanted not in scope_text:
            result.problems.append(f"Scope does not map the acceptance point: {item[:80]}")

    audit_owed, premise_owed = obligations(round_number, gate_lane)
    if audit_owed and not result.audit_evidence:
        result.problems.append(
            f"round {round_number} cites a fresh-context audit: 'Audit-evidence: <path> <verdict>'"
        )
    if premise_owed:
        checks = [ln for ln in sections.get("Premise-check", []) if _PREMISE_LINE_RE.match(ln)]
        if len(checks) < 2:
            result.problems.append(
                f"round {round_number} carries an independent premise check: a "
                "'## Premise-check' section with at least two "
                "'P<n> ... TRUE|FALSE|UNPROVEN' lines"
            )
    recurring = sorted(set(result.classes) & {_normalize(c) for c in previous_classes})
    if recurring and result.decision == "fix-instances":
        result.problems.append(
            "a class recurs from the previous round's reflection ("
            + ", ".join(recurring)
            + "): the decision is close-class, rework or send-back, never fix-instances"
        )
    return result


def _answer_to(lines: Sequence[str], prompt: str) -> str:
    for index, line in enumerate(lines):
        at = line.find(prompt)
        if at < 0:
            continue
        rest = line[at + len(prompt) :].strip()
        if rest:
            return rest
        following = next((n.strip() for n in lines[index + 1 :] if n.strip()), "")
        return "" if following.startswith("#") else following
    return ""


# -- git and the network -----------------------------------------------------


def _run(argv: Sequence[str], cwd: str | None = None) -> tuple[int, str, str]:
    try:
        done = subprocess.run(
            list(argv), cwd=cwd, capture_output=True, text=True, timeout=120, check=False
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


# -- what is covered and what is owed ----------------------------------------


def _log_reflections(cwd: str, *, exclude: str | None = None) -> list[tuple[str, str, str]]:
    """``(sha, "empty"|"content", body)`` for each recent commit whose message
    starts with the header, newest first. ``exclude`` limits the read to commits
    not reachable from that ref (this branch's own commits)."""
    args = ["log", f"-n{LOG_DEPTH}", "--format=%H%x00%T%x00%P%x00%B%x01", "HEAD"]
    if exclude:
        args += ["--not", exclude]
    raw = _git(cwd, *args)
    found = []
    for record in raw.split("\x01"):
        parts = record.lstrip("\n").split("\x00", 3)
        if len(parts) != 4 or not parts[3].startswith("Round-reflection:"):
            continue
        sha, tree, parents, body = parts
        parent = parents.split()[0] if parents.split() else ""
        empty = bool(parent) and tree == _git(cwd, "rev-parse", f"{parent}^{{tree}}").strip()
        found.append((sha, "empty" if empty else "content", body))
    return found


def previous_class_labels(cwd: str, head: str, *, base_ref: str) -> list[str]:
    """Class labels of the newest committed reflection on an EARLIER head of
    THIS branch: the previous round, never an earlier reflection of this one and
    never a reflection a stacked base branch carries. Drafts never count."""
    for _, kind, body in _log_reflections(cwd, exclude=base_ref):
        parsed = parse(body)
        if kind == "empty" and parsed.ok and parsed.head and parsed.head != head:
            return parsed.classes
    return []


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


def audit_problem(path: str, round_started: object = None) -> str | None:
    """Why a cited audit does not count, or None. Local only: the file must
    exist, pass the review gate's own adversarial-evidence check, and, when the
    round's start is known, not predate it (a file written for an earlier round
    does not count; touching a file also moves its time, so this is a floor,
    not proof of freshness)."""
    import review_state  # noqa: PLC0415

    evidence = Path(os.path.expanduser(path))
    try:
        text = evidence.read_text(encoding="utf-8")
        mtime = datetime.fromtimestamp(evidence.stat().st_mtime, UTC)
    except OSError as exc:
        return f"Audit-evidence is unreadable ({exc.strerror})"
    ok, why = review_state._evidence_is_adversarial(text)
    if not ok:
        return f"Audit-evidence is not an adversarial audit: {why}"
    started = _parse_when(round_started)
    if started is not None and mtime < started:
        return "Audit-evidence predates this round's first review"
    return None


def check(
    body: str,
    *,
    head: str,
    round_number: int,
    gate_lane: bool,
    previous_classes: Iterable[str] = (),
    acceptance: Sequence[str] | None = None,
    round_started: object = None,
) -> Reflection:
    """Every local rule a reflection must meet for this round, in one place."""
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
    if parsed.audit_evidence:
        trouble = audit_problem(parsed.audit_evidence, round_started)
        if trouble:
            parsed.problems.append(trouble)
    return parsed


def covered_keys(
    cwd: str,
    head: str,
    *,
    round_number: int,
    gate_lane: bool,
    base_ref: str,
    acceptance: Sequence[str] | None = None,
    round_started: object = None,
) -> set[str]:
    """Keys answered at ``head`` by committed reflections. THE coverage check.

    A reflection counts only when it is an EMPTY commit naming this exact head
    and ``check`` passes it: the round's structure and obligations, the
    recurring-class rule against this branch's previous round, a readable
    adversarial audit where one is cited, no escalation, and the acceptance
    points when the caller has them. A reflection committed by hand is held to
    exactly this; there is no weaker path. Raises ``Refused`` when git cannot
    be read: a caller must treat that as unknown, never as nothing owed.
    """
    previous = previous_class_labels(cwd, head, base_ref=base_ref)
    covered: set[str] = set()
    for _, kind, body in _log_reflections(cwd):
        if kind != "empty":
            continue
        result = check(
            body,
            head=head,
            round_number=round_number,
            gate_lane=gate_lane,
            previous_classes=previous,
            acceptance=acceptance,
            round_started=round_started,
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
        "owed": [],
        "round_head": None,
        "round_started": None,
        "settled": False,
        "settle_until": None,
    }
    rounds = budget.get("rounds") or []
    if budget.get("status") == "ok" and rounds:
        state["round_head"] = rounds[-1].get("head")
    if budget.get("status") != "ok" or budget.get("round_state") != "open":
        return state
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
    import acceptance_declaration  # noqa: PLC0415
    import review_budget  # noqa: PLC0415

    repo, number = pr_identity(cwd)
    budget = review_budget.evaluate_pr(repo, number)
    state = owed_state(budget, (), now=now or datetime.now(UTC))
    if state["status"] == "ok" and state["round_state"] == "open":
        meta = _pr_meta(repo, number)
        declared = acceptance_declaration.parse_acceptance(meta.get("body") or "")
        covered = covered_keys(
            cwd,
            str(state["head"]),
            round_number=int(state["round"] or 0),
            gate_lane=state["gate_lane"],
            base_ref=f"origin/{meta.get('baseRefName') or 'main'}",
            acceptance=list(declared["bullets"]) if declared.get("present") else None,
            round_started=state["round_started"],
        )
        state["owed"] = [k for k in state["open_keys"] if k not in covered]
    state.update({"repo": repo, "pr": number, "errors": budget.get("errors", [])})
    return state


# -- CLI ---------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="what the open round owes")
    validate = sub.add_parser("validate", help="structure-check a file, offline")
    validate.add_argument("file", type=Path)
    validate.add_argument("--round", type=int, default=None)
    validate.add_argument("--gate", action="store_true", help="the gate lane's obligations")
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            print(json.dumps(status(os.getcwd()), indent=2))
            return 0
        parsed = parse(
            args.file.read_text(encoding="utf-8"), round_number=args.round, gate_lane=args.gate
        )
        for problem in parsed.problems:
            print(f"- {problem}")
        return 0 if parsed.ok else 1
    except Refused as exc:
        print(f"review_reflection: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
