#!/usr/bin/env python3
"""Budget-only Codex adapter: allow recognized actions or stop for user handoff.

Internal dependencies on the Claude guards are intentional: they own command
resolution, PR scope, counting and exemptions. No review markers, Genesis session
lifecycle, approval receipts or new counting policy are introduced here.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "hooks"))

import git_push_guard as requests  # noqa: E402
import review_enforcement_commit as commits  # noqa: E402
import review_state as state  # noqa: E402
from git_repo_selection import REPO_VARS, raw_sets_repo_env, seg_redirects_repo  # noqa: E402
from review_deadline import Deadline  # noqa: E402
from shell_parse import analyze_checked, gh_pr_subcommand, git_subcommand, mentions  # noqa: E402


def decide(payload: object) -> str | None:
    """None permits execution; a reason requires denial, never a native ask."""
    if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return "Unreadable Codex shell-hook payload."
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    cwd = payload.get("cwd")
    if not isinstance(command, str) or not isinstance(cwd, str) or not Path(cwd).is_dir():
        return "Cannot resolve this shell action and its working directory."

    segs, blind = analyze_checked(command)
    if blind is not None:
        if mentions(command, re.compile(r"\b(?:git|gh)\b")):
            return f"Cannot classify this shell action: {blind.cause}. {blind.hint}"
        return None
    commit_segs = [s for s in segs if git_subcommand(s.argv) == "commit"]
    if commit_segs:
        # The lookup samples pre-command state. Other git/gh operations could
        # change that state or spend budget in the same command. Require separate
        # invocations rather than constructing another shell-state interpreter.
        other_actions = [
            s for s in segs
            if s not in commit_segs and (s.exe in {"git", "gh"} or gh_pr_subcommand(s.argv))
        ]
        if len(commit_segs) != 1 or other_actions:
            return "Run one commit separately from other git/gh actions."
        if any(os.environ.get(name) for name in REPO_VARS) or any(
            seg_redirects_repo(s) or raw_sets_repo_env(s.raw) for s in segs
        ):
            return "Repository-selection overrides cannot be resolved; use a literal git -C directory."
        effective_cwd = commits._effective_diff_cwd(command, payload, segs, commit_seg=commit_segs[0])
        if not isinstance(effective_cwd, str):
            return "Cannot resolve the commit directory; use a literal git -C directory."
        deadline = Deadline.after(commits._COMMIT_HOOK_REGISTERED_TIMEOUT - 0.5)
        branch = state.get_current_branch(cwd=effective_cwd, deadline=deadline.expires_at)
        result = commits._branch_review_budget(
            effective_cwd, branch, deadline=deadline, segs=segs, command=command
        )
        deadline.timeout(0.001)
        if result is not None and (
            result.get("status") != "ok" or result.get("commit_approval_required") is not False
        ):
            return commits._commit_budget_reason(result)

    decision, reason = requests._check_codex_round_escalation(segs, command, payload)
    if decision != "allow":
        return reason or "The shared review-request policy did not permit this action."
    return None


def main() -> int:
    try:
        reason = decide(json.load(sys.stdin))
    except Exception:  # Unreadable evidence or a broken dependency must not allow.
        reason = "The review-budget evaluator could not produce a reliable decision."
    if reason is None:
        print("allow")
        return 0
    print(
        f"BLOCKED: {reason}\nSTOP and handoff to the user. This Codex CLI adapter "
        "cannot obtain fresh native approval. Do not retry, add an override, "
        "or treat earlier approval as permission for this action.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
