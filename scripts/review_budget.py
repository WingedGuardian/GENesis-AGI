#!/usr/bin/env python3
"""Authoritative review-round budget from pull-request evidence.

The local review-state counter is deliberately unsuitable for this job: it
tracks a defect-bearing streak, while this policy counts distinct commit heads
that an external reviewer actually reviewed.  This module is stdlib-only so the
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
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
CODEX_REVIEW_BOT = "chatgpt-codex-connector[bot]"
MAX_PR_COMMITS_RESPONSE = 250
MAX_PR_FILES_RESPONSE = 3000

STANDING_REVIEWED_HEAD_LIMIT = 4
GATE_DISCOVERY_ROUND_LIMIT = 2
STRONGLY_DISCOURAGED_REVIEWED_HEADS = 5

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
                if raw.startswith('"'):
                    value = json.loads(raw)
                elif raw.startswith("'") and raw.endswith("'"):
                    value = raw[1:-1].replace("''", "'")
                else:
                    value = raw.split(" #", 1)[0].strip()
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


def evaluate_evidence(
    *,
    current_head: str,
    commit_heads: Sequence[object],
    codex_reviews: Sequence[Mapping[str, object]],
    issue_comments: Sequence[Mapping[str, object]],
    changed_files: Sequence[Mapping[str, object] | str],
    external_identity_templates: Sequence[str] = (),
) -> dict[str, Any]:
    """Evaluate already-fetched PR evidence without I/O.

    ``approval_required`` is the review-request decision.  Commit policy differs
    on gate PRs: the second-round fix and its confirmation are part of round two,
    so ``commit_approval_required`` begins after the confirmation has produced a
    third reviewed head.
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

    identities: list[re.Pattern[str]] = []
    for template in external_identity_templates:
        pattern = _identity_regex(template)
        if pattern is None:
            return _unknown("invalid_external_identity_template", current_head=head)
        identities.append(pattern)

    reviewed: set[str] = set()
    for item in codex_reviews:
        if not isinstance(item, Mapping):
            return _unknown("malformed_review_record", current_head=head)
        login = item.get("login")
        if not isinstance(login, str):
            return _unknown("malformed_review_record", current_head=head)
        if login != CODEX_REVIEW_BOT:
            continue
        resolved, error = _resolve_sha(str(item.get("commit_id") or ""), commits)
        if error:
            return _unknown(error, current_head=head)
        reviewed.add(resolved or "")

    comment_bodies: list[str] = []
    confirmation_requested = False
    for item in issue_comments:
        if not isinstance(item, Mapping):
            return _unknown("malformed_comment_record", current_head=head)
        login, author_type, body = item.get("login"), item.get("type"), item.get("body")
        if (
            not isinstance(login, str)
            or not isinstance(author_type, str)
            or not isinstance(body, str)
        ):
            return _unknown("malformed_comment_record", current_head=head)
        comment_bodies.append(body)
        if any(m.group(1).lower() == head for m in _CONFIRMATION_RE.finditer(body)):
            confirmation_requested = True
        if login == CODEX_REVIEW_BOT and author_type == "Bot" and _CODEX_CLEAN_RE.search(body):
            match = _REVIEWED_COMMIT_RE.search(body)
            if match is None:
                return _unknown("clean_comment_missing_head", current_head=head)
            resolved, error = _resolve_sha(match.group(1), commits)
            if error:
                return _unknown(error, current_head=head)
            reviewed.add(resolved or "")

    for pattern in identities:
        for body in comment_bodies:
            for match in pattern.finditer(body):
                resolved, error = _resolve_sha(match.group(1), commits)
                if error:
                    return _unknown(error, current_head=head)
                reviewed.add(resolved or "")

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
    heads = sorted(h for h in reviewed if h)
    count = len(heads)
    current_reviewed = head in reviewed
    confirmation_exempt = (
        gate_surface
        and count == GATE_DISCOVERY_ROUND_LIMIT
        and not current_reviewed
        and not confirmation_requested
    )
    if gate_surface:
        request_approval = count >= GATE_DISCOVERY_ROUND_LIMIT and not confirmation_exempt
        commit_approval = count > GATE_DISCOVERY_ROUND_LIMIT
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


def evaluate_pr(
    repo: str,
    pr: int | str,
    *,
    external_identity_templates: Sequence[str] | None = None,
    runner: Runner = _default_runner,
    timeout_for: Callable[[float], float] | None = None,
) -> dict[str, Any]:
    """Fetch and evaluate one PR.  Every failed endpoint yields ``unknown``."""
    if not repo or "/" not in repo or not str(pr).isdigit():
        return _unknown("invalid_pr_identity")

    if external_identity_templates is None:
        external_identity_templates, config_error = configured_external_identity_templates()
        if config_error:
            return _unknown(config_error)

    timeout_for = timeout_for or (lambda seconds: seconds)

    def run(argv: list[str], seconds: float) -> tuple[int, str, str]:
        try:
            return runner(argv, timeout=timeout_for(seconds))
        except Exception:
            return 1, "", "runner_failed"

    test_head = os.environ.get("_TEST_REVIEW_BUDGET_HEAD")
    if test_head is None:
        rc, head_raw, _ = run(
            [
                "gh",
                "pr",
                "view",
                str(pr),
                "--repo",
                repo,
                "--json",
                "headRefOid",
                "--jq",
                ".headRefOid",
            ],
            6,
        )
        if rc != 0:
            return _unknown("head_unreadable")
        test_head = head_raw.strip()

    endpoints = (
        (
            "reviews",
            "_TEST_GH_CODEX_REVIEWS",
            [
                "gh",
                "api",
                f"repos/{repo}/pulls/{pr}/reviews",
                "--paginate",
                "--jq",
                ".[] | {login: .user.login, commit_id: .commit_id, state: .state}",
            ],
        ),
        (
            "comments",
            "_TEST_GH_CODEX_COMMENTS",
            [
                "gh",
                "api",
                f"repos/{repo}/issues/{pr}/comments",
                "--paginate",
                "--jq",
                ".[] | {login: .user.login, type: .user.type, body: .body}",
            ],
        ),
        (
            "files",
            "_TEST_REVIEW_BUDGET_FILES",
            [
                "gh",
                "api",
                f"repos/{repo}/pulls/{pr}/files",
                "--paginate",
                "--jq",
                ".[] | {filename: .filename, previous_filename: .previous_filename}",
            ],
        ),
        (
            "commits",
            "_TEST_REVIEW_BUDGET_COMMITS",
            [
                "gh",
                "api",
                f"repos/{repo}/pulls/{pr}/commits",
                "--paginate",
                "--jq",
                ".[] | {sha: .sha}",
            ],
        ),
    )
    fetched: dict[str, list[dict[str, Any]]] = {}
    for name, env_name, argv in endpoints:
        raw = os.environ.get(env_name)
        if raw is None:
            rc, raw, _ = run(argv, 8)
            if rc != 0:
                return _unknown(f"{name}_unreadable", current_head=test_head)
        rows, error = _json_lines(raw, name)
        if error:
            return _unknown(error, current_head=test_head)
        fetched[name] = rows or []

    # Both REST endpoints have documented hard response ceilings. Landing
    # exactly on one cannot prove the evidence is complete, even with
    # ``--paginate``, so the budget is unknown rather than under-counted or
    # misclassified as an ordinary PR.
    if len(fetched["commits"]) >= MAX_PR_COMMITS_RESPONSE:
        return _unknown("commits_response_truncated", current_head=test_head)
    if len(fetched["files"]) >= MAX_PR_FILES_RESPONSE:
        return _unknown("files_response_truncated", current_head=test_head)

    final_head = os.environ.get("_TEST_REVIEW_BUDGET_HEAD_AFTER")
    if final_head is None and os.environ.get("_TEST_REVIEW_BUDGET_HEAD") is not None:
        final_head = test_head
    if final_head is None:
        rc, head_raw, _ = run(
            [
                "gh",
                "pr",
                "view",
                str(pr),
                "--repo",
                repo,
                "--json",
                "headRefOid",
                "--jq",
                ".headRefOid",
            ],
            6,
        )
        if rc != 0:
            return _unknown("final_head_unreadable", current_head=test_head)
        final_head = head_raw.strip()
    if final_head != test_head:
        return _unknown("head_changed_during_evaluation", current_head=final_head)

    commit_heads: list[str] = []
    for item in fetched["commits"]:
        sha = item.get("sha")
        if not isinstance(sha, str):
            return _unknown("commits_malformed", current_head=test_head)
        commit_heads.append(sha)

    return evaluate_evidence(
        current_head=test_head,
        commit_heads=commit_heads,
        codex_reviews=fetched["reviews"],
        issue_comments=fetched["comments"],
        changed_files=fetched["files"],
        external_identity_templates=external_identity_templates,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True)
    parser.add_argument("--external-identity-template", action="append", default=[])
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
