---
name: cc-update
description: >-
  Update Claude Code (the CC CLI / "clog code") to a new version, or bump the pinned CC
  version. Use when the user asks to update Claude Code, bump the CC pin, evaluate a new CC
  release, or says "clog code update". Routes to the canonical, standardized process in
  docs/reference/cc-compatibility.md — do NOT re-derive the update mechanism by grepping every
  time. Do NOT use for general "what changed in CC" trivia with no intent to update.
---

# CC Update — start from the canonical source, don't rediscover

Genesis's Claude Code update process is fully standardized. This skill exists so a session
**executes** that process instead of re-deriving it (grep archaeology every time). If you find
yourself hunting for where the pin lives or how the host updates, STOP and read the doc.

## Authoritative source — read these FIRST
- `docs/reference/cc-compatibility.md` → **§Updating Claude Code (host + container)** (the exact
  steps) and **§CC Update Evaluation Checklist** (the 8-lens impact eval).
- The pin is the single source of truth: `CC_VERSION` (+ `NODE_MAJOR`) in
  `scripts/lib/cc_version.sh`.

## The process in one breath (details live in the doc)
1. **Impact eval** — `recon_cc_update_check(old, new)` MCP (backed by
   `src/genesis/recon/cc_update_analyzer.py`). Note anything hooks / MCP / CLI-flags /
   subagents / permissions-relevant.
2. **Bump the pin** — `CC_VERSION` in `scripts/lib/cc_version.sh`. Bump `NODE_MAJOR` in the same
   file **only if** the new CC raises its `engines.node` floor — the `cc-node-lockstep` CI job
   (`scripts/check_cc_node_lockstep.py`) fails the PR otherwise.
3. **Update the doc** — `docs/reference/cc-compatibility.md`: §Current CC Version + a
   Version-History row + any new caveats (checklist step 8).
4. **PR → CI green** (incl. `cc-node-lockstep`) → private-data scan → **explicit user approval**
   → squash-merge. Then `git pull --rebase origin main`.
5. **Host-Deploy Gate** — `scripts/lib/cc_version.sh` is host-deployed. In the SAME session after
   merge, run `scripts/update.sh` from `~/genesis`: it aligns the **container**
   (`cc_ensure_local`) AND the **host VM** (guardian `update-cc` op) to the pin, idempotently.
   Between updates the nightly `genesis-cc-align.timer` closes drift.
6. **Verify E2E** — container + host `claude --version` == pin (host via the gateway `version` op /
   `~/.genesis/host_gateway_state.json`); guardian tick healthy; a targeted CC integration smoke
   (CCInvoker / a headless `claude -p`). The running foreground session keeps its OLD binary until
   its next launch — verify a FRESH process, not this one.

## Durable facts (don't re-derive; verify against the doc if a memory contradicts this)
- **Both container and host install CC via npm-global — there is NO native-installer path**
  (`/usr/bin/claude` → `/usr/lib/node_modules/@anthropic-ai/claude-code`; container under its own
  npm prefix). One canonical copy per machine, enforced by `cc_shadow_scan`. (If a stored memory
  claims the host uses the native installer, it is stale — the doc is authoritative.)
- `origin` = **`GENesis-AGI`** = the **public** repo — merging the pin publishes it; installs pull
  it via `update.sh`. There is no separate private→public step.
- Auto-updater is disabled (`DISABLE_AUTOUPDATER` / `DISABLE_UPDATES` in user-level
  `~/.claude/settings.json`), so the pin is the only mover; an ad-hoc `npm i -g` gets healed back
  to the pin on the next align run.
- Downgrade is supported and deliberate — the pin can go DOWN and `update-cc <older>` rolls the
  host back; there is NO `requiredMinimumVersion` floor (it would remove the incident-recovery
  path and can brick CC).

## Watch-items a CC bump can change under Genesis
Genesis leans on CC internals; a release can shift them silently. When evaluating, check whether
the delta touches:
- **Hooks** — exit-code-2 blocking semantics, `additionalContext`, matcher parsing (our approval
  gate + `bash_safety_hook` + review-enforcement hooks depend on these).
- **Subagents** — nesting default + `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH` /
  `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS` (2.1.217 made nesting opt-in); and whether
  workflow/subagent file-writes still hit our PreToolUse approval gate under `claude -p`.
- **Skills/commands** — e.g. 2.1.215 removed CC's proactive `/code-review` + `/verify`
  auto-invocation (Genesis's own review-enforcement layer is now the primary trigger, not a
  backstop).
- **`--flags` Genesis passes** (`--model`, `--mcp-config`, `--bare`, `-p`, `--append-system-prompt`).

Capture any such change as a follow-up + a doc entry — that is how the next update stays
execute-not-rediscover.
