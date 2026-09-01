---
name: list-skills
description: >
  List the Genesis skill catalog (Tier-1 always-indexed + Tier-2 specialized)
  with a one-line description each, so you can discover under-used capabilities
  on demand. Optional keyword filter. (Does not list Claude Code plugin skills.)
---

# List Skills

Show a roster of the **Genesis skill catalog** (Tier-1 + Tier-2), grouped by
tier, with a one-line description each. This complements the automatic
skill-injection nudge (which surfaces one matching skill per prompt) — use
`/list-skills` to browse the catalog at will. Note: this lists the Genesis
catalog only, NOT Claude Code plugin skills (e.g. `superpowers:*`), which are
invoked separately and are not part of this catalog.

## Arguments

Optional filter after `/list-skills`:
- `/list-skills` — list everything.
- `/list-skills browser` — only skills whose name, description, or keywords
  match "browser" (case-insensitive).

## Steps

1. **Read the catalog.** The roster comes from the prebuilt catalog at
   `~/.genesis/skill_catalog.json`. Keeping it fresh is the skill-injection
   hook's job — on every prompt it spawns a background regeneration (with the
   correct interpreter) whenever the file is missing or older than an hour, so
   `/list-skills` only READS the catalog and never regenerates it itself:

   ```bash
   cat ~/.genesis/skill_catalog.json
   ```

   Structure: `{ "generated_at", "tier1": [...], "tier2": [...] }`, each entry
   `{ name, description, keywords, tier, path }`. Tier-1 skills live in
   `.claude/skills/`; Tier-2 in `src/genesis/skills/` and
   `~/.genesis/skill-library/`.

   If the catalog is missing, empty, or not valid JSON, do NOT render an empty
   roster as though no skills exist:
   - **Missing or empty** (e.g. a first-ever run before the hook's background
     regeneration has landed): tell the user it is still being generated and to
     run `/list-skills` again shortly.
   - **Present but not valid JSON** (likely corrupt): the hook refreshes only on
     age, so a retry will NOT fix it — tell the user to delete
     `~/.genesis/skill_catalog.json` (the injection hook rebuilds it on the next
     prompt), then re-run `/list-skills`.

   Either way, stop rather than showing an empty roster.

2. **Present the roster.** State up front that this is the **Genesis skill
   catalog** (Tier-1 + Tier-2), NOT session commands or Claude Code plugin
   skills (`superpowers:*` etc.), and show the catalog's `generated_at`
   timestamp so the user can judge freshness (it refreshes at most hourly, so a
   just-added skill may briefly lag). Group and sort by name within each group:
   - **Tier 1 — always-indexed** — the `tier1` entries.
   - **Tier 2 — specialized** — the `tier2` entries.
   - One line per skill: `**<name>** — <first sentence of its description>`.
     Keep each to a single scannable line; truncate long descriptions rather
     than reflowing them.
   - Show the count per tier, the grand total, and the `generated_at` time.

3. **Apply the filter** when an argument is given: include only skills whose
   name, description, or keywords contain the argument (case-insensitive). If
   nothing matches, say so plainly rather than showing an empty list.
