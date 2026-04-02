# GStack Patches

Genesis-specific customizations applied on top of gstack upstream.

**GStack updates wipe ALL local changes** (`git reset --hard origin/main`).
After any gstack upgrade, run `scripts/apply_gstack_patches.sh` to reapply.

## Patches

### Full File Overlays

| File | Target | What It Does |
|------|--------|-------------|
| `codex-SKILL.md.tmpl` | `codex/SKILL.md.tmpl` | Codex 2.0 fallback chain (Codex CLI -> OpenCode/GLM5 -> Claude subagent). Custom three-tier adversarial review system. |
| `codex-SKILL.md` | `codex/SKILL.md` | Generated from template. Must be regenerated if template changes. |
| `review-checklist.md` | `review/checklist.md` | Adds verification taxonomy section (4-level: exists -> substantive -> wired -> data-flow verified). |

### Patch Scripts

| Script | What It Does |
|--------|-------------|
| `safety-frontmatter.patch` | Adds `disable-model-invocation: true` to ship and land-and-deploy SKILL.md files. |
| `descriptions.patch` | Trims verbose skill descriptions to save ~3k chars of context budget. |

## Baseline

These patches were captured against gstack commit:
```
cd66fc2f890982351e3178925be563681d0ab2c5
fix: 6 critical fixes + community PR guardrails (v0.13.2.0) (#602)
```

## Updating Patches

When gstack updates cause conflicts:
1. Run `scripts/apply_gstack_patches.sh` — it will report conflicts
2. Resolve manually in gstack dir
3. Copy updated files back: `cp ~/.claude/skills/gstack/<path> config/gstack-patches/<name>`
4. Update this README with the new baseline commit
