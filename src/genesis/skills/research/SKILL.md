---
name: research
description: Deep research on a topic — use when investigating unfamiliar domains, answering complex questions requiring multiple sources, or when an evaluation flags something for deeper analysis
consumer: cc_background_research
phase: 6
skill_type: workflow
---

# Research

This legacy Tier-2 entry delegates the research method to
`.claude/skills/web-research/SKILL.md`. Load and follow that skill. Preserve the
structured output below when a caller requires it.

## Purpose

Conduct thorough research on a topic, producing a structured summary with
sources and actionable takeaways for Genesis.

## When to Use

- User requests research on a topic.
- A blocker requires understanding an unfamiliar domain.
- Surplus compute is available and a research task is queued.
- An evaluation identified a WATCH or ADOPT item needing deeper analysis.

## Output Format

```yaml
topic: <research question>
date: <YYYY-MM-DD>
summary: <3-5 sentence overview>
key_findings:
  - finding: <finding>
    confidence: high | medium | low
    source: <source reference>
action_items:
  - <concrete next step>
open_questions:
  - <what remains unknown>
```

## References

- `docs/architecture/genesis-v3-vision.md` — For relevance filtering
- `docs/architecture/CURRENT.md` — the live subsystem map (what Genesis has
  today, with freshness stamps); consult before claiming a gap. Enumerate,
  don't spot-check.
