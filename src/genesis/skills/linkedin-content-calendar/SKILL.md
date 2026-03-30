---
name: linkedin-content-calendar
description: >
  This skill should be used when the user asks to "plan my LinkedIn content",
  "create a content calendar", "what should I post about this week", "plan my
  posting schedule", or when Genesis proactively suggests a weekly content plan
  during surplus compute. Also triggered by "I need post ideas" or "I don't
  know what to write about".
consumer: cc_foreground, cc_background_surplus
phase: 8
skill_type: workflow
---

# LinkedIn Content Calendar

## Purpose

Plan a cadence of LinkedIn posts that maintains consistent presence without
burning the user out. Produce concrete post ideas with angles — not vague
topics. Balance content types to avoid monotony. Adapt to the user's actual
capacity for posting.

## Calendar Design Principles

**Consistency over volume.** 2 posts/week every week beats 5 posts one week
and silence for three. Start conservative, increase only if sustainable.

**Variety in type.** Rotate through post types (experience, insight, technical,
commentary, question) to avoid becoming predictable. No more than 2 of the
same type in a row.

**Timeliness.** Connect at least one post per cycle to something currently
happening in the user's industry. Evergreen content is fine but a mix with
timely content performs better.

**Effort-awareness.** Some posts require deep thought (experience, insight).
Some are lighter (commentary, question). Alternate high-effort and low-effort
to prevent burnout. Flag effort level on each idea.

## Planning Process

1. **Establish cadence** — Ask the user how often they want to post. Default
   recommendation: 2-3x per week. Minimum viable: 1x per week. Adjust based
   on what the user can actually sustain.

2. **Inventory topics** — Pull from:
   - User's expertise areas (from voice profile)
   - Recent projects or work activity
   - Industry news and trends
   - Previous posts that performed well
   - Topics the user has expressed opinions on
   - Knowledge gaps where writing would force useful thinking
   - Items from the user's inbox or reading list

3. **Generate ideas with angles** — Each idea is a specific angle, not a topic.
   Bad: "Write about Kubernetes." Good: "The hidden cost of K8s that nobody
   talks about — your team's cognitive load." Include the post type and
   estimated effort level.

4. **Schedule** — Assign ideas to specific days/weeks. Consider:
   - Best posting times for the user's audience (typically Tue-Thu mornings)
   - Spacing between similar topics
   - High-effort posts early in the week (more energy)
   - Light posts (commentary, questions) for low-energy days

5. **Buffer** — Always plan 1-2 extra ideas beyond the calendar period. Ideas
   that aren't used roll to the next cycle, not lost.

## Output Format

```markdown
# Content Calendar: [Date Range]

**Cadence:** [N] posts/week
**Post types this cycle:** [breakdown]

## Week of [Date]

### [Day] — [Post Type]
**Angle:** [Specific angle, not just a topic]
**Hook idea:** [One strong opening line option]
**Effort:** Low | Medium | High
**Timely?:** Yes (reason) | No (evergreen)
**Notes:** [Any context — what prompted this idea, related reading]

### [Day] — [Post Type]
...

## Buffer Ideas (Unused — Roll Forward)
- [Angle 1] ([type], [effort])
- [Angle 2] ([type], [effort])
```

## Proactive Calendar Generation

When Genesis generates a content calendar during surplus compute:
- Review what the user has been working on this week (from conversation
  history, inbox items, cognitive state)
- Check what's happening in the user's industry
- Draft a 1-week plan with 2-3 post ideas
- Stage as surplus output for user review
- Include reasoning for why each topic was selected now

## References

- `../linkedin-post-writer/SKILL.md` — Post type definitions, writing process, topic areas
- `../voice-master/references/exemplars/social.md` — Social media voice exemplars
- `../voice-master/references/anti-slop.md` — AI-tell avoidance rules
- `../linkedin-hook-writer/SKILL.md` — Hook generation for calendar ideas
