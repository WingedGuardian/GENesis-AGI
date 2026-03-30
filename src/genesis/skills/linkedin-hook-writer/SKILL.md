---
name: linkedin-hook-writer
description: >
  This skill should be used when the user asks to "write a hook for my post",
  "give me opening lines", "help me start this LinkedIn post", "I need a
  better opener", or when the linkedin-post-writer skill needs strong opening
  options. Also triggered by "my posts aren't getting clicks" or "how do I
  get people to read my posts".
consumer: cc_foreground, cc_background_surplus
phase: 8
skill_type: workflow
---

# LinkedIn Hook Writer

## Purpose

Generate attention-grabbing opening lines for LinkedIn posts that earn the
click to "...see more" — without resorting to the clickbait patterns that
mark content as AI-generated or engagement-farmed. The hook must be honest:
it promises something the post actually delivers.

## Voice Loading

Read the voice-master anti-slop rules — especially the "Banned Openers" section.
This skill's entire value depends on producing hooks that are NOT on that list.

- `../voice-master/references/anti-slop.md`
- `../voice-master/references/exemplars/social.md`

If the exemplar file is empty or has no good matches, also read:
- `../voice-master/references/voice-dimensions.md`

## How LinkedIn Hooks Work

LinkedIn shows approximately the first 140-210 characters of a post before
truncating with "...see more." The hook must accomplish two things in that
space:

1. **Create genuine curiosity** — the reader wants to know more
2. **Signal quality** — the reader believes the rest is worth their time

A hook that creates curiosity but signals low quality gets skipped.
A hook that signals quality but creates no curiosity gets polite scrolling.

## Hook Patterns (Authentic)

### The Specific Detail
Start with a concrete, surprising detail that makes the reader want context.

- "We migrated 340 microservices in 6 weeks. Here's what almost killed us."
- "My team's Kubernetes bill dropped 40% because of a config nobody checked."
- "I interviewed 12 candidates last month. One question separated the strong ones."

**Why it works:** Specific numbers and details signal real experience. The
reader wants the story behind the detail.

### The Honest Admission
Start by admitting something most people in your position wouldn't say.

- "I've been doing cloud architecture for 8 years and I still don't fully
  understand IAM policies."
- "We shipped a feature last month that I knew wasn't ready. Here's why."
- "I got fired from a job I was good at. The reason surprised me."

**Why it works:** Vulnerability from someone with credibility is rare on
LinkedIn. It signals an honest post, not a performance.

### The Counterintuitive Claim
State something that goes against conventional wisdom — but only if you
can back it up.

- "The worst career advice I ever followed: 'always have an answer.'"
- "We stopped doing code reviews. Our quality went up."
- "Senior engineers who can't explain things simply aren't actually senior."

**Why it works:** Disrupts autopilot scrolling. The reader needs to see
the reasoning.

### The Observation
Notice something specific about the industry, the job, or the professional
world that others feel but haven't articulated.

- "There's a specific kind of tiredness that comes from meetings about
  meetings."
- "Every cloud migration proposal I've seen includes the same lie."
- "The gap between 'we use AI' and 'AI is useful to us' is enormous."

**Why it works:** Recognition — the reader sees their own experience
reflected and wants to see if the post develops it further.

### The Mid-Story Start
Begin in the middle of a situation, not at the beginning.

- "The Slack message said 'production is down' and I hadn't had coffee yet."
- "Halfway through the demo, the CTO asked a question I couldn't answer."
- "The third interview round was when I realized I didn't want the job."

**Why it works:** Narrative momentum — the reader is dropped into a moment
and wants to know what happened.

## Generation Process

1. **Understand the post** — Read the full post content or the angle
   from the content calendar. The hook must honestly represent what follows.

2. **Generate 3-5 options** — Use different patterns. Never generate 5
   variations of the same pattern — that's what AI does when it's lazy.

3. **Anti-slop check** — Verify each option against the banned openers list.
   Delete any that feel like they could appear in a "LinkedIn post template."

4. **Rank** — Evaluate each hook on: curiosity generated, quality signaled,
   authenticity, connection to the post body.

5. **Present with reasoning** — Show the user options with brief notes on
   why each works or might not work.

## Output Format

```markdown
## Hook Options for: [Post Topic/Angle]

### Option 1 (Pattern: [pattern name])
> [Hook text]
**Strength:** [Why this works]
**Risk:** [Why it might not — or "low risk"]

### Option 2 (Pattern: [pattern name])
> [Hook text]
...

### Recommended: Option [N]
**Reasoning:** [Why this one best fits the post and voice]
```

## References

- `../voice-master/references/anti-slop.md` — Banned openers and AI-tell patterns
- `../voice-master/references/exemplars/social.md` — Social media voice exemplars
- `../linkedin-post-writer/SKILL.md` — Post types and writing process
