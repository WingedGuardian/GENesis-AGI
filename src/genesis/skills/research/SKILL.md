---
name: research
description: Deep research on a topic — use when investigating unfamiliar domains, answering complex questions requiring multiple sources, or when an evaluation flags something for deeper analysis
consumer: cc_background_research
phase: 6
skill_type: workflow
---

# Research

## Purpose

Conduct thorough research on a topic, producing a structured summary with
sources and actionable takeaways for Genesis.

## When to Use

- User requests research on a topic.
- A blocker requires understanding an unfamiliar domain.
- Surplus compute is available and a research task is queued.
- An evaluation identified a WATCH or ADOPT item needing deeper analysis.

## Workflow

1. **Scope** — Define the research question clearly. What specifically do we
   need to know? What decisions does this inform?
2. **Gather** — Search web, documentation, code repositories. Collect primary
   sources. Prefer official docs over blog posts.
3. **Synthesize** — Organize findings into a coherent narrative. Identify
   consensus vs. conflicting information.
4. **Assess reliability** — Note source quality, recency, potential bias.
5. **Extract actionables** — What should Genesis do with this information?
6. **Write output** — Structured research report.

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
- `docs/architecture/genesis-v3-gap-assessment.md` — Known gaps
