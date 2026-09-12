---
name: setup
description: >
  First-run onboarding or config repair. Routes on two signals — whether the
  Layer-1 install finished, and whether onboarding has completed
  (~/.genesis/setup-complete). A broken/half-finished install is repaired; a
  complete-but-unconfigured install is onboarded; a configured install is
  refreshed.
---

# Genesis Setup

`/setup` covers two jobs — finishing/repairing the **install** and running
first-run **onboarding** — and routes between them from the system's actual
state. Detect first, then route; do not assume.

## Step 1 — Detect state

Two independent signals:

- **Infrastructure** — did the Layer-1 install (`bootstrap.sh` / `install.sh`)
  actually finish? An interrupted install leaves the venv or dependencies
  incomplete, and the `~/.genesis/setup-complete` marker absent (it is written
  only at the very end). Check that the venv exists and the package imports:

  ```bash
  ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
  "$ROOT/.venv/bin/python" -c "import genesis" 2>/dev/null && echo INFRA_OK || echo INFRA_INCOMPLETE
  ```

- **Onboarding** — has the system been configured? The marker is written at the
  end of onboarding (or by a completed `bootstrap.sh`):

  ```bash
  test -f ~/.genesis/setup-complete && echo ONBOARDED || echo NOT_ONBOARDED
  ```

## Step 2 — Route (decide in this order)

1. **INFRA_INCOMPLETE** — the install is broken or half-finished (e.g. an
   interrupted `bootstrap.sh`). Onboarding cannot run on a broken install, so
   **repair the infrastructure first** → **Config repair** below. After it
   completes, if keys/identity still need configuring, ask Genesis to "run
   onboarding" — the skill will run the sections you need.
2. **INFRA_OK + NOT_ONBOARDED** — a complete install that has not been configured
   → **Guided onboarding** below. (Even if the user said "repair" — there is no
   configured system to repair yet; onboarding *is* the setup.)
3. **INFRA_OK + ONBOARDED** — an already-configured install → **Config repair**
   below (the usual reason to run `/setup` on a working system).

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

For a full install/config + service re-render (this also finishes an interrupted
install), run `./scripts/bootstrap.sh`. It refuses to run while genesis-server is
live — a safety guard, because its crash-recovery path can `git reset --hard` the
tree. Stop the server first, or pass `--force` to override the guard deliberately.

## Re-running onboarding on a configured install

If the marker exists but the user wants to re-onboard or reconfigure one section
(e.g. "reconfigure telegram"), read `src/genesis/skills/onboarding/SKILL.md`
anyway — it detects the existing marker, notes that setup was already completed,
and runs only the requested section.
