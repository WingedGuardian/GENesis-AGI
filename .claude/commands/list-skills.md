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

1. **Ensure the catalog exists.** The roster is read from the prebuilt catalog
   at `~/.genesis/skill_catalog.json`, which the skill-injection hook
   regenerates (via the repo's Python 3.12 venv) when it is missing or stale —
   that hook runs on every prompt, so the file almost always exists. As a
   cold-start safety net, generate it once if it is still missing, using the
   repo venv interpreter (the generator requires Python 3.12; a bare `python3`
   may resolve to an older system interpreter and fail on `datetime.UTC`):

   ```bash
   ROOT="$(git rev-parse --show-toplevel)"
   PY="$ROOT/.venv/bin/python"; [ -x "$PY" ] || PY=python3
   test -f ~/.genesis/skill_catalog.json || "$PY" "$ROOT/scripts/generate_skill_catalog.py"
   ```

   This does NOT run when the catalog already exists, so it never races the
   hook's refresh.

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
   re-running the generator with the repo venv (root-independent):
   `"$(git rev-parse --show-toplevel)/.venv/bin/python" "$(git rev-parse --show-toplevel)/scripts/generate_skill_catalog.py"`,
   then stop.

3. **Present the roster.** State up front that this is the **Genesis skill
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

4. **Apply the filter** when an argument is given: include only skills whose
   name, description, or keywords contain the argument (case-insensitive). If
   nothing matches, say so plainly rather than showing an empty list.
