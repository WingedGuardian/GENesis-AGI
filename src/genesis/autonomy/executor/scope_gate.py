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

# Subtrees denied even though a broader allowed tree contains them.
# docs narrative/history dirs are human territory; src/genesis/mcp/health/
# is the ADMIN/cognitive-control tool surface (settings_update's readonly
# registry, session_control, ego tools, follow-up tools, ...) — a build may
# add new MCP modules under src/genesis/mcp/, but never modify the admin
# server's toolset. Widening is a human PR on Stage-1 evidence.
DENIED_SUBTREES: tuple[str, ...] = (
    "docs/journey/",
    "docs/reflections/",
    "docs/case-studies/",
    "docs/superpowers/",
    "src/genesis/mcp/health/",
)

# Basename glob patterns denied EVERYWHERE, even inside allowed trees.
# Matched against the LOWERCASED basename — fnmatch is case-sensitive on
# POSIX, and `SECRETS.ENV` must not slip past a lowercase-only pattern.
DENIED_BASENAME_GLOBS: tuple[str, ...] = (
    "*.env",
    "*.env.*",     # .env.local / .env.production / config.env.example
    ".env*",
    "secrets*",
    "*secret*",
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
    # Case-insensitive throughout: fnmatch does NOT fold case on POSIX, and
    # `SECRETS.ENV` / `Settings.JSON` must hit the same walls as lowercase.
    basename = path.rsplit("/", 1)[-1].lower()
    for pattern in DENIED_BASENAME_GLOBS:
        if fnmatch(basename, pattern):
            return f"denied filename pattern {pattern!r}"
    lowered = path.lower()
    for token in DENIED_PATH_SUBSTRINGS:
        if token in lowered:
            return f"denied path token {token!r}"
    for tree in DENIED_SUBTREES:
        if lowered.startswith(tree):
            return f"denied subtree {tree!r}"
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


_SYMLINK_MODE = "120000"


def evaluate_raw_diff(raw_lines: list[str]) -> ScopeGateResult:
    """Judge ``git diff --raw <base> HEAD`` output.

    Path-only gating can't see FILE CONTENT, so a symlink added inside an
    allowed tree could point anywhere on the host. ``--raw`` exposes the
    destination file mode; any entry whose new mode is 120000 (symlink) is
    blocked outright, then the remaining paths go through the normal
    allowlist. Unparseable raw lines are treated as paths (which blocks
    them) — never silently skipped.
    """
    paths: list[str] = []
    symlinks: list[str] = []
    for raw in raw_lines:
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        if not line.startswith(":"):
            paths.append(line)  # unexpected shape — let the allowlist judge it
            continue
        meta, sep, path = line.partition("\t")
        if not sep or not path.strip():
            paths.append(line)  # malformed — blocks as an unparseable path
            continue
        fields = meta.split()
        new_mode = fields[1] if len(fields) >= 2 else ""
        paths.append(path)
        if new_mode == _SYMLINK_MODE:
            symlinks.append(path)

    result = evaluate_scope(paths)
    if symlinks:
        blocked = [f"{p} (symlink — content-level escape)" for p in symlinks]
        return ScopeGateResult(
            allowed=False,
            reason=f"{len(symlinks)} symlink(s) in diff — blocked regardless of path",
            blocked_paths=blocked + result.blocked_paths,
            checked_paths=result.checked_paths,
        )
    return result
