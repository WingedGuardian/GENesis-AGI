> **Status: COMPLETE.** All 5 tasks implemented 2026-03-29. Checkboxes marked retroactively.

# Claude Setup Portability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Make Genesis's Claude Code harness (hooks, MCP servers, subagents) portable across machines and correctly structured per Claude Code conventions.

**Architecture:** All 15 hook commands currently hardcode `${HOME}/agent-zero/.venv/bin/python` and `${HOME}/genesis/` — wrong venv, wrong portability. We introduce two self-locating launcher scripts (one for hooks, one for MCP servers) that resolve the genesis root via `git rev-parse`, then add a setup script to regenerate config for any machine. Subagents go in `.claude/agents/` to complete the Claude setup quad.

**Tech Stack:** Bash (launchers), Python 3.12 (setup script), Claude Code `.claude/` conventions

---

## Gaps Being Closed

| Gap | Current State | Target State |
|-----|--------------|--------------|
| Hook venv | Uses `agent-zero/.venv` python | Uses `genesis/.venv` python |
| Hook path coupling | 15 hardcoded `${HOME}/genesis/` paths | 15 paths to a single launcher |
| Hook location | Scripts scattered in `scripts/` | Launcher in `.claude/hooks/`, scripts stay in `scripts/` |
| MCP path coupling | 4 hardcoded `${HOME}/genesis/` paths | 4 paths to a single MCP wrapper |
| Machine onboarding | Manual path editing | One-command setup script |
| Subagents | None (`.claude/agents/` missing) | 2 Genesis-specific agent personas |

---

## File Map

**Create:**
- `.claude/hooks/genesis-hook` — self-locating hook launcher (bash)
- `.claude/mcp/run-mcp-server` — self-locating MCP server launcher (bash)
- `scripts/setup_claude_config.py` — regenerates settings.json + .mcp.json for current machine
- `.claude/agents/genesis-investigator.md` — diagnostic agent persona
- `.claude/agents/genesis-architect.md` — architecture review agent persona

**Modify:**
- `.claude/settings.json` — update all 15 hook commands to use genesis-hook launcher + genesis venv
- `.mcp.json` — update all 4 MCP server commands to use run-mcp-server launcher

---

## Task 1: Create the Hook Launcher

**Files:**
- Create: `.claude/hooks/genesis-hook`

This script self-locates the genesis root via `git rev-parse`, then delegates to genesis's own venv. Because it uses its own `__dir__` to find itself, it works on any machine without config changes — only the path to `genesis-hook` itself in settings.json needs updating per machine.

- [x] **Step 1: Create `.claude/hooks/` directory and launcher script**

```bash
mkdir -p ${HOME}/genesis/.claude/hooks
```

Create `.claude/hooks/genesis-hook`:
```bash
#!/bin/bash
# Self-locating genesis hook launcher.
# Resolves genesis root from this script's location — portable across machines.
# Usage: genesis-hook <script_name.py> [args...]
#
# On a new machine: update the path to THIS script in .claude/settings.json.
# The script itself will find the correct genesis root and venv automatically.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GENESIS_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PYTHON="$GENESIS_ROOT/.venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
    echo "ERROR: Genesis venv not found at $GENESIS_ROOT/.venv/bin/python" >&2
    echo "Run: cd $GENESIS_ROOT && python -m venv .venv && pip install -e ." >&2
    exit 1
fi

SCRIPT_NAME="${1:?Usage: genesis-hook <script_name.py> [args...]}"
SCRIPT_PATH="$GENESIS_ROOT/scripts/$SCRIPT_NAME"

if [[ ! -f "$SCRIPT_PATH" ]]; then
    echo "ERROR: Hook script not found: $SCRIPT_PATH" >&2
    exit 1
fi

shift
exec "$PYTHON" "$SCRIPT_PATH" "$@"
```

- [x] **Step 2: Make it executable**

```bash
chmod +x ${HOME}/genesis/.claude/hooks/genesis-hook
```

- [x] **Step 3: Test the launcher directly**

```bash
cd ${HOME}/genesis
.claude/hooks/genesis-hook genesis_session_context.py 2>&1 | head -5
```

Expected: Runs without "venv not found" error. May print output or exit 0.

- [x] **Step 4: Commit**

```bash
cd ${HOME}/genesis
git add .claude/hooks/genesis-hook
git commit -m "feat(claude-setup): add self-locating hook launcher"
```

---

## Task 2: Update Hook Commands in settings.json

**Files:**
- Modify: `.claude/settings.json`

Replace every `agent-zero/.venv/bin/python ${HOME}/genesis/scripts/<script>.py` pattern with `${HOME}/genesis/.claude/hooks/genesis-hook <script>.py`. This fixes both the wrong-venv bug AND reduces the per-hook path to just the launcher path + script name.

Note: The two `bash -c '...'` hooks (pip-install guard and git safety guard) stay as-is — they're pure bash, no Python invocation.

- [x] **Step 1: Read current settings.json to capture it before editing**

```bash
cat ${HOME}/genesis/.claude/settings.json
```

- [x] **Step 2: Replace all Python hook invocations**

The pattern to replace in every hook command:
- From: `${HOME}/agent-zero/.venv/bin/python ${HOME}/genesis/scripts/<name>.py`
- To: `${HOME}/genesis/.claude/hooks/genesis-hook <name>.py`

Also covers `echo "$CLAUDE_TOOL_INPUT" | ${HOME}/agent-zero/.venv/bin/python ${HOME}/genesis/scripts/<name>.py` patterns:
- From: `echo "$CLAUDE_TOOL_INPUT" | ${HOME}/agent-zero/.venv/bin/python ${HOME}/genesis/scripts/<name>.py`
- To: `echo "$CLAUDE_TOOL_INPUT" | ${HOME}/genesis/.claude/hooks/genesis-hook <name>.py`

Edit `.claude/settings.json` — full hook command replacements:

| Event | Old command | New command |
|-------|-------------|-------------|
| SessionStart[0] | `${HOME}/agent-zero/.venv/bin/python ${HOME}/genesis/scripts/genesis_session_context.py` | `${HOME}/genesis/.claude/hooks/genesis-hook genesis_session_context.py` |
| SessionStart[1] | `${HOME}/agent-zero/.venv/bin/python ${HOME}/genesis/scripts/check_stale_pending.py` | `${HOME}/genesis/.claude/hooks/genesis-hook check_stale_pending.py` |
| UserPromptSubmit[0] | `${HOME}/agent-zero/.venv/bin/python ${HOME}/genesis/scripts/genesis_urgent_alerts.py` | `${HOME}/genesis/.claude/hooks/genesis-hook genesis_urgent_alerts.py` |
| UserPromptSubmit[1] | `${HOME}/agent-zero/.venv/bin/python ${HOME}/genesis/scripts/review_enforcement_prompt.py` | `${HOME}/genesis/.claude/hooks/genesis-hook review_enforcement_prompt.py` |
| UserPromptSubmit[2] | `${HOME}/agent-zero/.venv/bin/python ${HOME}/genesis/scripts/proactive_memory_hook.py` | `${HOME}/genesis/.claude/hooks/genesis-hook proactive_memory_hook.py` |
| PreToolUse[0] | `${HOME}/agent-zero/.venv/bin/python ${HOME}/genesis/scripts/procedure_advisor.py` | `${HOME}/genesis/.claude/hooks/genesis-hook procedure_advisor.py` |
| PreToolUse[2] | `${HOME}/agent-zero/.venv/bin/python ${HOME}/genesis/scripts/review_enforcement_commit.py` | `${HOME}/genesis/.claude/hooks/genesis-hook review_enforcement_commit.py` |
| PreToolUse[4] | `echo "$CLAUDE_TOOL_INPUT" \| ${HOME}/agent-zero/.venv/bin/python ${HOME}/genesis/scripts/pretool_check.py` | `echo "$CLAUDE_TOOL_INPUT" \| ${HOME}/genesis/.claude/hooks/genesis-hook pretool_check.py` |
| PreToolUse[5] | `echo "$CLAUDE_TOOL_INPUT" \| ${HOME}/agent-zero/.venv/bin/python ${HOME}/genesis/scripts/behavioral_linter.py` | `echo "$CLAUDE_TOOL_INPUT" \| ${HOME}/genesis/.claude/hooks/genesis-hook behavioral_linter.py` |
| Stop[0] | `${HOME}/agent-zero/.venv/bin/python ${HOME}/genesis/scripts/genesis_stop_hook.py` | `${HOME}/genesis/.claude/hooks/genesis-hook genesis_stop_hook.py` |
| SessionEnd[0] | `${HOME}/agent-zero/.venv/bin/python ${HOME}/genesis/scripts/genesis_session_end.py` | `${HOME}/genesis/.claude/hooks/genesis-hook genesis_session_end.py` |
| PostToolUse[0] | `${HOME}/agent-zero/.venv/bin/python ${HOME}/genesis/scripts/content_safety_hook.py` | `${HOME}/genesis/.claude/hooks/genesis-hook content_safety_hook.py` |
| PostToolUse[1] | `${HOME}/agent-zero/.venv/bin/python ${HOME}/genesis/scripts/plan_bookmark_hook.py` | `${HOME}/genesis/.claude/hooks/genesis-hook plan_bookmark_hook.py` |

The two bash-only hooks (pip editable guard, git safety guard, YouTube URL guard) are unchanged.

- [x] **Step 3: Verify no agent-zero venv references remain**

```bash
grep -c "agent-zero" ${HOME}/genesis/.claude/settings.json
```

Expected: `0`

- [x] **Step 4: Verify hook count unchanged**

```bash
cat ${HOME}/genesis/.claude/settings.json | python3 -c "
import json, sys
d = json.load(sys.stdin)
hooks = d.get('hooks', {})
total = sum(len(e.get('hooks',[])) for entries in hooks.values() for e in entries)
print('Total hooks:', total)
print('By event:', {k: sum(len(e.get('hooks',[])) for e in v) for k,v in hooks.items()})
"
```

Expected: Same counts as before (15 total across 6 event types).

- [x] **Step 5: Smoke-test a hook end-to-end**

```bash
# Simulate running the session context hook manually
${HOME}/genesis/.claude/hooks/genesis-hook genesis_session_context.py 2>&1 | head -10
```

Expected: No venv error, output from the script.

- [x] **Step 6: Commit**

```bash
cd ${HOME}/genesis
git add .claude/settings.json
git commit -m "fix(hooks): switch from agent-zero venv to genesis venv, use self-locating launcher"
```

---

## Task 3: Create the MCP Server Launcher

**Files:**
- Create: `.claude/mcp/run-mcp-server`
- Modify: `.mcp.json`

Same pattern as hooks but for long-lived MCP server processes.

- [x] **Step 1: Create `.claude/mcp/` and launcher**

```bash
mkdir -p ${HOME}/genesis/.claude/mcp
```

Create `.claude/mcp/run-mcp-server`:
```bash
#!/bin/bash
# Self-locating MCP server launcher.
# Resolves genesis root from this script's location — portable across machines.
# Usage: run-mcp-server --server <name>
#
# On a new machine: update the path to THIS script in .mcp.json.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GENESIS_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PYTHON="$GENESIS_ROOT/.venv/bin/python"
MCP_SCRIPT="$GENESIS_ROOT/scripts/genesis_mcp_server.py"

if [[ ! -x "$PYTHON" ]]; then
    echo "ERROR: Genesis venv not found at $GENESIS_ROOT/.venv/bin/python" >&2
    exit 1
fi

if [[ ! -f "$MCP_SCRIPT" ]]; then
    echo "ERROR: MCP server script not found: $MCP_SCRIPT" >&2
    exit 1
fi

exec "$PYTHON" "$MCP_SCRIPT" "$@"
```

- [x] **Step 2: Make executable**

```bash
chmod +x ${HOME}/genesis/.claude/mcp/run-mcp-server
```

- [x] **Step 3: Update `.mcp.json`**

Replace the full content of `.mcp.json`:
```json
{
  "mcpServers": {
    "genesis-health": {
      "command": "${HOME}/genesis/.claude/mcp/run-mcp-server",
      "args": ["--server", "health"]
    },
    "genesis-memory": {
      "command": "${HOME}/genesis/.claude/mcp/run-mcp-server",
      "args": ["--server", "memory"]
    },
    "genesis-outreach": {
      "command": "${HOME}/genesis/.claude/mcp/run-mcp-server",
      "args": ["--server", "outreach"]
    },
    "genesis-recon": {
      "command": "${HOME}/genesis/.claude/mcp/run-mcp-server",
      "args": ["--server", "recon"]
    }
  }
}
```

- [x] **Step 4: Verify no hardcoded script paths in .mcp.json**

```bash
grep -c "scripts/" ${HOME}/genesis/.mcp.json
```

Expected: `0`

- [x] **Step 5: Test the MCP launcher**

```bash
${HOME}/genesis/.claude/mcp/run-mcp-server --server health --help 2>&1 | head -5
```

Expected: Help output or clean startup, no venv error.

- [x] **Step 6: Commit**

```bash
cd ${HOME}/genesis
git add .claude/mcp/run-mcp-server .mcp.json
git commit -m "feat(claude-setup): add self-locating MCP server launcher, simplify .mcp.json"
```

---

## Task 4: Machine Onboarding Script

**Files:**
- Create: `scripts/setup_claude_config.py`

New machines only need this script to regenerate settings.json and .mcp.json with the correct absolute paths for wherever the repo is cloned. After cloning on a new machine, `python scripts/setup_claude_config.py` is the entire setup.

- [x] **Step 1: Create the setup script**

Create `scripts/setup_claude_config.py`:
```python
#!/usr/bin/env python3
"""
Genesis Claude config setup script.
Run once after cloning on a new machine to update absolute paths in:
  - .claude/settings.json (hook launcher paths)
  - .mcp.json (MCP server launcher paths)

Usage: python scripts/setup_claude_config.py [--genesis-root /path/to/genesis]
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path


def find_genesis_root() -> Path:
    """Find genesis root from this script's location."""
    return Path(__file__).resolve().parent.parent


def update_hook_commands(settings: dict, old_root: str, new_root: str) -> tuple[dict, int]:
    """Replace old genesis root in all hook commands. Returns (updated_settings, count)."""
    raw = json.dumps(settings)
    count = raw.count(old_root)
    updated = raw.replace(old_root, new_root)
    return json.loads(updated), count


def find_current_root_in_settings(settings: dict) -> str | None:
    """Detect the genesis root currently encoded in settings.json."""
    raw = json.dumps(settings)
    # Look for pattern: /some/path/.claude/hooks/genesis-hook
    match = re.search(r'(/[^\s"]+)/.claude/hooks/genesis-hook', raw)
    if match:
        return match.group(1)
    # Fallback: look for /some/path/scripts/
    match = re.search(r'(/[^\s"]+)/scripts/genesis_session_context', raw)
    if match:
        return match.group(1)
    return None


def main():
    parser = argparse.ArgumentParser(description="Set up Claude config for this machine")
    parser.add_argument("--genesis-root", type=Path, help="Override genesis root path")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without writing")
    args = parser.parse_args()

    genesis_root = args.genesis_root or find_genesis_root()
    genesis_root = genesis_root.resolve()

    settings_path = genesis_root / ".claude" / "settings.json"
    mcp_path = genesis_root / ".mcp.json"

    if not settings_path.exists():
        print(f"ERROR: settings.json not found at {settings_path}", file=sys.stderr)
        sys.exit(1)

    # --- Update settings.json ---
    settings = json.loads(settings_path.read_text())
    old_root = find_current_root_in_settings(settings)

    if old_root is None:
        print("WARNING: Could not detect current genesis root in settings.json")
        print("         Hooks may already be up-to-date or use an unexpected pattern")
    elif old_root == str(genesis_root):
        print(f"settings.json: already correct ({genesis_root})")
    else:
        updated_settings, count = update_hook_commands(settings, old_root, str(genesis_root))
        print(f"settings.json: replacing {old_root!r} → {genesis_root!r} ({count} occurrences)")
        if not args.dry_run:
            settings_path.write_text(json.dumps(updated_settings, indent=2) + "\n")
            print("  ✓ Written")

    # --- Update .mcp.json ---
    if not mcp_path.exists():
        print(f"WARNING: .mcp.json not found at {mcp_path}")
    else:
        mcp = json.loads(mcp_path.read_text())
        raw = json.dumps(mcp)
        # Detect current root from MCP config
        mcp_match = re.search(r'(/[^\s"]+)/.claude/mcp/run-mcp-server', raw)
        if mcp_match:
            mcp_old_root = mcp_match.group(1)
            if mcp_old_root == str(genesis_root):
                print(f".mcp.json: already correct ({genesis_root})")
            else:
                updated_raw = raw.replace(mcp_old_root, str(genesis_root))
                count = raw.count(mcp_old_root)
                print(f".mcp.json: replacing {mcp_old_root!r} → {genesis_root!r} ({count} occurrences)")
                if not args.dry_run:
                    mcp_path.write_text(json.dumps(json.loads(updated_raw), indent=2) + "\n")
                    print("  ✓ Written")
        else:
            print(".mcp.json: WARNING — could not detect current root pattern, skipping")

    if args.dry_run:
        print("\n(dry run — no files written)")
    else:
        print(f"\nSetup complete. Genesis root: {genesis_root}")
        print("Restart Claude Code to pick up hook changes.")


if __name__ == "__main__":
    main()
```

- [x] **Step 2: Test dry run**

```bash
cd ${HOME}/genesis && source .venv/bin/activate
python scripts/setup_claude_config.py --dry-run
```

Expected output:
```
settings.json: already correct (${HOME}/genesis)
.mcp.json: already correct (${HOME}/genesis)

(dry run — no files written)
```

- [x] **Step 3: Test with an explicit override (simulates new machine)**

```bash
python scripts/setup_claude_config.py --genesis-root ${HOME}/genesis --dry-run
```

Expected: "already correct" for both files (confirming idempotency on the current machine).

- [x] **Step 4: Add setup instructions to CLAUDE.md**

In the `## Common Commands` section of `CLAUDE.md`, add:
```markdown
python scripts/setup_claude_config.py          # One-time setup after clone on new machine
```

- [x] **Step 5: Commit**

```bash
cd ${HOME}/genesis
git add scripts/setup_claude_config.py CLAUDE.md
git commit -m "feat(claude-setup): add machine onboarding script for Claude config"
```

---

## Task 5: Create Genesis Subagents

**Files:**
- Create: `.claude/agents/genesis-investigator.md`
- Create: `.claude/agents/genesis-architect.md`

Subagents in `.claude/agents/` are persistent agent personas available to `claude --agent` invocations and subagent dispatches. These give Genesis-specific subagent sessions the context they need without requiring it to be injected at dispatch time.

- [x] **Step 1: Create `.claude/agents/` directory**

```bash
mkdir -p ${HOME}/genesis/.claude/agents
```

- [x] **Step 2: Create genesis-investigator agent**

Create `.claude/agents/genesis-investigator.md`:
```markdown
---
name: genesis-investigator
description: Diagnoses Genesis subsystem failures. Use when something is broken, degraded, or reporting unexpected state. Knows the full observability stack, event schema, and where to look for root causes.
tools: Bash, Read, Grep, Glob, mcp__genesis-health__health_status, mcp__genesis-health__health_errors, mcp__genesis-health__health_alerts, mcp__genesis-health__subsystem_heartbeats, mcp__genesis-health__job_health
---

You are a diagnostic agent for the Genesis AI system. Your job is to find root causes, not symptoms.

## What You Know

**Database**: `~/genesis/data/genesis.db`
Key tables: `events` (all system events), `observations` (signal log), `sessions` (CC session log), `task_queue` (autonomy tasks), `outreach_queue` (pending messages), `dead_letter` (failed operations).

**Subsystems and their health signals:**
- `awareness`: periodic tick events in `events` table, type `awareness.tick`
- `reflection`: events with type `reflection.*`, heartbeats in `subsystem_heartbeats`
- `pipeline`: events with type `pipeline.*`
- `learning`: events with type `learning.*`
- `inbox`: file presence in `~/inbox/`, events with type `inbox.*`
- `guardian`: events with type `guardian.*`, host VM health via `guardian.diagnosis`
- `cc_relay`: events with type `cc.*`, bridge logs at `~/genesis/logs/bridge.log`

**Common failure patterns:**
- "degraded" status = capability initialized but not functioning correctly
- Missing heartbeats = subsystem initialized but event loop died
- Dead-letter accumulation = operation failing repeatedly
- Circuit breaker open = provider down or rate-limited

## Investigation Workflow

1. Check `health_status` MCP tool for current subsystem states
2. Check `health_errors` for recent error events
3. Query the `events` table directly for the affected subsystem
4. Check bridge logs if CC-related: `tail -100 ~/genesis/logs/bridge.log`
5. Check systemd service status: `systemctl --user status genesis-bridge`
6. Identify the last known-good state and what changed since

## Rules

- State confidence levels explicitly: "70% this is X because Y"
- Do not propose fixes until root cause is confirmed
- If you can't confirm root cause, say what additional instrumentation would confirm it
- Quote the actual log lines or query results that support your diagnosis
```

- [x] **Step 3: Create genesis-architect agent**

Create `.claude/agents/genesis-architect.md`:
```markdown
---
name: genesis-architect
description: Reviews architectural decisions for Genesis. Use when evaluating new subsystems, integration patterns, or significant refactors. Enforces Genesis design principles and catches long-term liabilities.
tools: Read, Grep, Glob, Bash
---

You are an architecture review agent for the Genesis AI system. Your job is to catch what the implementer missed: wrong abstractions, scope creep, violated invariants, integration liabilities.

## Genesis Design Principles (Non-Negotiable)

1. **Flexibility > lock-in**: Every external dependency must be swappable. Adapter patterns, generic interfaces. A new provider should be a config change, not a refactor.

2. **LLM-first solutions**: Code handles structure (timeouts, validation, event wiring). Judgment belongs to the LLM. Prefer better prompts over heuristics.

3. **Quality over cost — always**: Cost tracking is observability, NEVER automatic control. No auto-throttling, no auto-degrading. The user decides tradeoffs. Genesis provides levers, never pulls them unilaterally.

4. **File size discipline**: Target ~600 LOC per file, hard cap 1000. Package-with-submodules pattern for splits.

5. **Built ≠ wired**: Every component must have a live call site in the actual runtime path. No dead code, no "will be wired later."

6. **CAPS markdown convention**: User-editable LLM behavior files use UPPERCASE filenames (SOUL.md, USER.md). Transparency breeds trust.

7. **Tool scoping**: `disallowed_tools` blacklists work with `--dangerously-skip-permissions`. `allowed_tools` whitelists are ignored. Don't fight autonomous sessions with tool restrictions — use PreToolUse hooks instead.

## V3 Scope Fence

V3 = conservative. Flag anything that looks like:
- V4: adaptive weights, channel learning, meta-prompting, procedural decay
- V5: identity evolution, meta-learning, LoRA fine-tuning
- L5-L7 autonomy actions without explicit approval gates

## What to Look For

- Hardcoded provider references (should be router/adapter)
- Cost-based decisions in code (should be observability only)
- External state mutations without event emission
- Background tasks without heartbeats
- `asyncio.create_task()` without `tracked_task()`
- `contextlib.suppress(Exception)` in data-returning code
- Bare `except Exception` without specific catches first
- Missing `exc_info=True` on error-path logging

## Review Output Format

For each concern:
1. **What**: specific file:line, exact code
2. **Why it's a problem**: which principle violated, what failure mode
3. **Confidence**: explicit percentage with rationale
4. **Fix**: concrete code change, not a description of a change
```

- [x] **Step 4: Verify agents load correctly**

```bash
ls -la ${HOME}/genesis/.claude/agents/
cat ${HOME}/genesis/.claude/agents/genesis-investigator.md | head -5
cat ${HOME}/genesis/.claude/agents/genesis-architect.md | head -5
```

Expected: Both files present, frontmatter shows correct `name` field.

- [x] **Step 5: Commit**

```bash
cd ${HOME}/genesis
git add .claude/agents/
git commit -m "feat(claude-setup): add genesis-investigator and genesis-architect subagents"
```

---

## Task 6: End-to-End Verification

Confirm the full Claude setup is working after all changes.

- [x] **Step 1: Verify hook launcher works for all script types**

```bash
# Test a simple hook
${HOME}/genesis/.claude/hooks/genesis-hook genesis_urgent_alerts.py 2>&1 | head -5

# Test a stdin-pipe hook
echo '{"tool":"Read","input":{}}' | ${HOME}/genesis/.claude/hooks/genesis-hook pretool_check.py 2>&1 | head -5
```

Expected: Scripts execute without venv errors.

- [x] **Step 2: Verify no remaining agent-zero venv references in hooks config**

```bash
grep -r "agent-zero" ${HOME}/genesis/.claude/settings.json
```

Expected: No output.

- [x] **Step 3: Verify MCP launcher works**

```bash
timeout 3 ${HOME}/genesis/.claude/mcp/run-mcp-server --server health 2>&1 | head -5 || true
```

Expected: Server starts (may timeout after 3s, that's fine — confirms it launches).

- [x] **Step 4: Verify setup script is idempotent**

```bash
cd ${HOME}/genesis && source .venv/bin/activate
python scripts/setup_claude_config.py
```

Expected: "already correct" for both files, no changes made.

- [x] **Step 5: Verify agents directory is complete**

```bash
ls ${HOME}/genesis/.claude/agents/
```

Expected: `genesis-investigator.md` and `genesis-architect.md`

- [x] **Step 6: Run lint and tests**

```bash
cd ${HOME}/genesis && source .venv/bin/activate
ruff check scripts/setup_claude_config.py
pytest tests/ -v -k "not slow" --tb=short 2>&1 | tail -20
```

Expected: `ruff` clean, no regressions in test suite.

- [x] **Step 7: Final commit (if any cleanup needed)**

```bash
cd ${HOME}/genesis
git status
# Stage only if there are unintended uncommitted files
git log --oneline -6
```

---

## New Machine Onboarding (Post-Implementation)

After this plan is complete, setting up Genesis on a new machine is:

```bash
git clone git@github.com:YOUR_GITHUB_USER/GENesis.git ~/genesis
cd ~/genesis
python -m venv .venv && source .venv/bin/activate && pip install -e .
python scripts/setup_claude_config.py
```

That's it. The setup script updates settings.json and .mcp.json with correct paths for wherever the repo is cloned.
