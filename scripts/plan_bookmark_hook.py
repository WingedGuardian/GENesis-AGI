#!/usr/bin/env python3
"""PostToolUse hook for ExitPlanMode — auto-bookmark plan sessions.

When a plan is approved (ExitPlanMode fires), this hook:
1. Writes a pending bookmark file for the MCP server to consume
2. Outputs the architecture review recommendation as additionalContext

The MCP server picks up the pending file on the next tool call and
creates the bookmark programmatically — no LLM dependency.

Reads hook input from stdin as JSON:
  {"tool_name": "ExitPlanMode", "tool_input": {...}, "tool_output": {...}}

Output format (CC PostToolUse hook contract):
{
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "..."
  }
}
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

# The architecture review recommendation
_ARCH_REVIEW = (
    "RECOMMENDED: You have exited plan mode. Before implementation, run an "
    "architecture review proportional to the plan scope. For small plans "
    "(1-2 files, wiring changes): dispatch a single code-architect agent to "
    "check dependencies, edge cases, and DRY violations. For medium plans "
    "(3-10 files, new components): run a focused CEO premise challenge + eng "
    "architecture review. For large plans (10+ files, new systems): run the "
    "full /autoplan pipeline (CEO \u2192 design \u2192 eng review). Always surface "
    "findings and update the plan before starting implementation. The goal is "
    "catching real issues, not ceremony."
)

_EXECUTION_PROTOCOL = (
    "EXECUTION PROTOCOL — You MUST follow these steps IN ORDER before "
    "writing any code:\n"
    "1. CREATE WORKTREE — git worktree add .claude/worktrees/<scope>-<desc> "
    "-b <scope>/<desc>. Work inside the worktree. NEVER commit to main.\n"
    # "STATE CONFIDENCE - explicit percentages for each part of the plan" used to
    # be step 2 here. It was a PLAN-level instruction delivered at PostToolUse,
    # i.e. after the plan had already been shown and approved - the wrong-moment
    # bug that scripts/hooks/plan_confidence_reminder.py exists to fix. That hook
    # now owns the ask, and it fires at the plan-mode boundaries instead: at
    # EnterPlanMode, where it still reaches the plan being written, and at
    # ExitPlanMode, where it reaches the revision and the next plan. Repeating it
    # here would be a second copy arriving strictly later than both. Step 2 below
    # is NOT the same thing and stays: reading the files you are about to edit is
    # implementation diligence, at a moment where it is still actionable.
    "2. DUE DILIGENCE — Read every file you plan to modify. Verify functions "
    "and classes exist as expected. Check git log for recent conflicts.\n"
    "3. CONFIRM PLAN VALIDITY — Verify plan is still valid against current "
    "code. If anything changed since planning, update the plan first.\n"
    "4. WORK THE PLAN IN ORDER — one step at a time, verifying each before the "
    "next; dispatch subagents for work that would bloat this context. The "
    "`superpowers` plugin's executing-plans / subagent-driven-development skills "
    "do this well where the install has it (optional; not present everywhere).\n\n"
    "DETERMINISTIC CHECKPOINTS — After completing each task:\n"
    "- Run: ruff check <modified_files>\n"
    "- Run: pytest <relevant_test_file> -v\n"
    "- Commit: git commit with conventional prefix — uncommitted work is "
    "invisible work.\n"
    "- Review: if the task changed more than 3 files, dispatch a "
    "code-reviewer agent.\n"
    "Do NOT skip these checkpoints. They are the minimum bar for quality."
)

_GENESIS_DIR = Path.home() / ".genesis"
_PENDING_FILE = _GENESIS_DIR / "plan_bookmark_pending.json"


def _outside_fences(lines: list[str]):
    """Yield `(index, line)` for lines that are NOT inside a fenced block.

    A plan documents a convention by SHOWING it, so the divider and the title
    heading both appear inside ``` examples in perfectly ordinary plans — this
    repo's own reference doc does exactly that. Matching those is how a plan
    truncates itself at its own illustration.

    Fence detection is the CommonMark-ish subset that markdown writers actually
    use: a line whose first non-space run is three or more backticks or tildes
    toggles the state. The info string is ignored, and a closing fence of a
    different character does not close — ``` inside a ~~~ block is content.
    """
    fence_char = ""
    for idx, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped[:3] in ("```", "~~~"):
            char = stripped[0]
            if not fence_char:
                fence_char = char
                continue
            if char == fence_char:
                fence_char = ""
            continue
        if not fence_char:
            yield idx, line


def _live_half(content: str) -> str:
    """The part of a plan above its superseded-content divider.

    The convention is documented in the genesis-development skill
    (`references/plan-docs.md`): a heading containing SUPERSEDED BELOW divides
    live plan content from archaeology kept for provenance. No divider means
    the whole document is live, which is also the correct reading for every
    plan written before the convention existed.

    Matched on the WORDS at a heading line, not on the decorative rule the
    template draws around them — the box characters are ornament and a plan
    that omits them still means it. Anchored at column zero on a `#` heading so
    the phrase quoted inside a paragraph cannot truncate the document, and
    skipped inside FENCED blocks so a plan that documents the convention by
    showing it — which is how anyone would document it, and how the reference
    doc does — does not truncate itself at its own example.
    """
    lines = content.splitlines()
    for idx, line in _outside_fences(lines):
        if line.startswith("#") and "SUPERSEDED BELOW" in line:
            return "\n".join(lines[:idx])
    return content


def _classify_plan_complexity(plan_path: str) -> str:
    """Classify plan as small/medium/large based on task and step count."""
    if not plan_path:
        return "unknown"
    try:
        content = Path(plan_path).read_text(encoding="utf-8")
    except OSError:
        return "unknown"

    # Count tasks (### Task headers) and steps (- [ ] checkboxes) in the LIVE
    # half only. A long-running plan keeps superseded sections below a
    # divider for provenance; counting those makes a small current plan
    # classify as `large`, and `_plan_instructions` then injects the full
    # planning pipeline on the strength of work that is already done. The
    # archaeology grows without bound, so this only ever gets worse.
    content = _live_half(content)
    task_count = content.count("### Task")
    step_count = content.count("- [ ]")

    if task_count <= 2 and step_count <= 6:
        return "small"
    elif task_count <= 5 and step_count <= 15:
        return "medium"
    else:
        return "large"



def _extract_plan_info(hook_input: dict) -> tuple[str, str]:
    """Extract plan file path and title.

    Tries hook input first, falls back to most recently modified file
    in ~/.claude/plans/.

    Returns (plan_path, title). Both may be empty if not available.
    """
    tool_input = hook_input.get("tool_input", {})
    tool_output = hook_input.get("tool_output", {})

    plan_path = ""
    title = ""

    # Try to find plan path in hook input/output
    for source_str in (str(tool_output), str(tool_input)):
        if ".claude/plans/" in source_str:
            match = re.search(r"(/[^\s\"']+\.claude/plans/[^\s\"']+\.md)", source_str)
            if match:
                plan_path = match.group(1)
                break

    # Fallback: find most recently modified plan file
    if not plan_path:
        plans_dir = Path.home() / ".claude" / "plans"
        if plans_dir.is_dir():
            try:
                candidates = sorted(
                    plans_dir.glob("*.md"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                if candidates:
                    plan_path = str(candidates[0])
            except OSError:
                pass

    # Read the plan title from the first heading
    if plan_path:
        try:
            path = Path(plan_path)
            if path.exists():
                for line in _title_candidate_lines(path.read_text()):
                    if line.startswith("#") and not line.startswith("<!--"):
                        title = line.lstrip("#").strip()
                        break
        except OSError:
            pass

    return plan_path, title


def _title_candidate_lines(text: str) -> list[str]:
    """The lines a plan's `# ` title can plausibly be on, stripped.

    YAML frontmatter is skipped first. Without that, a plan carrying the
    structured header (13 lines including both fences) pushes its heading past
    the scan window, `title` stays empty, and the bookmark becomes unfindable
    by keyword — silently, since nothing raises and nothing logs. MEASURED
    2026-09-15 against a real headered plan: extracted title was ``''``.

    The window stays bounded rather than scanning the file: a plan doc runs to
    thousands of lines, and a `# ` heading that far down is a section, not the
    document's title. Ten lines is kept from the original — the point of this
    change is where the window STARTS, not how wide it is.

    A leading `---` only opens frontmatter if a closing fence is actually
    found; otherwise it is a thematic break and the text is scanned as-is.

    BOTH fences are matched at COLUMN ZERO, without stripping, because that is
    what YAML frontmatter is — and `.strip()` gets all three cases wrong:
      - an INDENTED thematic break (` ---`) would read as an opener, so
        `` ---\\n# Actual title\\n…\\n---`` loses the title it used to find;
      - an indented `  ---` inside a block scalar would read as the closing
        fence, starting the scan window inside the YAML;
      - and there is no third spelling to be lenient toward: the delimiter is
        exactly three hyphens at column zero.

    The closing fence is searched for across the whole document rather than a
    fixed number of lines. An arbitrary bound reintroduces the very bug this
    function exists to fix the moment a header grows past it — and the header's
    id lists are documented as growing — while buying nothing: the lines are
    already in memory, so the scan is a walk over a list we have.

    And a matched PAIR of column-zero rules is still not enough on its own: a
    plan that opens with a thematic break and uses another later has the same
    shape as frontmatter, and skipping between them loses a real title. There is
    no lexical tell that separates the two — so the block is PARSED. Frontmatter
    is YAML by definition, so content that does not load as a YAML MAPPING is
    not frontmatter, whatever it is fenced by. That replaces a fourth heuristic
    with the actual definition; the three heuristics above were each wrong in a
    different direction before this.
    """
    lines = text.splitlines()
    if lines and lines[0] == "---":
        for idx in range(1, len(lines)):
            if lines[idx] == "---":
                if _is_yaml_mapping("\n".join(lines[1:idx])):
                    lines = lines[idx + 1 :]
                break
    return [line.strip() for line in lines[:10]]


def _is_yaml_mapping(block: str) -> bool:
    """Whether `block` loads as a YAML mapping — i.e. is really frontmatter.

    Degrades to TRUE when PyYAML is unavailable, which keeps the previous
    paired-fence behaviour rather than newly treating every header as prose. A
    plan whose header stops being skipped would lose its title again, the exact
    regression this module exists to fix; a plan whose thematic rules are
    wrongly skipped loses a title it never reliably had. Fail toward the
    behaviour that is already shipped and tested.
    """
    try:
        import yaml
    except ImportError:
        return True
    try:
        return isinstance(yaml.safe_load(block), dict)
    except yaml.YAMLError:
        # Unparseable is a positive answer to "is this frontmatter?": no.
        return False


def _guess_session_id() -> str:
    """Best-effort session ID from most recently modified sessions directory."""
    sessions_dir = _GENESIS_DIR / "sessions"
    if not sessions_dir.exists():
        return ""

    # Skip Genesis background sessions (env var check)
    if os.environ.get("GENESIS_CC_SESSION") == "1":
        return ""

    try:
        # Find the most recently modified session directory
        candidates = sorted(
            (d for d in sessions_dir.iterdir() if d.is_dir()),
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            return candidates[0].name
    except OSError:
        pass

    return ""


def main() -> int:
    """Read PostToolUse hook input, write pending bookmark, output arch review."""
    try:
        raw = sys.stdin.read()
        hook_input = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        hook_input = {}

    plan_path, title = _extract_plan_info(hook_input)
    session_id_hint = _guess_session_id()

    # Write pending bookmark file for MCP server to consume
    _GENESIS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        pending_data = {
            "plan_path": plan_path,
            "title": title,
            "session_id_hint": session_id_hint,
            "created_at": datetime.now(UTC).isoformat(),
        }
        _PENDING_FILE.write_text(json.dumps(pending_data))
    except OSError as exc:
        print(f"plan_bookmark_hook: failed to write pending file: {exc}", file=sys.stderr)

    # Classify plan complexity for review depth guidance
    complexity = _classify_plan_complexity(plan_path)
    complexity_note = ""
    if complexity == "small":
        complexity_note = (
            "PLAN COMPLEXITY: small (1-2 tasks). A single code-architect "
            "agent review is sufficient before implementation."
        )
    elif complexity == "medium":
        complexity_note = (
            "PLAN COMPLEXITY: medium (3-5 tasks). Run a focused architecture "
            "review (CEO premise challenge + eng review) before implementation."
        )
    elif complexity == "large":
        complexity_note = (
            "PLAN COMPLEXITY: large (5+ tasks). Run the full /autoplan pipeline "
            "(CEO -> design -> eng review) before implementation."
        )

    # Build structured execution protocol
    reminders = [_EXECUTION_PROTOCOL]
    if complexity_note:
        reminders.append(complexity_note)
    reminders.append(_ARCH_REVIEW)

    # Output all reminders
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": "\n\n".join(reminders),
        }
    }

    json.dump(output, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
