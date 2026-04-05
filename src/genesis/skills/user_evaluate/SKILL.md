---
name: user_evaluate
description: Evaluate content for personal relevance to the user using the user model
consumer: cc_background_inbox
phase: 6
skill_type: workflow
---

# User Evaluate

## Purpose

Evaluate content through the lens of what Genesis knows about the user. The
differentiator vs generic AI summary is the user model — Genesis's accumulated
understanding of who this person is, what they care about, and what they're
working on. Assume everything matters; find HOW it matters.

## When to Use

- User drops personal content (articles, research, ideas) into the inbox.
- Content is not Genesis-architecture-relevant but matters to the user.
- Inbox monitor classifies an item as user-relevant.
- User invokes `/user-evaluate` in a foreground session.

## Workflow

1. **Assemble user context** — Read USER.md, query `memory_recall` MCP for
   topics related to the content, check user_model_cache and recent observations.
   USER.md is the floor; the memory system is the ceiling.
2. **Fetch content** — If URLs, fetch and read actual content. Never evaluate
   based on URL text alone. Exhaust all access methods before reporting failure.
3. **Apply four lenses** — Evaluate through all four, do not skip or collapse:
   - **What This Is** — content-native analysis (argument, evidence, contribution)
   - **How This Could Help You** — user-model-informed value extraction
   - **What We Could Do With It** — collaborative actions (Genesis + user)
   - **What to Watch** — critical assessment (gaps, biases, counterarguments)
4. **Tag (report-only)** — Suggest Action Timeline (Now/Soon/Someday) and
   Relevance (Direct/Tangential/Background). These are recommendations, NOT
   binding metadata.
5. **Write output** — Structured evaluation in the format below.

## Output Format

```yaml
target: <what was evaluated>
date: <YYYY-MM-DD>
summary: <2-3 sentences — what it is and why it might matter to the user>
action_timeline: Now | Soon | Someday
relevance: Direct | Tangential | Background
action_items:
  - <concrete collaborative next step if any>
reasoning: |
  <four-lens evaluation, 3-5 paragraphs>
```

## Key Rules

- **Assume it matters.** The user put it here for a reason. Find the value.
- **Never dismiss** content because the user model doesn't mention this topic.
- **Never over-filter** based on the user's known profile. They may be exploring
  new interests.
- **"We" framing** — actions are collaborative (Genesis + user), not reports.
- **Tags are suggestions** — Genesis does not dictate priority to the user.

## References

- `src/genesis/identity/USER.md` — Compressed user snapshot
- `docs/actions/user/active.md` — User action item tracking
- `docs/actions/README.md` — Action item conventions
