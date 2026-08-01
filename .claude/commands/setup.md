---
name: setup
description: >
  First-run onboarding or config repair. Routes on install state: a fresh
  install (no ~/.genesis/setup-complete marker) runs guided onboarding; an
  already-configured install runs config / Claude Code-config repair.
---

# Genesis Setup

`/setup` covers two distinct jobs — first-run **onboarding** and config
**repair** — and routes between them based on install state. Detect first, then
route; do not assume.

## Step 1 — Detect install state

```bash
test -f ~/.genesis/setup-complete && echo COMPLETE || echo FRESH
```

## Step 2 — Route

- **FRESH** (marker absent) → **Guided onboarding** (below), *always* — even if
  the user said "repair". A never-onboarded install has no configured system to
  repair yet; onboarding *is* the setup. (Running `bootstrap.sh` here would mark
  setup complete without configuring anything, permanently suppressing the
  first-run onboarding prompt.)
- **COMPLETE** (marker present) → **Config repair** (below). Running `/setup` on
  an already-configured install is almost always a fix/refresh request. To
  deliberately re-onboard or reconfigure one section, see the last section.

### Guided onboarding

Read `src/genesis/skills/onboarding/SKILL.md` and follow its steps: it configures
the user profile, essential API keys, Telegram, GitHub backup, and verifies
endpoints, then writes the `~/.genesis/setup-complete` marker itself at the end.
Do **not** run `bootstrap.sh` for onboarding — bootstrap is infrastructure setup,
not the onboarding flow.

### Config repair

Most repairs (hooks or MCP servers not firing) only need the Claude Code config
re-rendered, which is safe on a running system:

```bash
python scripts/setup_claude_config.py          # .mcp.json only
python scripts/setup_claude_config.py --global  # also update ~/.claude/settings.json
```

For a full config + service re-render, run `./scripts/bootstrap.sh`. It refuses
to run while genesis-server is live — a safety guard, because its crash-recovery
path can `git reset --hard` the tree. Stop the server first, or pass `--force`
to override the guard deliberately.

## Re-running onboarding on a configured install

If the marker exists but the user wants to re-onboard or reconfigure one section
(e.g. "reconfigure telegram"), read `src/genesis/skills/onboarding/SKILL.md`
anyway — it detects the existing marker, notes that setup was already completed,
and runs only the requested section.
