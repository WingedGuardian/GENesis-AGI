#!/usr/bin/env python3
"""Authoritative review-round budget from pull-request evidence.

The local review-state counter is deliberately unsuitable for this job: it
tracks a defect-bearing streak, while this policy counts ROUNDS: distinct commit
heads that drew findings from a GitHub App reviewer (before
``ROUND_RULE_CUTOVER_ISO``, heads the primary reviewed).  This module is stdlib-only so the
PreToolUse and commit hooks, the external-review runner, and maintenance CLIs can
all ask the same question.

Unknown evidence is a first-class result.  Callers must ask in an interactive
session and deny in an autonomous one; they must never reinterpret ``unknown``
as zero rounds.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from review_deadline import Deadline

SCHEMA_VERSION = 1
CODEX_REVIEW_BOT = "chatgpt-codex-connector[bot]"
MAX_PR_COMMITS_RESPONSE = 250
#: gh defaults to 30 per page, and the caps above tolerate far more than
#: that — so the default turns a large PR into dozens of SERIAL round trips,
#: which the commit hook then runs under one aggregate deadline. 100 is the
#: API maximum. It goes in the PATH: `gh api -f` sends a POST body field and
#: flips the method, which MEASURED returns 404 on every one of these reads
#: and degrades the whole lookup to `unknown` — faster than the healthy path,
#: which is exactly how it would go unnoticed.
_PAGE_SIZE = 100
MAX_PR_FILES_RESPONSE = 3000

STANDING_REVIEWED_HEAD_LIMIT = 4
GATE_DISCOVERY_ROUND_LIMIT = 2
STRONGLY_DISCOURAGED_REVIEWED_HEADS = 5

#: When rounds stopped meaning "heads the primary reviewed" and started meaning
#: "heads that drew findings from any App reviewer" (owner ruling 2026-10-01:
#: the moment the change's pull request opened). Evidence from before it keeps
#: the old rule, so no pull request's count moved at that instant; evidence
#: after it is counted by the new one. MEASURED 2026-10-01: the new rule alone
#: would have put 26 of 68 open PRs at the terminal round, against 14.
ROUND_RULE_CUTOVER_ISO = "2026-10-01T15:19:00+00:00"

#: The two real choices at a terminal round, in the words the owner reads AT the
#: approval dialog. SINGLE-SOURCED here because BOTH gates state it — the push
#: guard's review-request ask and the commit gate's fix-commit ask — and a copy
#: in each is precisely how the two drift apart. That is not hypothetical: the
#: change that introduced this framing put it in 2 of the 6 approval branches
#: across those two gates and external review found the other 4 (PR #2382).
#:
#: Safe to read as a module attribute from either gate with no local fallback,
#: for two independent reasons: each gate inserts its own ``scripts/`` at
#: ``sys.path[0]`` so gate and evaluator are always co-tree, and both reach the
#: branches that use these only after ``evaluate_pr`` returned ``status=ok``,
#: which cannot happen unless this module loaded.
TERMINAL_DECISION = (
    "The decision here is to MERGE with the outstanding issues accepted and "
    "filed, or to SEND IT BACK for rework."
)

#: The ordinary lane's terminal RULE, stating the boundary and the decision and
#: nothing about what a given approval authorizes. Interpolates the limit rather
#: than spelling "four", so a future change to STANDING_REVIEWED_HEAD_LIMIT cannot
#: leave the prose asserting the old boundary — the exact failure this PR removed,
#: where a retired cap of 7 still shaped the live four-head tier.
#:
#: ACTION-NEUTRAL on purpose, and the split below is not cosmetic. The two gates
#: authorize DIFFERENT things — the push guard a review round, the commit gate a
#: single fix commit — so a shared sentence that says "approve one further ROUND"
#: is simply false at the commit gate, and pasting it there contradicts its own
#: next sentence. Use this constant wherever the action is not a round.
#:
#: It also asserts a FOUR-head boundary, so it belongs only where the count is
#: actually four: rendering "there is no ordinary round 5" to someone already
#: holding five heads tells them something false. Past the boundary, use
#: TERMINAL_DECISION with a lead that says so.
ORDINARY_TERMINAL_RULE = (
    f"ROUND {STANDING_REVIEWED_HEAD_LIMIT} IS TERMINAL: there is no ordinary "
    f"round {STANDING_REVIEWED_HEAD_LIMIT + 1}. {TERMINAL_DECISION}"
)

#: The rule plus the clause for a gate whose approval authorizes a further ROUND.
ORDINARY_TERMINAL_NOTICE = (
    f"{ORDINARY_TERMINAL_RULE} Approve only to authorize one further round anyway, "
    "and only if that is genuinely the least costly option."
)

CONFIRMATION_MARKER_TEMPLATE = "<!-- genesis-review-request head={head} kind=confirmation -->"

HOOK_SURFACE_PREFIXES = (
    "scripts/hooks/",
    ".claude/hooks/",
    "config/behavioral_rules/",
)
HOOK_SURFACE_FILES = frozenset(
    {
        "scripts/bash_safety_hook.sh",
        "scripts/review_scope.py",
        "scripts/review_state.py",
        "scripts/review_budget.py",
        "scripts/review_findings.py",
        "scripts/review_deadline.py",
        "scripts/external_review.py",
        "scripts/lib/gate_menu.py",
        ".claude/settings.json",
        "scripts/behavioral_linter.py",
        "scripts/check_stale_pending.py",
        "scripts/content_safety_hook.py",
        "scripts/contribution_offer_hook.py",
        "scripts/edit_failure_sensor.py",
        "scripts/file_context_hook.py",
        "scripts/file_modification_audit_hook.py",
        "scripts/genesis_precompact.py",
        "scripts/genesis_session_context.py",
        "scripts/genesis_session_end.py",
        "scripts/genesis_stop_hook.py",
        "scripts/genesis_urgent_alerts.py",
        "scripts/plan_bookmark_hook.py",
        "scripts/pretool_check.py",
        "scripts/proactive_memory_hook.py",
        "scripts/procedure_advisor.py",
        "scripts/review_enforcement_commit.py",
        "scripts/review_enforcement_prompt.py",
        "scripts/review_invalidate_on_commit.py",
        "scripts/surface_handoffs.py",
        "scripts/surface_open_prs.py",
        "scripts/surface_pr_updates.py",
        "config/protected_paths.yaml",
        "config/repo_topology.yaml",
        "config/external_review.yaml",
        "src/genesis/session_awareness/external_review.py",
        "src/genesis/session_awareness/external_review_config.py",
    }
)

_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_PREFIX_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_CODEX_CLEAN_RE = re.compile(r"Codex Review:\s*Didn'?t find any major issues", re.IGNORECASE)
_REVIEWED_COMMIT_RE = re.compile(r"Reviewed commit:\**\s*`?([0-9a-fA-F]{7,40})`?", re.IGNORECASE)
_CONFIRMATION_RE = re.compile(
    r"<!--\s*genesis-review-request\s+head=([0-9a-fA-F]{40})\s+"
    r"kind=confirmation\s*-->",
    re.IGNORECASE,
)

Runner = Callable[..., tuple[int, str, str]]


def _parse_external_identity_scalar(raw: str) -> object:
    """Decode the small YAML scalar subset used by the stdlib-only hook."""
    if raw.startswith('"'):
        value, end = json.JSONDecoder().raw_decode(raw)
        suffix = raw[end:]
        if suffix.strip() and (
            not suffix[:1].isspace() or not suffix.lstrip().startswith("#")
        ):
            raise ValueError("unexpected content after quoted scalar")
        return value
    if raw.startswith("'"):
        match = re.fullmatch(r"'((?:[^']|'')*)'(?:\s+#.*)?\s*", raw)
        if match is None:
            raise ValueError("malformed single-quoted scalar")
        return match.group(1).replace("''", "'")
    return raw.split(" #", 1)[0].strip()


def configured_external_identity_templates() -> tuple[tuple[str, ...], str | None]:
    """Load the optional external-review identity without a YAML dependency.

    The hook tree is stdlib-only.  This reads one deliberately scalar config
    key from the shipped file and its install-local overlay; the overlay wins.
    An unreadable or malformed declared value returns an error so callers treat
    the whole budget as unknown rather than silently dropping a reviewer.
    """
    env_value = os.environ.get("GENESIS_EXTERNAL_REVIEW_IDENTITY_TEMPLATE")
    if env_value is not None:
        return ((env_value,) if env_value else ()), None

    paths = (
        Path(__file__).resolve().parents[1] / "config" / "external_review.yaml",
        Path.home() / ".genesis" / "config" / "external_review.local.yaml",
    )
    found: str | None = None
    for path in paths:
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return (), "external_identity_config_unreadable"
        for line in lines:
            match = re.match(r"^\s*report_identity_template\s*:\s*(.*?)\s*$", line)
            if not match:
                continue
            raw = match.group(1).strip()
            if not raw:
                found = ""
                continue
            try:
                value = _parse_external_identity_scalar(raw)
            except (json.JSONDecodeError, ValueError):
                return (), "external_identity_config_malformed"
            if not isinstance(value, str):
                return (), "external_identity_config_malformed"
            found = value
    return ((found,) if found else ()), None


def is_hook_surface_path(path: str) -> bool:
    """Whether a repository-relative path belongs to the enforcement surface."""
    return path in HOOK_SURFACE_FILES or any(path.startswith(p) for p in HOOK_SURFACE_PREFIXES)


def confirmation_marker(head: str) -> str:
    """The exact request marker for a full commit head."""
    normalized = str(head or "").strip().lower()
    if not _FULL_SHA_RE.fullmatch(normalized):
        raise ValueError("confirmation head must be a full 40-character SHA")
    return CONFIRMATION_MARKER_TEMPLATE.format(head=normalized)



class _BudgetExhausted(Exception):
    """The aggregate lookup budget ran out before this call could be issued."""


class _Truncated(Exception):
    """A nested connection had more nodes than one read returns."""


def _unknown(*errors: str, current_head: str = "") -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "unknown",
        "reason": "evidence_unknown",
        "reviewed_heads": [],
        "count": None,
        "current_head": current_head,
        "current_head_reviewed": None,
        "next_round": None,
        "gate_surface": None,
        "confirmation_requested": None,
        "confirmation_exempt": False,
        "approval_required": True,
        "commit_approval_required": True,
        "strongly_discouraged": True,
        "errors": [e for e in errors if e],
    }


def _full_sha(value: object) -> str | None:
    token = str(value or "").strip().lower()
    return token if _FULL_SHA_RE.fullmatch(token) else None


def _resolve_sha(token: str, commits: Sequence[str]) -> tuple[str | None, str | None]:
    normalized = token.strip().lower()
    if not _PREFIX_SHA_RE.fullmatch(normalized):
        return None, "malformed_review_head"
    if len(normalized) == 40:
        if normalized not in commits:
            return None, "unresolved_review_head"
        return normalized, None
    matches = [head for head in commits if head.startswith(normalized)]
    if len(matches) != 1:
        return None, "ambiguous_review_head" if len(matches) > 1 else "unresolved_review_head"
    return matches[0], None


def _identity_regex(template: str) -> re.Pattern[str] | None:
    if not isinstance(template, str) or template.count("{head}") != 1:
        return None
    escaped = re.escape(template).replace(re.escape("{head}"), r"([0-9a-fA-F]{40})")
    return re.compile(escaped)


def _parse_time(raw: object) -> tuple[bool, datetime | None]:
    """``(ok, when)`` for an ISO-8601 UTC timestamp; ``(True, None)`` when absent.

    Compared as datetimes, never as strings: GitHub writes ``Z`` and Python
    writes ``+00:00``, and a string comparison across the two is wrong.
    """
    if raw is None:
        return True, None
    if not isinstance(raw, str):
        return False, None
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return False, None
    if when.tzinfo is None:
        return False, None
    return True, when


#: What this module calls on ``review_findings``. A tree whose copy predates any
#: of them is version skew: the budget is unknown, never a traceback.
_FINDINGS_API = (
    "WORKFLOW_BOTS",
    "surface_only_logins",
    "is_app_login",
    "is_finding",
    "body_finding_count",
    "declares_findings",
    "codex_comment_finding_head",
)


def _findings_module() -> Any:
    """The sibling ``review_findings`` module, or None when absent or stale."""
    try:
        import review_findings  # noqa: PLC0415 - sibling stdlib module
    except Exception:  # noqa: BLE001 - reverse skew is unknown, never zero rounds.
        return None
    if not all(hasattr(review_findings, name) for name in _FINDINGS_API):
        return None
    return review_findings


def evaluate_evidence(
    *,
    current_head: str,
    commit_heads: Sequence[object],
    reviews: Sequence[Mapping[str, object]],
    issue_comments: Sequence[Mapping[str, object]],
    changed_files: Sequence[Mapping[str, object] | str],
    external_identity_templates: Sequence[str] = (),
    primary_login: str = CODEX_REVIEW_BOT,
    cutover: str | None = None,
) -> dict[str, Any]:
    """Evaluate already-fetched PR evidence without I/O.

    A ROUND is a distinct head that drew findings (owner ruling 2026-10-01): a
    review by any GitHub App reviewer carrying at least one finding, by
    ``review_findings.is_finding``, or a Codex findings issue comment. Its head
    is the review's full commit SHA, whether or not a force-push has since
    removed that commit from the PR. A clean review, a Codex clean comment and
    the configured identity template CONFIRM a head (``current_head_reviewed``)
    and never add a round.

    Evidence before ``cutover`` (default ``ROUND_RULE_CUTOVER_ISO``) keeps the
    old rule: every head the PRIMARY reviewed, clean included. Items carrying no
    timestamp are treated as before it. The count is the union of the two, so at
    the cutover instant it equals the old count by construction.

    ``approval_required`` is the review-request decision. Commit policy differs
    on gate PRs: the second-round fix and its one confirmation are part of round
    two, so ``commit_approval_required`` begins after that confirmation.
    """
    head = _full_sha(current_head)
    if head is None:
        return _unknown("malformed_current_head", current_head=str(current_head or ""))

    commits: list[str] = []
    for raw in commit_heads:
        full = _full_sha(raw)
        if full is None:
            return _unknown("malformed_pr_commit", current_head=head)
        if full not in commits:
            commits.append(full)
    if head not in commits:
        return _unknown("current_head_missing_from_commits", current_head=head)
    order = {sha: index for index, sha in enumerate(commits)}

    identities: list[re.Pattern[str]] = []
    for template in external_identity_templates:
        pattern = _identity_regex(template)
        if pattern is None:
            return _unknown("invalid_external_identity_template", current_head=head)
        identities.append(pattern)

    review_findings = _findings_module()
    if review_findings is None:
        return _unknown("review_findings_unimportable", current_head=head)

    ok, cut = _parse_time(ROUND_RULE_CUTOVER_ISO if cutover is None else cutover)
    if not ok or cut is None:
        return _unknown("malformed_round_cutover", current_head=head)

    legacy: set[str] = set()  # the old rule's heads, before cutover
    confirmed: set[str] = set()  # heads the primary (or the identity) reviewed
    found: dict[str, dict[str, Any]] = {}  # round head -> what landed there

    def confirm(sha: str, before: bool) -> None:
        confirmed.add(sha)
        if before:
            legacy.add(sha)

    def add_round(sha: str, login: str, findings: int) -> None:
        entry = found.setdefault(sha, {"findings": 0, "reviewers": []})
        entry["findings"] += findings
        if login not in entry["reviewers"]:
            entry["reviewers"].append(login)

    surface_only = review_findings.surface_only_logins()
    for item in reviews:
        if not isinstance(item, Mapping):
            return _unknown("malformed_review_record", current_head=head)
        login = item.get("login")
        if login is None:
            # Deleted author. That account can no longer be any reviewer we
            # know, so it carries no evidence; rejecting it would wedge the PR.
            continue
        if not isinstance(login, str):
            return _unknown("malformed_review_record", current_head=head)
        primary = login == primary_login
        if item.get("state") == "PENDING" and not primary:
            # The old rule counted every primary review, pending included; keep it
            # for the legacy count. A pending review has no time, so it is placed
            # before the cutover and never opens a round under the new rule.
            continue
        if not primary and (
            not review_findings.is_app_login(login)
            or login in review_findings.WORKFLOW_BOTS
            or login in surface_only
        ):
            continue  # humans, workflow bots and CodeQL never open a round
        ok, when = _parse_time(item.get("submitted_at"))
        if not ok:
            return _unknown("malformed_review_time", current_head=head)
        # A review object always names its full commit. A missing one is a
        # malformed record, never a review of nothing.
        sha = _full_sha(item.get("commit_id"))
        if sha is None:
            return _unknown("malformed_review_head", current_head=head)
        before = when is None or when < cut
        if primary:
            confirm(sha, before)
        elif when is None:
            # Only a test seam omits the time; a non-primary review cannot be
            # placed on either side of the cutover, and dropping it undercounts.
            return _unknown("review_time_missing", current_head=head)
        if before:
            continue
        top_level = item.get("top_level")
        body = item.get("body")
        if body is None:
            body = ""
        if (
            not isinstance(top_level, list)
            or not all(isinstance(b, str) for b in top_level)
            or not isinstance(body, str)
        ):
            return _unknown("review_comments_unreadable", current_head=head)
        findings = sum(1 for b in top_level if review_findings.is_finding(login, b))
        findings += review_findings.body_finding_count(login, body)
        if findings:
            add_round(sha, login, findings)
        elif not top_level and review_findings.declares_findings(login, body):
            # Its body says it posted findings and no top-level comment
            # survives: they were deleted, which must not read as a clean review.
            # Partial deletion is not caught (MEASURED 2026-10-01: declared count
            # equals surviving comments on 552 of 554 reviews, and one exception
            # is a live PR, so a strict comparison would wedge it unverified).
            return _unknown("review_findings_deleted", current_head=head)

    confirmation_heads: set[str] = set()
    for item in issue_comments:
        if not isinstance(item, Mapping):
            return _unknown("malformed_comment_record", current_head=head)
        login, author_type, body = item.get("login"), item.get("type"), item.get("body")
        if not isinstance(body, str):
            return _unknown("malformed_comment_record", current_head=head)
        ok, when = _parse_time(item.get("created_at"))
        if not ok:
            return _unknown("malformed_comment_time", current_head=head)
        before = when is None or when < cut
        # The confirmation marker and the identity template are matched on TEXT,
        # not on who wrote it, so a deleted author's comment still counts for both.
        for marker in _CONFIRMATION_RE.finditer(body):
            confirmation_heads.add(marker.group(1).lower())
        for pattern in identities:
            for match in pattern.finditer(body):
                resolved, error = _resolve_sha(match.group(1), commits)
                if error == "unresolved_review_head" and not before:
                    continue  # confirms only, as for a clean comment below
                if error:
                    return _unknown(error, current_head=head)
                confirm(resolved or "", before)
        if login is None or author_type is None:
            continue  # deleted author: never the primary
        if not isinstance(login, str) or not isinstance(author_type, str):
            return _unknown("malformed_comment_record", current_head=head)
        if login != primary_login or author_type != "Bot":
            continue
        if _CODEX_CLEAN_RE.search(body):
            match = _REVIEWED_COMMIT_RE.search(body)
            if match is None:
                return _unknown("clean_comment_missing_head", current_head=head)
            resolved, error = _resolve_sha(match.group(1), commits)
            if error == "unresolved_review_head" and not before:
                # After cutover a clean comment only confirms a head, and a commit
                # no longer in the PR can never be the current one.
                continue
            if error:
                return _unknown(error, current_head=head)
            confirm(resolved or "", before)
            continue
        is_findings, sha = review_findings.codex_comment_finding_head(body)
        if is_findings and not before:
            if sha is None:
                return _unknown("codex_findings_comment_unbound", current_head=head)
            confirm(sha, False)
            add_round(sha, login, 1)

    paths: list[str] = []
    for item in changed_files:
        if isinstance(item, str):
            paths.append(item)
            continue
        if not isinstance(item, Mapping) or not isinstance(item.get("filename"), str):
            return _unknown("malformed_changed_file", current_head=head)
        paths.append(str(item["filename"]))
        previous = item.get("previous_filename")
        if previous is not None:
            if not isinstance(previous, str):
                return _unknown("malformed_changed_file", current_head=head)
            paths.append(previous)

    gate_surface = any(is_hook_surface_path(path) for path in paths)
    round_heads = legacy | set(found)
    heads = sorted(round_heads)
    count = len(heads)
    current_reviewed = head in confirmed
    confirmation_requested = head in confirmation_heads
    # A head force-pushed out of the PR sorts before every live commit.
    rounds = [
        {
            "head": sha,
            "legacy": sha not in found,
            "findings": found[sha]["findings"] if sha in found else None,
            "reviewers": found[sha]["reviewers"] if sha in found else [],
        }
        for sha in sorted(round_heads, key=lambda s: (order.get(s, -1), s))
    ]
    # The gate lane grants ONE confirmation request after its last round, and the
    # guard admits it only when the request carries the exact-head marker. That
    # marker is the one-shot token: a marker for any head other than the current
    # one means the confirmation was used. Clean reviews add no round, so without
    # this a clean confirmation would leave the count at the limit and re-grant
    # the exemption at every new head. Read as a set, never by ordering events:
    # three reviews in a row found defects in rules that inferred it from
    # commit positions or timestamps (owner ruling 2026-10-01).
    # The current head's own confirmation is spent too once the primary has
    # reviewed it: a fix commit on top of a confirmed head is past the budget.
    confirmation_spent = bool(confirmation_heads - {head}) or (
        confirmation_requested and current_reviewed
    )
    confirmation_exempt = (
        gate_surface
        and count == GATE_DISCOVERY_ROUND_LIMIT
        and not current_reviewed
        and not confirmation_requested
        and not confirmation_spent
        # A head that already drew findings is a round, not a fix to confirm.
        and head not in round_heads
    )
    if gate_surface:
        request_approval = count >= GATE_DISCOVERY_ROUND_LIMIT and not confirmation_exempt
        commit_approval = count > GATE_DISCOVERY_ROUND_LIMIT or (
            count == GATE_DISCOVERY_ROUND_LIMIT and confirmation_spent
        )
        discouraged = count >= GATE_DISCOVERY_ROUND_LIMIT and not confirmation_exempt
        reason = (
            "gate_confirmation"
            if confirmation_exempt
            else ("gate_limit" if request_approval else "within_gate_limit")
        )
    else:
        request_approval = count >= STANDING_REVIEWED_HEAD_LIMIT
        commit_approval = request_approval
        discouraged = count >= STRONGLY_DISCOURAGED_REVIEWED_HEADS
        reason = "ordinary_limit" if request_approval else "within_ordinary_limit"

    # Live rounds only: a head a force-push removed has no position, so its place
    # in this order (and so in the trend) would be a guess.
    scored = [r["findings"] for r in rounds if r["findings"] is not None and r["head"] in order]
    trend = None
    if len(scored) >= 2:
        trend = (
            "rising"
            if scored[-1] > scored[-2]
            else "falling"
            if scored[-1] < scored[-2]
            else "flat"
        )
    if not rounds:
        round_state = "none"
    elif rounds[-1]["head"] == head:
        round_state = "open"
    else:
        round_state = "complete"

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        "reason": reason,
        "reviewed_heads": heads,
        "count": count,
        "current_head": head,
        "current_head_reviewed": current_reviewed,
        "next_round": count + 1,
        "gate_surface": gate_surface,
        "confirmation_requested": confirmation_requested,
        "confirmation_exempt": confirmation_exempt,
        "approval_required": request_approval,
        "commit_approval_required": commit_approval,
        "strongly_discouraged": discouraged,
        "rounds": rounds,
        "round_state": round_state,
        "trend": trend,
        "legacy_heads": len(legacy),
        "errors": [],
    }


def _default_runner(argv: Sequence[str], *, timeout: float) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=timeout, check=False
        )
        return result.returncode, result.stdout, result.stderr
    except Exception:
        return 1, "", "runner_failed"


def _json_lines(raw: str, source: str) -> tuple[list[dict[str, Any]] | None, str | None]:
    rows: list[dict[str, Any]] = []
    for line in (raw or "").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            return None, f"{source}_malformed"
        if not isinstance(item, dict):
            return None, f"{source}_malformed"
        rows.append(item)
    return rows, None


#: Every GraphQL connection is read 100 nodes at a time (the API maximum) and
#: followed by cursor. The page bound turns a runaway PR into ``unknown`` rather
#: than an unbounded serial walk under the commit hook's deadline: 50 pages is
#: 5,000 reviews or comments, against a MEASURED maximum of 94 reviews on one PR
#: over the 300 most recent (2026-09-29). Commits and files keep their own REST
#: ceilings below, which stop the walk long before this does.
_GRAPHQL_MAX_PAGES = 50

#: The whole of ONE read (every page of it), not one call. Each page keeps the
#: 8s cap a REST ``--paginate`` process had for its entire walk, so without this
#: a many-page read could run to 50 x 8s. MEASURED 2026-09-29: a 1,971-file PR
#: pages 20 times in 13.2s (the old REST files walk took 7.4s). 20s leaves such
#: a PR readable and holds the worst case for the whole lookup (two reads plus
#: the rename fallback) to 48s, under the 60s the push guard is registered for
#: -- where an overrun is a SIGKILL that lets the command through. A PR too
#: large to read in time is ``unknown``, exactly as the REST walk hitting its
#: 8s cap was.
_GRAPHQL_READ_SECONDS = 20.0

#: One connection per REST endpoint the lookup used to call, projected to the
#: fields ``evaluate_evidence`` reads and nothing more.
_GRAPHQL_CONNECTIONS = {
    "reviews": (
        "reviews(first: 100, after: $after_reviews) { pageInfo { hasNextPage endCursor } "
        "nodes { state submittedAt body author { login __typename } commit { oid } "
        "comments(first: 100) { pageInfo { hasNextPage } nodes { replyTo { id } body } } } }"
    ),
    "comments": (
        "comments(first: 100, after: $after_comments) { pageInfo { hasNextPage endCursor } "
        "nodes { body createdAt author { login __typename } } }"
    ),
    "files": (
        "files(first: 100, after: $after_files) { pageInfo { hasNextPage endCursor } "
        "nodes { path changeType } }"
    ),
    "commits": (
        "commits(first: 100, after: $after_commits) { pageInfo { hasNextPage endCursor } "
        "nodes { commit { oid } } }"
    ),
}

#: A changed file GraphQL reports this way had an earlier path, and GraphQL does
#: not expose it. The earlier path decides the hook surface (a file renamed OUT of
#: it is still a gate change), so a PR carrying one falls back to the REST files
#: read, which does.
_GRAPHQL_PATH_CHANGING = frozenset({"RENAMED", "COPIED"})


def _graphql_query(names: Sequence[str]) -> str:
    params = "".join(f", $after_{name}: String" for name in names)
    fields = " ".join(_GRAPHQL_CONNECTIONS[name] for name in names)
    return (
        f"query($owner: String!, $name: String!, $number: Int!{params}) "
        "{ repository(owner: $owner, name: $name) { pullRequest(number: $number) "
        f"{{ headRefOid {fields} }} }} }}"
    )


def _graphql_author(author: object) -> tuple[str | None, str | None]:
    """The REST ``user.login`` / ``user.type`` pair for a GraphQL author.

    GraphQL names a GitHub App by its bare app slug; REST, and every login
    constant in the hooks, carries a ``[bot]`` suffix. MEASURED 2026-09-29:
    ``chatgpt-codex-connector`` in GraphQL is ``chatgpt-codex-connector[bot]`` in
    REST. Keyed on ``__typename == "Bot"``, so a human whose login happens to
    match a bot slug is never promoted. A deleted account is ``None`` in GraphQL
    (REST substitutes a ``ghost`` user); both carry no reviewer identity.
    """
    if author is None:
        return None, None
    if not isinstance(author, dict):
        raise ValueError("author")
    login, kind = author.get("login"), author.get("__typename")
    if not isinstance(login, str) or not isinstance(kind, str):
        raise ValueError("author")
    if kind == "Bot" and not login.endswith("[bot]"):
        login = f"{login}[bot]"
    return login, kind


def _graphql_rows(name: str, nodes: object) -> tuple[list[dict[str, Any]], bool]:
    """Convert one page of nodes to the REST row shapes. Raises ValueError."""
    if not isinstance(nodes, list):
        raise ValueError(name)
    rows: list[dict[str, Any]] = []
    path_changed = False
    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError(name)
        if name == "reviews":
            login, _ = _graphql_author(node.get("author"))
            commit = node.get("commit")
            if commit is not None and not isinstance(commit, dict):
                raise ValueError(name)
            submitted = node.get("submittedAt")
            if node.get("state") != "PENDING" and not isinstance(submitted, str):
                raise ValueError(name)  # a submitted review always carries its time
            thread = node.get("comments")
            if not isinstance(thread, dict) or not isinstance(thread.get("nodes"), list):
                raise ValueError(name)
            info = thread.get("pageInfo")
            if not isinstance(info, dict) or not isinstance(info.get("hasNextPage"), bool):
                raise ValueError(name)
            if info["hasNextPage"]:
                # Past 100 comments on ONE review the rest are unread, and an
                # unread finding is an uncounted round (MEASURED 2026-09-29: max 16).
                raise _Truncated
            top_level = []
            for comment in thread["nodes"]:
                if not isinstance(comment, dict) or not isinstance(comment.get("body"), str):
                    raise ValueError(name)
                if comment.get("replyTo") is None:
                    top_level.append(comment["body"])
            rows.append(
                {
                    "login": login,
                    "commit_id": (commit or {}).get("oid"),
                    "state": node.get("state"),
                    "submitted_at": submitted,
                    "body": node.get("body"),
                    "top_level": top_level,
                }
            )
        elif name == "comments":
            login, kind = _graphql_author(node.get("author"))
            rows.append(
                {
                    "login": login,
                    "type": kind,
                    "body": node.get("body"),
                    "created_at": node.get("createdAt"),
                }
            )
        elif name == "files":
            path = node.get("path")
            if not isinstance(path, str):
                raise ValueError(name)
            if node.get("changeType") in _GRAPHQL_PATH_CHANGING:
                path_changed = True
            rows.append({"filename": path, "previous_filename": None})
        else:
            commit = node.get("commit")
            if not isinstance(commit, dict):
                raise ValueError(name)
            rows.append({"sha": commit.get("oid")})
    return rows, path_changed


def _review_digest(rows: Sequence[Mapping[str, object]], rf: Any) -> list[tuple[object, ...]]:
    """What decides the verdict in each review row, without the prose.

    The re-read must agree with the first read on everything the count depends
    on. Comparing raw bodies instead would turn every reviewer EDIT between the
    two reads (CodeRabbit marks its comments "Addressed" right after a push, the
    moment sessions commit) into ``unknown``, which denies dispatched sessions.
    """
    digest: list[tuple[object, ...]] = []
    for row in rows:
        login = row.get("login")
        body = row.get("body") if isinstance(row.get("body"), str) else ""
        tops = row.get("top_level") if isinstance(row.get("top_level"), list) else []
        named = isinstance(login, str)
        digest.append(
            (
                login,
                row.get("commit_id"),
                row.get("state"),
                row.get("submitted_at"),
                len(tops),
                tuple(named and isinstance(b, str) and rf.is_finding(login, b) for b in tops),
                rf.body_finding_count(login, body) if named else 0,
                rf.declares_findings(login, body) if named else False,
            )
        )
    return digest


def _evaluate_pr_inner(
    repo: str,
    pr: int | str,
    *,
    external_identity_templates: Sequence[str] | None = None,
    runner: Runner = _default_runner,
    timeout_for: Callable[[float], float] | None = None,
    budget_seconds: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Fetch and evaluate one PR.  Every failed endpoint yields ``unknown``.

    ``budget_seconds`` caps the WHOLE lookup, not each call. The per-call caps
    below are 6-8s each and run serially, so a degraded-but-not-dead GitHub can
    keep every individual call inside its own cap while the total runs to ~36s.
    A caller living under a harness timeout needs the aggregate bound instead:
    the PreToolUse commit hook is registered for 10s, and an overrun SIGKILLs
    it, which FAILS OPEN — the commit proceeds with neither this budget check
    nor the review-current and depth checks that run after it.

    Exhausting the budget yields ``unknown`` like any other unreadable endpoint,
    which denies autonomous sessions and asks a human. Slow is treated as
    unreadable on purpose: both mean the evidence did not arrive, and only the
    reason differs.

    Default is ``None`` — unbounded, the behaviour every non-hook caller had
    before. ``monotonic`` is a seam so the deadline can be tested without
    sleeping; it is monotonic rather than wall-clock so an NTP step cannot
    expire or extend a live budget.
    """
    if not repo or "/" not in repo or not str(pr).isdigit():
        return _unknown("invalid_pr_identity")

    if external_identity_templates is None:
        external_identity_templates, config_error = configured_external_identity_templates()
        if config_error:
            return _unknown(config_error)

    # The reviewer whose reviewed heads are rounds is THE primary
    # (`review_findings.CODEX_LOGIN`), the same reviewer merge freshness requires,
    # read from that one definition rather than a copy here. An unimportable
    # module is unknown, never "Codex by default".
    review_findings = _findings_module()
    try:
        if review_findings is None:
            raise ImportError("review_findings")
        primary_login = review_findings.primary_reviewer_login()
    except Exception:  # noqa: BLE001 - reverse skew is unknown, never zero rounds.
        return _unknown("review_findings_unimportable")

    timeout_for = timeout_for or (lambda seconds: seconds)
    deadline = Deadline.after(budget_seconds, monotonic=monotonic)
    # A call given less than this cannot complete a TLS handshake plus a GitHub
    # round trip, so issuing it would burn the remaining budget to arrive at the
    # same `unknown` — with the timeout landing INSIDE the caller's harness
    # window rather than before it.
    floor = 0.75

    def run(argv: list[str], seconds: float) -> tuple[int, str, str]:
        remaining = deadline.remaining()
        if remaining is not None:
            if deadline.exhausted(minimum_useful=floor):
                raise _BudgetExhausted
            seconds = min(seconds, remaining)
        try:
            return runner(argv, timeout=timeout_for(seconds))
        except Exception:
            return 1, "", "runner_failed"

    repo_owner, _, repo_name = repo.partition("/")

    def snapshot(names: Sequence[str]) -> tuple[dict[str, Any] | None, str | None]:
        """The PR head plus every page of the named connections, in ONE query.

        It replaced five REST reads (head, reviews, issue comments, files,
        commits) plus a second head read and a re-read of the mutable two.
        MEASURED 2026-09-29, old vs new ``evaluate_pr`` over all 70 open PRs:
        identical results on every one; old median 5.21s / p90 5.98s / max
        10.78s, new median 1.54s / p90 1.80s / max 2.33s -- against the 7.5s the
        commit hook allows. A lookup past that budget reads as ``unknown``, which
        asks in the foreground and denies a dispatched session; how often the
        old path hit it was not counted (a same-day run under the budget saw 0
        of 70, an earlier one showed a p90 above it).

        Every page re-reads ``headRefOid``; a head that moves between pages is
        the same race the final head read below exists to catch.
        """
        rows: dict[str, list[dict[str, Any]]] = {item: [] for item in names}
        cursors: dict[str, str] = {}
        pending = list(names)
        head: str | None = None
        path_changed = False
        read = Deadline.after(_GRAPHQL_READ_SECONDS, monotonic=monotonic)
        for _page in range(_GRAPHQL_MAX_PAGES):
            # ONE clock reading decides both whether to call and how long the
            # call may take. Two readings leave a gap a stall can fall into:
            # past the deadline it raised out of `evaluate_pr` (Codex P2,
            # #2594); just short of it, it issued a call too small to finish.
            left = read.remaining()
            if left is None or left < floor:
                return None, "graphql_read_timeout"
            argv = [
                "gh",
                "api",
                "graphql",
                "-f",
                f"query={_graphql_query(pending)}",
                "-f",
                f"owner={repo_owner}",
                "-f",
                f"name={repo_name}",
                "-F",
                f"number={pr}",
            ]
            for item in pending:
                if item in cursors:
                    argv += ["-f", f"after_{item}={cursors[item]}"]
            rc, raw, _ = run(argv, min(8.0, left))
            if rc != 0:
                return None, "graphql_unreadable"
            try:
                payload = json.loads(raw)
                # gh exits non-zero on an `errors` response, including one that
                # carries partial `data` (MEASURED 2026-09-29). Checked here too,
                # so partial evidence can never be counted if that ever changes.
                if payload.get("errors"):
                    return None, "graphql_errors"
                data = payload["data"]["repository"]["pullRequest"]
            except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
                return None, "graphql_malformed"
            if not isinstance(data, dict):
                return None, "graphql_malformed"
            page_head = data.get("headRefOid")
            if not isinstance(page_head, str):
                return None, "graphql_malformed"
            if head is None:
                head = page_head
            elif page_head != head:
                return None, "head_changed_during_evaluation"
            following: list[str] = []
            for item in pending:
                connection = data.get(item)
                if not isinstance(connection, dict):
                    return None, f"{item}_malformed"
                try:
                    page_rows, changed = _graphql_rows(item, connection.get("nodes"))
                except _Truncated:
                    return None, f"{item}_comments_truncated"
                except ValueError:
                    return None, f"{item}_malformed"
                rows[item].extend(page_rows)
                path_changed = path_changed or changed
                info = connection.get("pageInfo")
                if not isinstance(info, dict):
                    return None, f"{item}_malformed"
                more = info.get("hasNextPage")
                if not isinstance(more, bool):
                    return None, f"{item}_malformed"
                if more:
                    cursor = info.get("endCursor")
                    if not isinstance(cursor, str) or not cursor:
                        return None, f"{item}_malformed"
                    cursors[item] = cursor
                    following.append(item)
            if not following:
                return {"head": head, "path_changed": path_changed, **rows}, None
            pending = following
        return None, f"{pending[0]}_response_truncated"

    # Each seam short-circuits its own read, as each REST call's seam did, so a
    # test that supplies every input issues no call at all.
    seams = {
        "reviews": "_TEST_GH_CODEX_REVIEWS",
        "comments": "_TEST_GH_CODEX_COMMENTS",
        "files": "_TEST_REVIEW_BUDGET_FILES",
        "commits": "_TEST_REVIEW_BUDGET_COMMITS",
    }
    test_head = os.environ.get("_TEST_REVIEW_BUDGET_HEAD")
    needed = [item for item, env_name in seams.items() if os.environ.get(env_name) is None]
    first: dict[str, Any] | None = None
    if needed or test_head is None:
        first, error = snapshot(needed)
        if error or first is None:
            return _unknown(error or "graphql_unreadable", current_head=test_head or "")
        if test_head is None:
            test_head = str(first["head"]).strip()

    fetched: dict[str, list[dict[str, Any]]] = {}
    for item, env_name in seams.items():
        raw = os.environ.get(env_name)
        if raw is None:
            fetched[item] = list((first or {}).get(item) or [])
            continue
        rows, error = _json_lines(raw, item)
        if error:
            return _unknown(error, current_head=test_head)
        fetched[item] = rows or []

    if first is not None and "files" in needed and first.get("path_changed"):
        rc, raw, _ = run(
            [
                "gh",
                "api",
                f"repos/{repo}/pulls/{pr}/files?per_page={_PAGE_SIZE}",
                "--paginate",
                "--jq",
                ".[] | {filename: .filename, previous_filename: .previous_filename}",
            ],
            8,
        )
        if rc != 0:
            return _unknown("files_unreadable", current_head=test_head)
        rows, error = _json_lines(raw, "files")
        if error:
            return _unknown(error, current_head=test_head)
        fetched["files"] = rows or []

    # Both connections carry the REST endpoints' hard response ceilings. Landing
    # exactly on one cannot prove the evidence is complete, so the budget is
    # unknown rather than under-counted or misclassified as an ordinary PR.
    if len(fetched["commits"]) >= MAX_PR_COMMITS_RESPONSE:
        return _unknown("commits_response_truncated", current_head=test_head)
    if len(fetched["files"]) >= MAX_PR_FILES_RESPONSE:
        return _unknown("files_response_truncated", current_head=test_head)

    # The final read pins the head AND the mutable evidence: a review or a
    # confirmation comment posted after the first read but before the head check
    # leaves the head unchanged while the snapshot under-counts the budget. One
    # query re-reads both and the snapshots must agree -- the race window
    # shrinks to the last call rather than the whole fetch block. (Neither API
    # offers a point-in-time read; this narrows, not closes, it.)
    final_head = os.environ.get("_TEST_REVIEW_BUDGET_HEAD_AFTER")
    if final_head is None and os.environ.get("_TEST_REVIEW_BUDGET_HEAD") is not None:
        final_head = test_head
    mutable = [item for item in ("reviews", "comments") if item in needed]
    second: dict[str, Any] | None = None
    if final_head is None or mutable:
        second, error = snapshot(mutable)
        if error or second is None:
            if error == "graphql_unreadable":
                error = "final_head_unreadable"
            return _unknown(error or "final_head_unreadable", current_head=test_head)
        if final_head is None:
            final_head = str(second["head"]).strip()
    if final_head != test_head:
        return _unknown("head_changed_during_evaluation", current_head=final_head)
    for item in mutable:
        again = (second or {}).get(item)
        if item == "reviews":
            same = _review_digest(again or [], review_findings) == _review_digest(
                fetched[item], review_findings
            )
        else:
            same = again == fetched[item]
        if not same:
            return _unknown("evidence_changed_during_evaluation", current_head=final_head)

    commit_heads: list[str] = []
    for item in fetched["commits"]:
        sha = item.get("sha")
        if not isinstance(sha, str):
            return _unknown("commits_malformed", current_head=test_head)
        commit_heads.append(sha)

    return evaluate_evidence(
        current_head=test_head,
        commit_heads=commit_heads,
        reviews=fetched["reviews"],
        issue_comments=fetched["comments"],
        changed_files=fetched["files"],
        external_identity_templates=external_identity_templates,
        primary_login=primary_login,
    )


def evaluate_pr(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """`_evaluate_pr_inner`, with the aggregate-budget stop turned into a result.

    The budget stop is an exception rather than a return code so it cannot be
    mistaken for one endpoint failing: it must abandon the whole lookup, and a
    sentinel return would have to be re-checked at every call site — which is
    the shape that lets one missed check issue another 8-second call.
    """
    try:
        return _evaluate_pr_inner(*args, **kwargs)
    except _BudgetExhausted:
        return _unknown("lookup_budget_exhausted")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True)
    # `default=None`, NOT `[]`. An empty list is a real value meaning "no
    # reviewer identities", so passing it suppresses the `None` branch that
    # loads `report_identity_template` from the shipped and install-local
    # config. On an install with a configured secondary reviewer the CLI then
    # omitted those reviewed heads and could report standing authorization
    # while the hook callers — which do take the `None` branch — reported that
    # approval was required. Two answers to the same question from one module.
    parser.add_argument("--external-identity-template", action="append", default=None)
    args = parser.parse_args(argv)
    result = evaluate_pr(
        args.repo,
        args.pr,
        external_identity_templates=args.external_identity_template,
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
