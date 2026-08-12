---
name: list-skills
description: >
  List every available skill (Tier-1 always-indexed + Tier-2 specialized) with
  a one-line description each, so you can discover under-used capabilities on
  demand. Optional keyword filter.
---

# List Skills

Show a roster of every skill Genesis can invoke, grouped by tier, with a
one-line description each. This complements the automatic skill-injection nudge
(which surfaces one matching skill per prompt) — use `/list-skills` to browse
the full set at will.

## Arguments

Optional filter after `/list-skills`:
- `/list-skills` — list everything.
- `/list-skills browser` — only skills whose name, description, or keywords
  match "browser" (case-insensitive).

## Steps

1. **Ensure the catalog exists.** The roster is read from the prebuilt catalog
   at `~/.genesis/skill_catalog.json` (regenerated hourly by
   `scripts/generate_skill_catalog.py`). On a fresh install it may not exist
   yet — generate it once if missing (this does NOT run when it already exists,
   so it never races the hourly refresh):

   ```bash
   test -f ~/.genesis/skill_catalog.json \
     || python3 "$(git rev-parse --show-toplevel)/scripts/generate_skill_catalog.py"
   ```

2. **Read the catalog** (a single read-only file — do NOT regenerate it when it
   already exists):

   ```bash
   cat ~/.genesis/skill_catalog.json
   ```

   Structure: `{ "generated_at", "tier1": [...], "tier2": [...] }`, each entry
   `{ name, description, keywords, tier, path }`. Tier-1 skills live in
   `.claude/skills/`; Tier-2 in `src/genesis/skills/` and
   `~/.genesis/skill-library/`.

   If the file is still missing, empty, or not valid JSON (e.g. caught
   mid-regeneration or hand-edited), do NOT render an empty roster as though no
   skills exist — tell the user the catalog is unavailable and suggest
   re-running `python3 scripts/generate_skill_catalog.py`, then stop.

3. **Present the roster**, grouped and sorted by name within each group:
   - **Tier 1 — always-indexed** — the `tier1` entries.
   - **Tier 2 — specialized** — the `tier2` entries.
   - One line per skill: `**<name>** — <first sentence of its description>`.
     Keep each to a single scannable line; truncate long descriptions rather
     than reflowing them.
   - Show the count per tier and the grand total.

4. **Apply the filter** when an argument is given: include only skills whose
   name, description, or keywords contain the argument (case-insensitive). If
   nothing matches, say so plainly rather than showing an empty list.
