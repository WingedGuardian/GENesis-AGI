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
from git_repo_selection import REPO_VARS  # noqa: E402
from review_deadline import Deadline  # noqa: E402
from shell_parse import (  # noqa: E402
    _GH_ALL_VALUE_FLAGS,
    _GH_FLAG_TABLE,
    _argv,
    _gh_option,
    analyze_checked,
    gh_command,
    gh_pr_subcommand,
    git_subcommand,
    git_subcommand_index,
    mentions,
)


class Fixable(str):
    """A denial the agent can clear by rewriting the command and running it again.

    Every other denial is a STOP for the user: a review-budget or approval
    boundary, or evidence the adapter cannot read. Telling an agent to stop on a
    reason it could fix by itself stalls the work; telling it to retry a budget
    stop would invite it to route around the boundary. The command is still
    re-checked in full when it is run again.
    """


def _non_mutating(seg) -> bool:
    """Prove help/dry-run from option positions, never from message/path text."""
    argv = seg.argv
    if git_subcommand(argv) == "commit":
        i = git_subcommand_index(argv) + 1
        dry_run = False
        while i < len(argv):
            tok = argv[i]
            if tok == "--":
                break
            if tok in {"--help", "-h"}:
                return True
            if tok in {"--dry-run", "--no-dry-run"}:
                dry_run = tok == "--dry-run"
            elif tok.startswith("--"):
                name = tok.split("=", 1)[0]
                if name == "--gpg-sign":
                    # Git's optional key is attached, never the next token.
                    # Do not hide a following --no-dry-run behind this option.
                    i += 1
                    continue
                if name in commits._COMMIT_VALUE_LONG:
                    i += 1 if "=" in tok else 2
                    continue
                # Unknown option arity cannot establish a read-only mode.
                return False
            elif tok.startswith("-"):
                consumes_next = False
                for j, ch in enumerate(tok[1:], 1):
                    if ch in commits._COMMIT_ARG_SHORT:
                        consumes_next = j == len(tok) - 1
                        break
                    if ch == "S":  # optional attached signing key
                        break
                    if ch == "h":
                        return True
                    if ch not in "aienopqsvuz":
                        return False
                i += 2 if consumes_next else 1
                continue
            i += 1
        return dry_run
    if gh_pr_subcommand(argv) == "comment":
        invocation = gh_command(argv)
        if invocation is None or invocation.unmodelled:
            return False
        i = 1
        while i < len(argv):
            if argv[i] == "--":
                break
            flags = _GH_ALL_VALUE_FLAGS if i < invocation.path_end else _GH_FLAG_TABLE[("pr", "comment")][0]
            name, glued = _gh_option(argv[i], flags)
            if name in flags:
                i += 1 if glued is not None else 2
                continue
            if argv[i] in {"--help", "-h"}:
                return True
            i += 1
    return False


def _commit_cwd(seg) -> str | None:
    """Require one absolute literal -C: native hook cwd omits workdir overrides."""
    index = git_subcommand_index(seg.argv)
    options = seg.argv[1:index]
    if len(options) == 2 and options[0] == "-C":
        target = options[1]
    else:
        return None
    if not os.path.isabs(target) or any(ch in target for ch in "$`\\*?[]{}()<>\n"):
        return None
    return os.path.abspath(target) if Path(target).is_dir() else None


def _request_identity_reason(argv: list[str]) -> Fixable | None:
    """Admit only identities the shared hostless budget lookup resolves correctly.

    gh 2.100.0 selects a PR URL before --repo; the shared helper does the reverse
    and drops hostnames. Mixed selectors and foreign hosts require a rewrite.
    """
    token, _ = requests._comment_positional(argv)
    repo = requests._comment_repo_value(argv)
    if requests._unresolvable_identity(argv) is None and token is not None:
        if re.fullmatch(r"[0-9]+", token) and repo is not None:
            if re.fullmatch(r"(?:github\.com/)?[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repo):
                return None
        elif (repo is None and token.startswith(("https://github.com/", "http://github.com/"))
              and requests._LITERAL_URL_RE.fullmatch(token)):
            return None
    return Fixable(
        "Use one literal PR number with --repo OWNER/REPO (github.com), or one "
        "literal github.com PR URL without --repo. Branches, implicit targets, "
        "expansions, mixed URL/repository selectors and other hosts are unsupported."
    )


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
            return Fixable(f"Cannot classify this shell action: {blind.cause}. {blind.hint}")
        return None
    actions = [
        seg for seg in segs if not _non_mutating(seg) and (
            git_subcommand(seg.argv) == "commit" or (
                gh_pr_subcommand(seg.argv) == "comment"
                and requests._comment_review_request(seg.argv)[2] is not False
            )
        )
    ]
    if not actions:
        return None
    # Exact raw/resolved argv agreement excludes stripped env assignments and
    # wrappers. Single top-level commands exclude prior/conditional shell state.
    if len(segs) != 1 or actions[0].depth or _argv(actions[0].raw) != actions[0].argv:
        return Fixable("Run the action as one standalone, unwrapped git/gh command.")
    if any(os.environ.get(name) for name in (*REPO_VARS, "GH_REPO")):
        return "Inherited repository-selection overrides cannot be resolved."
    if os.environ.get("GH_HOST", "github.com") not in {"", "github.com"}:
        return "Inherited GH_HOST must select github.com for the shared budget lookup."
    seg = actions[0]
    if git_subcommand(seg.argv) == "commit":
        effective_cwd = _commit_cwd(seg)
        if effective_cwd is None:
            return Fixable(
                "Use git -C followed by one literal absolute directory; "
                "other global options are unsupported."
            )
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
    else:
        identity_reason = _request_identity_reason(seg.argv)
        if identity_reason is not None:
            return identity_reason

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
    # "deny" on stdout tells the launcher this denial was explained here. An
    # interpreter that never ran this file (a missing or unreadable script also
    # exits 2) prints nothing to stdout, so it cannot be mistaken for one.
    print("deny")
    if isinstance(reason, Fixable):
        print(
            f"BLOCKED: {reason}\nThis is not an approval stop. Rewrite the command "
            "as described and run it again; the rewritten command is checked in full.",
            file=sys.stderr,
        )
        return 2
    print(
        f"BLOCKED: {reason}\nSTOP and handoff to the user. This Codex CLI adapter "
        "cannot obtain fresh native approval. Do not retry, add an override, "
        "or treat earlier approval as permission for this action.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
