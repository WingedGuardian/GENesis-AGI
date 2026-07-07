"""Diff allowlist gate for build-lane task delivery.

Pure functions only — no I/O, no git. The engine computes the committed
diff's changed paths and asks this module whether the build may leave the
worktree (push + draft PR). Applied ONLY to ``source='build_lane'`` tasks;
user-submitted tasks never pass through here.

Design (fail-closed, allowlist):

- An ALLOWLIST of capability trees ("body" code) is the only way out.
  A denylist would fail OPEN on unenumerated paths; the allowlist fails
  CLOSED — anything not explicitly allowed is blocked, including every
  core subsystem (autonomy/, ego/, inbox/, memory/, runtime/, db/,
  contribution/, scripts/, .claude/, .github/, config/).
- DENY OVERRIDES win even inside allowed trees (secrets, service units,
  settings, approval-related files, and the build lane's own submission
  tool) — the lane must never be able to PR its own gate or its own
  approval surface. This module lives in ``src/genesis/autonomy/``,
  a tree the allowlist does not contain, so a build can never modify
  the gate that judges it.
- An EMPTY diff is blocked: a build that changed nothing has nothing
  deliverable, and pushing an empty branch would masquerade as success.
- Malformed paths (absolute, ``..`` traversal) are blocked, never
  normalized into legality.

Blocked builds are PARKED (no push, no PR, outcome ``scope_blocked``)
with the offending paths listed — widening the allowlist is a deliberate
HUMAN pull request informed by Stage-1 evidence, never an autonomous one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from fnmatch import fnmatch

# Capability trees a build may touch ("body", never "brain").
ALLOWED_TREES: tuple[str, ...] = (
    "src/genesis/modules/",
    "src/genesis/skills/",
    "src/genesis/mcp/",
    "tests/",
    "docs/",
)

# Documentation subtrees that remain human territory (narrative/history);
# builds document capabilities under docs/reference, docs/architecture, etc.
DENIED_DOC_TREES: tuple[str, ...] = (
    "docs/journey/",
    "docs/reflections/",
    "docs/case-studies/",
    "docs/superpowers/",
)

# Basename glob patterns denied EVERYWHERE, even inside allowed trees.
DENIED_BASENAME_GLOBS: tuple[str, ...] = (
    "*.env",
    "secrets*",
    "*.service",
    "*.timer",
    "settings*.json",
)

# Full-path substrings denied everywhere. "approval" covers approval gates,
# approval tools, and approval tests — the lane never touches its own
# authorization surface, not even the test side of it.
DENIED_PATH_SUBSTRINGS: tuple[str, ...] = (
    "approval",
)

# Exact paths denied everywhere: the build lane's own submission door.
DENIED_EXACT_PATHS: frozenset[str] = frozenset({
    "src/genesis/mcp/health/task_tools.py",
})


@dataclass(frozen=True)
class ScopeGateResult:
    """Verdict for one build delivery."""

    allowed: bool
    reason: str
    blocked_paths: list[str] = field(default_factory=list)
    checked_paths: int = 0

    def to_json(self) -> str:
        return json.dumps({
            "allowed": self.allowed,
            "reason": self.reason,
            "blocked_paths": self.blocked_paths,
            "checked_paths": self.checked_paths,
        })


def _deny_reason(path: str) -> str | None:
    """Return why *path* is denied outright, or None."""
    if path.startswith("/") or ".." in path.split("/"):
        return "malformed path"
    if path in DENIED_EXACT_PATHS:
        return "task submission surface"
    basename = path.rsplit("/", 1)[-1]
    for pattern in DENIED_BASENAME_GLOBS:
        if fnmatch(basename, pattern):
            return f"denied filename pattern {pattern!r}"
    lowered = path.lower()
    for token in DENIED_PATH_SUBSTRINGS:
        if token in lowered:
            return f"denied path token {token!r}"
    for tree in DENIED_DOC_TREES:
        if path.startswith(tree):
            return f"denied docs subtree {tree!r}"
    return None


def _is_allowed_tree(path: str) -> bool:
    return any(path.startswith(tree) for tree in ALLOWED_TREES)


def evaluate_scope(changed_paths: list[str]) -> ScopeGateResult:
    """Judge a build's committed diff against the allowlist.

    ``changed_paths`` are repo-relative paths from ``git diff --name-only``.
    Returns a blocked result when the list is empty (nothing deliverable)
    or when ANY path falls outside the allowlist / hits a deny override.
    """
    paths = [p.strip().removeprefix("./") for p in changed_paths if p and p.strip()]
    if not paths:
        return ScopeGateResult(
            allowed=False,
            reason="empty diff — nothing deliverable",
        )

    blocked: list[str] = []
    for path in paths:
        deny = _deny_reason(path)
        if deny is not None:
            blocked.append(f"{path} ({deny})")
        elif not _is_allowed_tree(path):
            blocked.append(f"{path} (outside allowed capability trees)")

    if blocked:
        return ScopeGateResult(
            allowed=False,
            reason=f"{len(blocked)} of {len(paths)} changed paths outside build scope",
            blocked_paths=blocked,
            checked_paths=len(paths),
        )
    return ScopeGateResult(
        allowed=True,
        reason="all changed paths within allowed capability trees",
        checked_paths=len(paths),
    )
