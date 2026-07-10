#!/usr/bin/env python3
"""PostToolUse Edit|Write hook: ruff format + autofix, then ADVISORY report.

Replaces the former inline settings.json ruff hook, which formatted and
auto-fixed silently — unfixable diagnostics never reached the model, so
edit-time feedback was fix-what-ruff-can and hide the rest. This hook keeps
the exact same mutation behavior (format, then ``check --fix``) and then
surfaces whatever ruff could NOT fix as PostToolUse ``additionalContext``.

It is ONE hook rather than a fixer + a separate reporter because Claude Code
runs all matching hooks for an event IN PARALLEL with no ordering guarantee
(hooks docs) — a separate reporter would race the fixer and report
diagnostics that are about to disappear.

Contract (binding):
- ADVISORY ONLY. Never exits non-zero on diagnostics, never blocks — the
  enforcement points are the commit gate (review_enforcement_commit.py) and
  CI lint. An edit-blocking lint gate is the auto-throttling anti-pattern.
- Fail-open: any unexpected error exits 0 silently.
- Model feedback uses the documented PostToolUse JSON channel
  (``hookSpecificOutput.additionalContext``); plain stdout does not reach
  the model. Precedent: scripts/content_safety_hook.py.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# Per-invocation cap, not a policy timeout: ruff on a single file is
# ~50-300ms; the settings.json hook timeout (10, in SECONDS per the CC hooks
# docs) is the outer kill. This cap just keeps one wedged invocation from
# eating the whole budget so the later calls (and the advisory) still run.
# Fail-open on expiry.
_RUFF_TIMEOUT_S = 1.5

# Advisory payload cap — enough to act on, small enough not to flood context.
_MAX_DIAG_LINES = 12

_ADVISORY_FOOTER = (
    "(Advisory only — fix these now if they are in scope for your current "
    "task, otherwise leave them; the commit-time review gate and CI lint are "
    "the enforcement points. Do not treat this as a blocking instruction or "
    "derail into unrelated lint cleanup.)"
)


def _ruff() -> Path | None:
    ruff = Path(sys.executable).parent / "ruff"
    return ruff if ruff.is_file() else None


def _run(ruff: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 - fixed venv binary, no shell
        [str(ruff), *args],
        capture_output=True,
        text=True,
        timeout=_RUFF_TIMEOUT_S,
    )


def _process(data: dict) -> None:
    if data.get("tool_name") not in ("Edit", "Write"):
        return
    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return
    file_path = tool_input.get("file_path") or ""
    if not file_path.endswith(".py"):
        return
    path = Path(file_path)
    if not path.is_file():
        return
    ruff = _ruff()
    if ruff is None:
        return

    # Same mutations the old inline hook performed, in the same order.
    _run(ruff, "format", "--quiet", str(path))
    _run(ruff, "check", "--fix", "--quiet", str(path))

    # What remains after autofix is what the model should hear about.
    result = _run(ruff, "check", "--output-format", "concise", str(path))
    if result.returncode == 0:
        return
    diagnostics = [ln for ln in result.stdout.splitlines() if ln.strip()]
    # Drop ruff's "Found N errors" / fix-hint summary lines.
    diagnostics = [
        ln for ln in diagnostics
        if not ln.startswith(("Found ", "[*]", "No fixes"))
    ]
    if not diagnostics:
        return

    total = len(diagnostics)
    shown = diagnostics[:_MAX_DIAG_LINES]
    if total > len(shown):
        shown.append(f"... and {total - len(shown)} more")
    context = (
        f"[ruff advisory] {total} unresolved diagnostic(s) in {file_path} "
        "after autofix:\n" + "\n".join(shown) + "\n" + _ADVISORY_FOOTER
    )
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": context,
            }
        },
        sys.stdout,
    )


def main() -> int:
    try:
        data = json.loads(sys.stdin.read())
        _process(data)
    except Exception:  # noqa: BLE001 - hooks fail open, never crash the session
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
