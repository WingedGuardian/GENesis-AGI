# Procedure Activation Architecture

**Status:** Active | **Last updated:** 2026-03-18


## Problem

Genesis's Phase 6 procedure learning system stores procedures (task_type,
steps, context_tags, confidence) but nothing creates them and nothing
retrieves them at decision points. Learned knowledge doesn't translate
to behavior across sessions.

## Solution: Four Activation Layers

Reliability decreases as scope increases. Layers reinforce each other.

### Layer 1: PreToolUse Procedure Advisor (highest reliability)
- `scripts/procedure_advisor.py` — fires on every CC tool call
- Reads YAML trigger cache (`config/procedure_triggers.yaml`)
- Outputs JSON with `additionalContext` for matching procedures
- Only L1-tier procedures with tool triggers
- ~10ms overhead per non-matching call

### Layer 2: Skill-Embedded Procedures (active when skill invoked)
- Skills contain `## Known Procedures` section
- Procedures synced from L2-tier via `skills/procedure_sync.py`
- Auto-applied as MINOR changes at L2+ autonomy

### Layer 3: SessionStart Injection (active per session)
- `scripts/genesis_session_context.py` section 2.5
- Injects top-5 L3+ procedures, 200-word budget
- Positioned after cognitive state, before MCP tools hint

### Layer 4: CLAUDE.md / STEERING.md Rule (advisory)
- "Check procedures before multi-step tasks"
- Weakest layer but broadest scope

## Procedure Lifecycle

```
New procedure (triage pipeline or MCP tool)
  → L4 (speculative, advisory only)
  → L3 (3+ successes, conf >= 0.65, non-speculative)
  → L2 (5+ successes, conf >= 0.75, embedded in skills)
  → L1 (8+ successes, conf >= 0.85, tool trigger set)
```

Demotion: 3+ consecutive failures → tier - 1
Quarantine: confidence < 0.3 → excluded from all layers

## Ingestion Paths

1. **Triage pipeline** — extracts procedures from APPROACH_FAILURE and
   WORKAROUND_SUCCESS outcomes via LLM (call site 34)
2. **MCP tool** — `procedure_store` for manual/retrospective creation
3. **Seed script** — `scripts/seed_procedures.py` for battle-tested procedures

## Key Files

| File | Purpose |
|------|---------|
| `src/genesis/learning/procedural/extractor.py` | LLM procedure extraction |
| `src/genesis/learning/procedural/trigger_cache.py` | YAML cache generation |
| `src/genesis/learning/procedural/session_inject.py` | SessionStart injection |
| `src/genesis/learning/procedural/promoter.py` | Tier promotion/demotion |
| `scripts/procedure_advisor.py` | PreToolUse hook |
| `scripts/seed_procedures.py` | Known procedure seeding |
| `config/procedure_triggers.yaml` | L1 trigger cache |

## Hook Registration

The PreToolUse advisor hook must be registered in `.claude/settings.json`:
```json
{
  "matcher": ".*",
  "hooks": [{
    "type": "command",
    "command": "${HOME}/agent-zero/.venv/bin/python ${HOME}/genesis/scripts/procedure_advisor.py",
    "timeout": 2000
  }]
}
```
This is a CRITICAL protected path — requires direct CLI or user action.

---

## Related Documents

- [genesis-v3-autonomous-behavior-design.md](genesis-v3-autonomous-behavior-design.md) — Learning and procedure pipeline
- [genesis-v3-build-phases.md](genesis-v3-build-phases.md) — Phase 6: learning fundamentals
