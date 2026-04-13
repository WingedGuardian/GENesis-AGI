# Genesis Build State

> This file describes the GENERIC structure of Genesis's build state.
> Install-specific pending work, dates, and current project status live
> in MEMORY.md (per-install) and should be checked there.

## How to Check Current State

1. **MEMORY.md** — `## Build State` and `## Pending Implementation Work`
   sections have install-specific pending items with dates and plan pointers
2. **`~/.genesis/capabilities.json`** — written at bootstrap, lists which
   subsystems initialized successfully
3. **`git log --oneline -10`** — recent commits show what changed
4. **`systemctl --user status genesis-server`** — is the server running?

## Subsystem Status Patterns

When checking whether a subsystem is active:
- **ACTIVE** means registered in bootstrap, has callers, produces data
- **INERT** means code exists but zero production callers (e.g., ego/)
- **STUB** means interface exists but implementation is placeholder
- **Skeleton** means registry exists but no real modules registered

Check `src/genesis/runtime/_capabilities.py` `_CAPABILITY_DESCRIPTIONS`
for the authoritative list of registered capabilities.

## Critical Incidents (Generic Patterns)

Genesis has experienced several classes of incidents worth knowing about
to avoid repeats. Install-specific incident details live in
`docs/incidents/` and memory files. Common patterns:

- **Production data deletion** — tests must never touch production Qdrant
  collections or genesis.db without explicit isolation
- **Editable install to worktree** — `pip install -e` to a worktree
  redirects system-wide imports, breaking all Genesis processes
- **Process kill with PGID=1** — `os.killpg(1, sig)` kills ALL user
  processes in the container
- **Auth-exempt endpoint gaps** — new health endpoints must be added to
  auth-exempt list or Guardian probes fail with 401
- **Memory exhaustion** — long-running processes accumulate memory;
  cgroup limits cause OOM without proper reclaim
