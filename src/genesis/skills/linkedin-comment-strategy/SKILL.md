---
name: linkedin-comment-strategy
description: >
  This skill should be used when the user asks to "write a comment for this
  LinkedIn post", "help me respond to this post", "what should I comment on
  this", "craft a LinkedIn comment", or when Genesis identifies high-value
  posts in the user's network worth engaging with. Also triggered by "how
  should I engage on LinkedIn" or "help me be more visible on LinkedIn".
consumer: cc_foreground
phase: 8
skill_type: workflow
---

# LinkedIn Comment Strategy

## Purpose

Write LinkedIn comments that build genuine professional visibility — not
generic "Great post!" noise. Every comment should demonstrate expertise,
add value to the conversation, or make the author want to respond. Comments
are the highest-ROI LinkedIn activity for building network presence.

## Voice Loading

Before writing comments, read voice exemplars and anti-slop rules:
- `../voice-master/references/exemplars/social.md`
- `../voice-master/references/anti-slop.md`

If the exemplar file is empty or has no good matches, also read:
- `../voice-master/references/voice-dimensions.md`

Comments follow the same voice rules as posts but in compressed form.

## Comment Types

### 1. Add-Value Comment
Extend the post's point with additional insight, a related experience, or
a nuance the author didn't cover.

**When to use:** The post makes a good point that you can genuinely build on.
**Length:** 2-4 sentences.
**Example pattern:** "[Specific agreement with a detail]. In my experience
with [specific context], [additional insight]. [Optional: question or
implication]."

### 2. Respectful Challenge
Disagree with or complicate the post's thesis — with reasoning, not
contrarianism.

**When to use:** The post oversimplifies something or misses a key angle.
**Length:** 3-5 sentences.
**Example pattern:** "[Acknowledge the valid part]. Where I'd push back is
[specific point] because [evidence/experience]. [What this changes about
the conclusion]."

### 3. Experience Share
Contribute a relevant personal experience that illustrates or complicates
the post's point.

**When to use:** The post resonates with something you've lived through.
**Length:** 3-6 sentences.
**Example pattern:** "[Brief connection to the post]. When I was [specific
situation], [what happened]. [What you learned or how it relates]."

### 4. Question Comment
Ask a genuine question that advances the discussion. Not a rhetorical
question for engagement — a question you'd actually want answered.

**When to use:** The post raises something you're genuinely curious about.
**Length:** 1-3 sentences.
**Example pattern:** "[What specifically triggered the question]. How do
you think about [specific aspect]?"

## Anti-Slop Rules for Comments

- NEVER: "Great post, [Name]!" / "Love this!" / "So true!" / "Couldn't agree more!"
- NEVER: "Thanks for sharing this, [Name]!" without adding substance
- NEVER: Tag people who aren't part of the conversation
- NEVER: Use the comment to pivot to self-promotion
- NEVER: Write a comment longer than the original post
- NEVER: Start with an emoji
- Always reference something SPECIFIC from the post (proves you read it)
- Always add something the author or readers didn't have before

## Comment Workflow

1. **Read the post** — The user pastes or describes the post content.

2. **Identify the angle** — What's the most natural type of comment given
   the user's expertise and relationship to the topic?

3. **Check relevance** — Is this a post worth commenting on? Criteria:
   - Author is someone the user wants to build a relationship with
   - Topic intersects with the user's expertise
   - Post has meaningful engagement potential (not dead)
   - Comment would be genuine, not forced

4. **Draft** — Write the comment in voice. Short, specific, adds value.

5. **Anti-slop check** — Verify no banned patterns. Verify it references
   something specific from the post.

## Strategic Commenting Guidance

When the user asks for a general commenting strategy (not a specific comment):

- **Target authors:** People in the user's target audience or industry
  who post regularly and engage with comments. Commenting consistently
  on 5-10 key authors builds more visibility than scattered commenting.

- **Timing:** Comment within the first hour of a post going live. Early
  comments get more visibility as engagement grows.

- **Frequency:** 3-5 meaningful comments per day is more effective than
  15 generic ones. Quality signals expertise; quantity signals desperation.

- **Reciprocity:** When someone comments on the user's posts, prioritize
  engaging with their content. Builds genuine relationships.

## Output Format

```yaml
post_summary: <brief summary of the post being commented on>
comment_type: <add-value | challenge | experience | question>
comment: |
  <the comment text>
rationale: |
  <why this type and angle were chosen>
```

## References

- `../voice-master/references/exemplars/social.md` — Social media voice exemplars
- `../voice-master/references/anti-slop.md` — AI-tell avoidance rules
