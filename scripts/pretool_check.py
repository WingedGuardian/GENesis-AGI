#!/usr/bin/env python3
"""PreToolUse hook — blocks Write/Edit to CRITICAL protected paths in
AUTONOMOUS (dispatched) sessions.

Called by CC CLI via .claude/settings.json PreToolUse hook.
Reads the CC hook payload from stdin (via hook_input), extracts file_path,
checks against CRITICAL patterns from config/protected_paths.yaml.

Enforcement scope (matches the config's documented intent — CRITICAL paths
"cannot be modified from any relay/chat channel. Only modifiable from direct
CC CLI sessions"): every relay/chat channel reaches the filesystem through a
Genesis-DISPATCHED CC session, which cc/invoker.py stamps with
``GENESIS_CC_SESSION=1``. So the block applies to dispatched sessions only; a
direct interactive CLI session (no stamp — the user is present and sovereign)
is allowed through.

Path matching is TAIL-based: a repo-relative pattern like
``src/genesis/autonomy/protection.py`` matches that suffix under ANY checkout
root (main repo, a linked worktree, a fresh clone) — CC sends ABSOLUTE paths,
and a naive relative fnmatch silently never fired for the repo-relative
patterns (found inert 2026-08-01). Absolute patterns (``/etc/netplan/**``)
match as-given.

Exit codes:
  0 — allow (interactive session, or path is not CRITICAL)
  2 — block (dispatched session + CRITICAL path)

Emits SteerMessage for unified enforcement feedback when the genesis package
is importable; blocking NEVER depends on it (stdlib fallback message
otherwise — a fresh/broken install must not fail open, audit B4).
"""

import os
import sys
from fnmatch import fnmatch
from pathlib import Path

# The shared hook-input helper lives in scripts/hooks/; this script runs from
# scripts/ (a different sys.path[0]), so add the hooks dir before importing it.
sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
from hook_input import field, read_payload, run_guard  # noqa: E402

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "protected_paths.yaml"

# Hardcoded fallback — protects the most dangerous paths even when config is
# missing or corrupted.  Fail-closed: if we can't load the full config, at
# least these patterns are still enforced.
_FALLBACK_CRITICAL = [
    "*/secrets.env",
    ".claude/settings.json",
    "src/genesis/autonomy/protection.py",
    "config/protected_paths.yaml",
    "scripts/systemd/*.template",
]


def _load_critical_patterns() -> list[str]:
    """Load CRITICAL path patterns from config, falling back to hardcoded list.

    ``yaml`` is imported HERE, not at module top level: an import-time failure
    (venv-less install, missing PyYAML) would crash the script before run_guard
    could catch it — historically exit 1 = silent fail-open. Degrading to the
    fallback set keeps the most dangerous paths enforced instead.
    """
    try:
        import yaml

        # `or {}`: an empty YAML file parses to None — degrade to the fallback
        # instead of crashing on None.get (the pre-2026-08 fail-open bug).
        data = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
    except Exception as exc:
        print(
            f"WARNING: protected_paths.yaml load failed ({exc}), using fallback",
            file=sys.stderr,
        )
        return list(_FALLBACK_CRITICAL)
    patterns = []
    for rule in data.get("critical", []):
        patterns.append(rule["pattern"])
    return patterns or list(_FALLBACK_CRITICAL)


def _matches(path: str, patterns: list[str]) -> str | None:
    """Return the matching pattern if ``path`` matches any CRITICAL pattern.

    A pattern is tried against the full path AND every '/'-suffix of it, so a
    repo-relative pattern hits the file under any checkout root. CC delivers
    absolute paths — matching only the verbatim string left every repo-relative
    pattern inert (verified live 2026-08-01: settings.json / protection.py /
    protected_paths.yaml never matched). Suffix matching deliberately
    over-matches a same-named path outside any checkout — acceptable, because
    the block applies only to dispatched sessions, where conservative is right.
    """
    normalized = path.replace("\\", "/")
    parts = [p for p in normalized.split("/") if p]
    candidates = [normalized] + ["/".join(parts[i:]) for i in range(len(parts))]
    for pattern in patterns:
        for cand in candidates:
            if fnmatch(cand, pattern):
                return pattern
        # Handle ** recursive glob (fnmatch's * doesn't cross '/' semantics
        # here are fine, but a bare prefix check keeps legacy behavior).
        if "**" in pattern:
            prefix = pattern.split("**")[0]
            for cand in candidates:
                if cand.startswith(prefix):
                    return pattern
    return None


def _is_dispatched() -> bool:
    """True in a Genesis-dispatched (autonomous/relay) CC session.

    cc/invoker.py stamps ``GENESIS_CC_SESSION=1`` on every dispatched session;
    a user-launched interactive session does not carry it. Mirrors the same
    check in git_push_guard.
    """
    return os.environ.get("GENESIS_CC_SESSION") == "1"


def _block_message(matched: str, file_path: str) -> str:
    """The block text — via SteerMessage when genesis is importable, else a
    plain-text equivalent. Blocking must never depend on the genesis package
    (audit B4: the import lived on the block path, so a fresh/broken install
    crashed → exit 1 → CC ran the Write anyway)."""
    suggestion = (
        "CRITICAL paths cannot be modified from an autonomous/dispatched "
        "session. Ask the user to make this change from an interactive "
        "Claude Code session."
    )
    try:
        from genesis.autonomy.steering import SteerMessage
        from genesis.autonomy.types import ApprovalDecision, EnforcementLayer

        return SteerMessage(
            layer=EnforcementLayer.PERMISSION_GATE,
            rule_id="critical_protected_path",
            decision=ApprovalDecision.BLOCK,
            severity="critical",
            title="CRITICAL protected path",
            context=f"Matches pattern '{matched}'",
            suggestion=suggestion,
            tool_name="Write",
            file_path=file_path,
        ).to_stderr()
    except Exception:
        return (
            f"BLOCKED [critical_protected_path]: {file_path} matches CRITICAL "
            f"pattern '{matched}'.\n  Fix: {suggestion}"
        )


def main() -> int:
    file_path = field(read_payload(), "file_path")
    if not file_path:
        return 0

    if not _is_dispatched():
        # Direct interactive CLI session — the user is present and sovereign;
        # CRITICAL protection targets relay/autonomous channels only (see the
        # config header). Allow.
        return 0

    patterns = _load_critical_patterns()
    matched = _matches(file_path, patterns)
    if matched:
        print(_block_message(matched, file_path), file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    run_guard(main, "pretool_check")
